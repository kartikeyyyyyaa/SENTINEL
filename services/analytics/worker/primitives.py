"""The event vocabulary.

This module is the contract between the analytics worker and the incident rule
engine, and it is deliberately small. Every primitive here is something a
pretrained model or a few lines of geometry can produce from one camera's frames,
with no knowledge of what an "incident" is.

**New incident types are authored as rules over these primitives, not as new
models.** That is the whole extensibility argument. "Vehicle stopped on a
flyover" is a rule over ``Track`` plus ``ZoneDwell``; "crowd forming outside a
temple" is a rule over ``CrowdDensity``; "wrong-way driving" is a rule over
``Trajectory`` direction against a per-camera expected heading; "no-entry
violation" is ``Trajectory`` crossing a registry-defined line. None of those need
a trained model, a labelled dataset, or a change to this file. Adding a genuinely
new *sensing* capability (say, smoke) needs a new model; adding a new *incident*
does not. The rule engine owns that logic and lives elsewhere — nothing in this
module may know what an incident is.

**Timestamps are always PTS-derived.** Every ``t`` in this file is seconds on the
capture module's monotonic stream timeline, not wall clock. The gateway replays a
buffered GOP on connect, so arrival time runs faster than real time for the first
second or two of every connection; anything that computed a speed or a dwell from
arrival time would produce garbage exactly at join. ``PrimitiveEvent`` carries a
wall-clock field too, but only as provenance for the audit trail — never as a
measurement input.

**Frozen and slotted.** These objects cross a thread boundary into the sink and
get retried after a failed POST. An event that could be mutated between emission
and delivery is an event whose JSON does not match what the rule engine reasoned
over.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Primitive kinds, as they appear on the wire. The rule engine matches on these
# strings, so they are API: append, never rename.
KIND_DETECTION = "detection"
KIND_PLATE_READ = "plate_read"
KIND_TRACK = "track"
KIND_TRAJECTORY = "trajectory"
KIND_ZONE_DWELL = "zone_dwell"
KIND_CROWD_DENSITY = "crowd_density"
KIND_SPEED_ESTIMATE = "speed_estimate"


@dataclass(frozen=True, slots=True)
class Box:
    """Axis-aligned box in source-frame pixel coordinates.

    Source-frame, not model-input: the streams mix resolutions and codecs, so
    every frame is letterboxed to the model's square input individually and the
    detector maps its boxes back before constructing one of these. Downstream
    consumers must never have to know what input size the model happened to use.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def foot(self) -> tuple[float, float]:
        """Bottom-centre point: where the object meets the ground.

        Tracking and speed use this rather than the box centre. The centre moves
        vertically as a vehicle's apparent height changes with distance, which
        adds a spurious velocity component; the ground contact point does not.
        """
        return ((self.x1 + self.x2) / 2.0, self.y2)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height > 0 else 0.0

    def iou(self, other: Box) -> float:
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0.0:
            return 0.0
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def clip(self, width: float, height: float) -> Box:
        return Box(
            x1=min(max(0.0, self.x1), width),
            y1=min(max(0.0, self.y1), height),
            x2=min(max(0.0, self.x2), width),
            y2=min(max(0.0, self.y2), height),
        )

    def as_int(self) -> tuple[int, int, int, int]:
        return int(round(self.x1)), int(round(self.y1)), int(round(self.x2)), int(round(self.y2))


@dataclass(frozen=True, slots=True)
class Detection:
    """One object in one frame. Stage 2's only output.

    ``confidence`` is retained verbatim rather than thresholded away, because the
    tracker treats weak detections differently from strong ones: too weak to
    start a track, strong enough to continue one through an occlusion. Discarding
    the number here would throw away the information ByteTrack exists to use.
    """

    box: Box
    label: str
    confidence: float
    t: float  # Stream-timeline seconds, PTS-derived.
    class_id: int = -1

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": [self.box.x1, self.box.y1, self.box.x2, self.box.y2],
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "t": round(self.t, 4),
            "class_id": self.class_id,
        }


