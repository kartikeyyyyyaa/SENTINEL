"""The event vocabulary shared by the edge analytics workers and the state core.

This module is the integration seam of the whole analytics half of the platform, so
it is deliberately tiny, has no dependencies beyond the standard library, and knows
nothing about cameras, models, databases or transports. Everything upstream produces
these objects; everything downstream consumes them.

Three decisions are worth defending, because they are the ones that break systems
like this in production:

1. **Time is always PTS-derived, never arrival time.** The Sentinel grid replays a
   buffered GOP on connect, so the first one to two seconds of a stream arrives
   faster than real time. A worker that timestamps by ``datetime.now()`` will place
   those frames in the future relative to their true capture moment, and every
   cross-camera travel-time calculation downstream inherits the error. ``ts`` on
   every object in this module means *when the photons hit the sensor*, reconstructed
   from the stream's presentation timestamp. See ``StreamClock``.

2. **Geometry is normalised to [0, 1].** The grid mixes resolutions and mixes H.264
   with H.265, so there is no fixed frame shape to batch against and no pixel
   coordinate that means the same thing on two cameras. Normalised boxes survive a
   resolution change mid-stream, which is a thing that actually happens when a
   camera renegotiates its profile.

3. **A sighting is an observation, not a conclusion.** ``Sighting`` records what one
   camera saw. It carries no identity claim and no incident judgement. Deciding that
   two sightings are the same vehicle is the correlation layer's job and is
   explicitly falsifiable there; deciding that a pattern of sightings is a crime is
   the rule engine's job. Keeping those separable is what makes a false positive
   diagnosable instead of mysterious.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------
# Kept as frozensets rather than enums so a value read from JSON or from Postgres
# can be validated without a conversion step, and so an unknown label from a model
# upgrade is a data problem rather than an import-time crash.

#: Object classes the detector may report. Deliberately includes the two Indian
#: road users that COCO-trained models systematically mishandle: an auto-rickshaw
#: is usually reported as ``car`` or ``truck``, and a motorcycle carrying three
#: people is still one ``motorcycle``. Mapping those correctly is a post-processing
#: concern, but the vocabulary has to admit them or the mapping has nowhere to land.
OBJECT_CLASSES = frozenset({
    "person",
    "bicycle",
    "motorcycle",
    "car",
    "auto_rickshaw",
    "bus",
    "truck",
    "tractor",
    "animal",
    "unknown",
})

#: Classes that can carry a number plate, i.e. that are worth sending to ANPR.
#: The cascade uses this to decide whether to spend an OCR call, which is the
#: single most expensive stage in the pipeline.
PLATED_CLASSES = frozenset({
    "motorcycle", "car", "auto_rickshaw", "bus", "truck", "tractor",
})

#: Primitive event kinds. Incidents are *not* in this list on purpose: an incident
#: is a rule's opinion about a sequence of primitives, authored as data in the
#: correlation layer. Adding "hit and run" here would hard-code a policy decision
#: into the wire format and force a redeploy every time the definition is tuned.
PRIMITIVE_KINDS = frozenset({
    "track_start",     # a new object entered the scene
    "track_update",    # periodic heartbeat for a live track
    "track_end",       # object left the scene or was lost
    "anpr",            # a plate was read
    "zone_enter",      # track crossed into a configured polygon
    "zone_exit",
    "dwell",           # track remained in a zone beyond its threshold
    "line_cross",      # track crossed a configured line, with direction
    "speed",           # an estimated speed for a track
    "crowd",           # a density measurement for a region
    "abandoned",       # a static object persisted with no owner nearby
    "proximity",       # two tracks stayed close for a sustained period
    "stream_gap",      # the worker lost and regained the stream; see below
    "scene_change",    # hard discontinuity, e.g. the sandbox feed looping
})

# ``stream_gap`` and ``scene_change`` are primitives rather than log lines because
# they are *evidence about the evidence*. A rule that fires "vehicle never exited"
# must know whether the camera was simply down for that interval, and a court asking
# why a vehicle appears to teleport deserves the answer "the demo feed looped here".
# Silently swallowing these is how an analytics platform produces confident nonsense.


class StreamClock:
    """Converts a stream's presentation timestamps into absolute UTC.

    A decoder gives PTS relative to an arbitrary stream origin. To place a frame on
    the wall clock you need one anchor: the wall-clock instant corresponding to some
    known PTS. Anchor once at connect, then every later frame is
    ``anchor_wall + (pts - anchor_pts)``.

    This is worth a class rather than an inline subtraction because of the
    discontinuity case. Each sandbox feed loops with a hard scene cut, and the PTS
    typically jumps backwards at the loop point. Treating that jump as a negative
    time delta would emit sightings dated before the stream opened. ``advance``
    detects it and re-anchors, reporting that it did so, which is what lets the
    worker emit a ``scene_change`` primitive instead of corrupt timestamps.
    """

    __slots__ = ("anchor_wall", "anchor_pts", "_last_pts", "discontinuities")

    #: A backwards jump smaller than this is treated as B-frame reordering, which is
    #: normal and must not re-anchor. Anything larger is a genuine discontinuity.
    REORDER_TOLERANCE_S = 0.5

    #: A forward jump larger than this is treated as a gap (the stream stalled or the
    #: worker fell behind) rather than as real elapsed scene time.
    FORWARD_GAP_S = 10.0

    def __init__(self, anchor_wall: datetime | None = None, anchor_pts: float = 0.0) -> None:
        self.anchor_wall = anchor_wall or datetime.now(timezone.utc)
        self.anchor_pts = float(anchor_pts)
        self._last_pts = float(anchor_pts)
        self.discontinuities = 0

    def advance(self, pts: float) -> tuple[datetime, bool]:
        """Map a PTS to UTC. Returns ``(timestamp, discontinuity_detected)``.

        The caller is expected to emit a ``scene_change`` primitive when the second
        element is true, and to reset any per-track state — track ids cannot survive
        a scene cut, because the objects behind them are gone.
        """
        pts = float(pts)
        delta = pts - self._last_pts

        if delta < -self.REORDER_TOLERANCE_S or delta > self.FORWARD_GAP_S:
            # Re-anchor to now. We have lost the ability to reconstruct true capture
            # time across the discontinuity, and pretending otherwise would be worse
            # than admitting it.
            self.anchor_wall = datetime.now(timezone.utc)
            self.anchor_pts = pts
            self._last_pts = pts
            self.discontinuities += 1
            return self.anchor_wall, True

        if pts > self._last_pts:
            self._last_pts = pts
        # Out-of-order-but-within-tolerance frames still get a correct timestamp;
        # they just do not move the high-water mark.
        return self.to_utc(pts), False

    def to_utc(self, pts: float) -> datetime:
        from datetime import timedelta

        return self.anchor_wall + timedelta(seconds=float(pts) - self.anchor_pts)


@dataclass(frozen=True, slots=True)
class BBox:
    """An axis-aligned box in normalised frame coordinates, origin top-left.

    Validated on construction because a box with ``x2 < x1`` produces a negative
    area, which silently poisons every IoU comparison the tracker makes rather than
    failing anywhere near the bug.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        for name in ("x1", "y1", "x2", "y2"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or math.isnan(v):
                raise ValueError(f"BBox.{name} must be a real number, got {v!r}")
            if not -0.05 <= v <= 1.05:
                # A small tolerance: detectors routinely return boxes a pixel or two
                # outside the frame for objects at the edge, and rejecting those
                # would discard exactly the objects that are entering or leaving.
                raise ValueError(f"BBox.{name}={v} is outside normalised range")
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError(f"BBox must have positive area, got {self!r}")

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def foot(self) -> tuple[float, float]:
        """Bottom-centre point: where the object meets the ground.

        This, not the centre, is what maps to a ground-plane position via homography.
        Using the centre makes a tall vehicle appear further away than a short one at
        the same distance, which corrupts speed estimates systematically rather than
        randomly.
        """
        return ((self.x1 + self.x2) / 2.0, self.y2)

    def iou(self, other: BBox) -> float:
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass(frozen=True, slots=True)
class Sighting:
    """One camera's observation of one object at one instant.

    This is the atom of the analytics layer. It asserts only what was seen, and
    carries the provenance needed to defend it later: which model produced it, at
    what confidence, from which frame.
    """

    camera_id: int
    ts: datetime
    track_id: str
    class_label: str
    confidence: float
    bbox: BBox

    #: Plate text is normalised (uppercase, no spaces or hyphens) or None. Storing
    #: the raw OCR output separately matters for evidence: the operator needs to see
    #: what the machine actually read, not only what it decided that meant.
    plate_text: str | None = None
    plate_text_raw: str | None = None
    plate_confidence: float | None = None

    speed_kmph: float | None = None

    #: Free-form, string-valued, and explicitly NOT a place for demographic
    #: inference. See the note at the bottom of this module.
    attributes: Mapping[str, str] = field(default_factory=dict)

    #: Provenance. ``frame_pts`` plus ``camera_id`` locates the source frame exactly,
    #: which is what an evidence export needs to re-extract the image.
    frame_pts: float | None = None
    detector: str | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            # A naive datetime here is a bug that surfaces days later as an
            # eight-hour offset in a cross-camera match. Refuse it at the boundary.
            raise ValueError("Sighting.ts must be timezone-aware UTC")
        if self.class_label not in OBJECT_CLASSES:
            raise ValueError(
                f"unknown class_label {self.class_label!r}; "
                f"add it to OBJECT_CLASSES if the detector legitimately emits it"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")

    @property
    def is_plated(self) -> bool:
        return self.class_label in PLATED_CLASSES


@dataclass(frozen=True, slots=True)
class PrimitiveEvent:
    """A discrete, incident-agnostic fact emitted by a worker.

    ``payload`` is intentionally untyped: each ``kind`` has its own shape, and
    enumerating them all here would couple the wire format to every rule that
    might ever read it. The kinds themselves are closed, so a typo fails fast
    while a new field does not require a schema migration.
    """

    kind: str
    camera_id: int
    ts: datetime
    track_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in PRIMITIVE_KINDS:
            raise ValueError(
                f"unknown primitive kind {self.kind!r}. Incidents are composed from "
                f"primitives by rules in the correlation layer, not added here."
            )
        if self.ts.tzinfo is None:
            raise ValueError("PrimitiveEvent.ts must be timezone-aware UTC")


# ---------------------------------------------------------------------------
# A deliberate omission
# ---------------------------------------------------------------------------
# There is no gender, age, caste, religion or ethnicity field anywhere in this
# module, and ``Sighting.attributes`` must not be used to smuggle one in.
#
# This is a design position, not an oversight. Appearance-based demographic
# classification on real CCTV — wide angle, low light, motion blur, partial
# occlusion, often 20+ metres out — is unreliable to a degree that makes it unfit
# to inform a police response, and its errors are not evenly distributed. A system
# that dispatches officers on such a signal would concentrate those errors on
# whoever the model handles worst.
#
# The crimes-against-women use case is served instead by *behavioural* primitives
# that are measurable from geometry alone and hold regardless of who is involved:
# ``proximity`` (two tracks sustaining close distance with correlated trajectories),
# ``dwell``, and time-of-day plus location context. "A lone person followed
# persistently down a street at 23:40" is both more accurate and more defensible
# than any guess about that person's demographics, and it is what an operator can
# actually act on.
#
# Under the DPDP Act 2023 this also keeps processing tied to a stated purpose, which
# ``app.audit_log.purpose`` and ``case_reference`` already record per access.