@dataclass(frozen=True, slots=True)
class PlateCrop:
    """Stage 3's output: a region worth paying stage 4 for.

    Carries no pixels. The crop array travels alongside as a separate argument
    rather than inside a primitive, because primitives get serialised, retried and
    buffered, and a buffered event holding a numpy view keeps the whole decoded
    frame alive. That is how a bounded event buffer turns into an unbounded memory
    leak.
    """

    box: Box  # Plate region, source-frame coordinates.
    vehicle_box: Box
    t: float
    track_id: int | None = None
    score: float = 0.0  # Geometric plausibility, not a model confidence.

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": [self.box.x1, self.box.y1, self.box.x2, self.box.y2],
            "vehicle_box": [
                self.vehicle_box.x1,
                self.vehicle_box.y1,
                self.vehicle_box.x2,
                self.vehicle_box.y2,
            ],
            "t": round(self.t, 4),
            "track_id": self.track_id,
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class PlateRead:
    """A plate string with the confidence that produced it.

    Confidence is mandatory and unrounded. An ANPR platform that emits plate
    strings without confidence forces every downstream consumer to treat a 0.41
    guess and a 0.97 read identically, and the first time that matters is when
    somebody is stopped on the strength of a hallucinated character.

    ``text_raw`` is what the engine returned; ``text`` is that string uppercased
    and stripped of separators. Normalisation never *invents* characters — no
    O/0 or I/1 substitution to force a match against the expected format. If the
    read does not match the format, ``format_valid`` is False and the rule engine
    decides what to do about it.
    """

    text: str
    text_raw: str
    confidence: float
    t: float
    box: Box
    format_valid: bool = False
    track_id: int | None = None
    engine: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "text_raw": self.text_raw,
            "confidence": round(self.confidence, 4),
            "t": round(self.t, 4),
            "box": [self.box.x1, self.box.y1, self.box.x2, self.box.y2],
            "format_valid": self.format_valid,
            "track_id": self.track_id,
            "engine": self.engine,
        }


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One timestamped observation on a track.

    ``t`` is PTS-derived and intervals between consecutive points are *not*
    uniform. Every consumer must read the timestamps rather than assume a frame
    rate; that is why the timestamp is stored per point instead of a start time
    plus an fps.
    """

    t: float
    x: float
    y: float
    box: Box
    confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"t": round(self.t, 4), "x": round(self.x, 2), "y": round(self.y, 2)}


@dataclass(frozen=True, slots=True)
class Track:
    """One object followed across frames within a single segment.

    ``segment_id`` is part of a track's identity, not metadata. Sandbox feeds loop
    with a hard scene cut, and a track cannot span the cut — the vehicle on either
    side is a different vehicle. Tracks are therefore reset at the discontinuity
    and a new segment's ids start fresh. Interpolating across the cut would
    manufacture a vehicle that teleported across the frame at an impossible speed,
    which is exactly the kind of artefact a speeding rule would faithfully report.
    """

    track_id: int
    label: str
    segment_id: int
    points: tuple[TrackPoint, ...]
    hits: int = 0
    age_seconds: float = 0.0
    confirmed: bool = False
    lost: bool = False

    @property
    def first_seen(self) -> float:
        return self.points[0].t if self.points else 0.0

    @property
    def last_seen(self) -> float:
        return self.points[-1].t if self.points else 0.0

    @property
    def duration(self) -> float:
        return self.last_seen - self.first_seen

    @property
    def latest_box(self) -> Box | None:
        return self.points[-1].box if self.points else None

    def displacement(self) -> float:
        if len(self.points) < 2:
            return 0.0
        a, b = self.points[0], self.points[-1]
        return math.hypot(b.x - a.x, b.y - a.y)

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "label": self.label,
            "segment_id": self.segment_id,
            "first_seen": round(self.first_seen, 4),
            "last_seen": round(self.last_seen, 4),
            "duration": round(self.duration, 4),
            "hits": self.hits,
            "confirmed": self.confirmed,
            "lost": self.lost,
            "points": [p.as_dict() for p in self.points],
        }


@dataclass(frozen=True, slots=True)
class Trajectory:
    """A track's path reduced to what rules actually ask about.

    Rules want "which way was it going", "did it cross this line", "was it moving
    at all" — not a hundred raw points. Precomputing the summary here means the
    rule engine never has to re-derive geometry, and every rule derives it the
    same way.
    """

    track_id: int
    label: str
    segment_id: int
    start: tuple[float, float]
    end: tuple[float, float]
    t_start: float
    t_end: float
    path_length_px: float
    net_displacement_px: float
    heading_degrees: float  # 0 = +x (right), 90 = +y (down, i.e. towards the camera).
    is_stationary: bool
    point_count: int

    @property
    def straightness(self) -> float:
        """Net displacement over path length: 1.0 is a straight line.

        A U-turn scores near zero even though it covers a lot of ground, which is
        how a no-U-turn rule tells a turn from a lane change without any model.
        """
        return self.net_displacement_px / self.path_length_px if self.path_length_px > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["straightness"] = round(self.straightness, 4)
        return d


@dataclass(frozen=True, slots=True)
class ZoneDwell:
    """How long a track stayed inside a named polygon.

    Zones are configured per camera in the registry, never inferred. "Parked in a
    no-parking zone" and "loitering near a substation" are the same primitive with
    different zone definitions and different thresholds — again, a rule, not a
    model.

    ``still`` distinguishes "present in the zone" from "not moving in the zone",
    because a busy junction is permanently occupied and only the stationary case
    is an incident.
    """

    track_id: int
    label: str
    segment_id: int
    zone_id: str
    entered_t: float
    last_seen_t: float
    dwell_seconds: float
    still: bool
    max_movement_px: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CrowdDensity:
    """Person count over a region at one instant.

    A count, not a headcount claim. YOLOv8n undercounts a dense crowd badly, so
    this is emitted with the region area and the count, letting a rule threshold
    on *change* in density — which is robust to a constant undercount — rather
    than on an absolute number, which is not.
    """

    segment_id: int
    t: float
    person_count: int
    region_id: str = "frame"
    region_area_px: float = 0.0
    mean_confidence: float = 0.0

    @property
    def per_megapixel(self) -> float:
        return self.person_count / (self.region_area_px / 1e6) if self.region_area_px > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["per_megapixel"] = round(self.per_megapixel, 3)
        return d


@dataclass(frozen=True, slots=True)
class SpeedEstimate:
    """Speed along a track, in pixels per second and optionally in km/h.

    Two fields for a reason. Pixels per second is measured; km/h requires a
    per-camera ``meters_per_pixel`` that somebody calibrated, and where that
    calibration is absent ``kmph`` is None rather than a plausible-looking
    invention. An uncalibrated camera can still support "much faster than the
    other vehicles in this frame", which is a rule over the pixel figure.

    ``dt_seconds`` is carried so a consumer can judge the estimate: the same
    displacement over 0.06 s and over 2.0 s are not equally trustworthy, and after
    the adaptive sampler widens the stride the second case is common.
    """

    track_id: int
    label: str
    segment_id: int
    t: float
    pixels_per_second: float
    dt_seconds: float
    sample_count: int
    kmph: float | None = None
    meters_per_pixel: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pixels_per_second"] = round(self.pixels_per_second, 3)
        if self.kmph is not None:
            d["kmph"] = round(self.kmph, 2)
        return d


@dataclass(frozen=True, slots=True)
class PrimitiveEvent:
    """The envelope every primitive leaves the edge in.

    ``t`` is the PTS-derived stream timeline; ``segment_id`` says which
    uninterrupted run of stream it belongs to. Both are required for correlation:
    two events with the same ``t`` and different ``segment_id`` are from different
    passes of a looping feed and must not be reasoned about as simultaneous.

    ``wall_time`` is Unix seconds at emission. It exists so an operator can find
    the event in a log and so the audit trail has something monotonic in real
    time. It is never an input to a measurement — see the module docstring.
    """

    camera_id: str
    kind: str
    t: float
    segment_id: int
    payload: dict[str, Any]
    wall_time: float = 0.0
    worker_id: str = ""
    schema_version: int = SCHEMA_VERSION
    # Base64 JPEG, small, optional. Thumbnails and short alert clips are the only
    # pixels that ever leave the edge. Continuous video does not, at any setting.
    thumbnail_jpeg_b64: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "camera_id": self.camera_id,
            "kind": self.kind,
            "t": round(self.t, 4),
            "segment_id": self.segment_id,
            "wall_time": round(self.wall_time, 3),
            "worker_id": self.worker_id,
            "payload": self.payload,
        }
        if self.tags:
            d["tags"] = list(self.tags)
        if self.thumbnail_jpeg_b64:
            d["thumbnail_jpeg_b64"] = self.thumbnail_jpeg_b64
        return d

    def to_json(self) -> str:
        # separators without spaces: at 50 cameras the sink is shipping a lot of
        # these and the whitespace is measurable bandwidth for zero benefit.
        return json.dumps(self.as_dict(), separators=(",", ":"), ensure_ascii=False)


def make_event(
    camera_id: str,
    kind: str,
    t: float,
    segment_id: int,
    payload: dict[str, Any],
    *,
    wall_time: float = 0.0,
    worker_id: str = "",
    thumbnail_jpeg_b64: str | None = None,
    tags: tuple[str, ...] = (),
) -> PrimitiveEvent:
    """Construct an envelope. Kept as a function so callers do not have to
    remember which fields are keyword-only."""
    return PrimitiveEvent(
        camera_id=camera_id,
        kind=kind,
        t=t,
        segment_id=segment_id,
        payload=payload,
        wall_time=wall_time,
        worker_id=worker_id,
        thumbnail_jpeg_b64=thumbnail_jpeg_b64,
        tags=tags,
    )


def trajectory_from_track(track: Track, stationary_threshold_px: float = 12.0) -> Trajectory:
    """Reduce a track to its trajectory summary.

    Path length is summed over consecutive points rather than taken as
    start-to-end distance, because the two differ exactly where it matters — a
    vehicle that reversed, turned, or circled has a long path and a short
    displacement.
    """
    if not track.points:
        return Trajectory(
            track_id=track.track_id,
            label=track.label,
            segment_id=track.segment_id,
            start=(0.0, 0.0),
            end=(0.0, 0.0),
            t_start=0.0,
            t_end=0.0,
            path_length_px=0.0,
            net_displacement_px=0.0,
            heading_degrees=0.0,
            is_stationary=True,
            point_count=0,
        )
    pts = track.points
    path = 0.0
    for a, b in zip(pts, pts[1:]):
        path += math.hypot(b.x - a.x, b.y - a.y)
    dx, dy = pts[-1].x - pts[0].x, pts[-1].y - pts[0].y
    net = math.hypot(dx, dy)
    heading = math.degrees(math.atan2(dy, dx)) % 360.0
    return Trajectory(
        track_id=track.track_id,
        label=track.label,
        segment_id=track.segment_id,
        start=(pts[0].x, pts[0].y),
        end=(pts[-1].x, pts[-1].y),
        t_start=pts[0].t,
        t_end=pts[-1].t,
        path_length_px=path,
        net_displacement_px=net,
        heading_degrees=heading,
        is_stationary=net < stationary_threshold_px,
        point_count=len(pts),
    )
