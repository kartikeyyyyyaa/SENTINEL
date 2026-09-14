"""Primitives: pure geometry over tracks. No model runs in this file.

This is where the extensibility argument becomes concrete. Every incident type
Sentinel supports is a rule over these primitives, authored as data in the
correlation layer — not a new model, not a new dataset, not a redeploy of this
worker. "Loitering near a substation after 22:00" and "parked in a no-parking zone"
are the same ``dwell`` primitive with different polygons and different thresholds.
"Wrong-way driving" is a ``line_cross`` whose direction sign disagrees with the
carriageway direction the registry holds. That is why these are geometry and not
classifiers: geometry is auditable, tunable by an operator, and its failure modes
are explainable in a sentence.

Everything here consumes ``Track`` objects and emits ``PrimitiveEvent`` objects. It
touches no frames, no models and no network.

**On ``proximity``.** Read the "deliberate omission" note at the bottom of
``services/common/events.py`` first. There is no gender, age, caste, religion or
ethnicity attribute anywhere in Sentinel, and ``Sighting.attributes`` must not be
used to smuggle one in. ``proximity`` is the substitute, and it is a better signal
rather than a politically safer one: "one track followed another at under three
metres for ninety seconds with correlated heading, at 23:40, on a street with no
other tracks present" is measurable from geometry, holds regardless of who is
involved, is defensible in front of a court, and is something an operator can act on.
An appearance-based demographic guess on a wide-angle 20-metre CCTV view is none of
those things, and its errors are not evenly distributed across the population it
would be used on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from services.common.events import PrimitiveEvent

from .config import CameraConfig, CrossingLine, Homography, PrimitiveConfig, Zone
from .tracker import Track

#: Tolerance for "the point is on the polygon edge", in normalised units. At a work
#: resolution of 1920 px this is a fifth of a pixel, so it captures genuine
#: on-the-boundary cases without widening the zone perceptibly.
_EDGE_EPS = 1e-9

#: Classes eligible for the abandoned-object primitive. Note what is missing: there
#: is no ``bag``, ``suitcase`` or ``backpack`` class in ``OBJECT_CLASSES``, because
#: the detector Sentinel ships does not reliably produce one at CCTV distances. So
#: this primitive detects abandoned *vehicles* and static unclassified objects, which
#: is genuinely useful — an unattended vehicle outside a public building is the
#: canonical case — and it does not detect left luggage. Claiming otherwise would be
#: claiming a capability that is not there.
_ABANDONABLE = frozenset({"car", "motorcycle", "bicycle", "truck", "bus", "auto_rickshaw", "unknown"})


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def point_in_polygon(
    point: tuple[float, float], polygon: Sequence[tuple[float, float]]
) -> bool:
    """Ray casting, with points exactly on an edge treated as inside.

    Ray casting rather than a convex test, because operator-drawn zones are routinely
    concave — a zone following a kerb around a corner, a platform edge that wraps a
    pillar — and a convex-hull shortcut would silently include the road. The cost is
    O(vertices), which on the six-to-twelve-vertex polygons an operator actually draws
    is nothing.

    The on-edge case is handled explicitly and inclusively, before the ray cast,
    because ray casting is genuinely ambiguous there: whether a boundary point counts
    depends on floating-point rounding of the crossing test. A track whose foot point
    sits on a zone boundary — a person standing at the kerb, a car stopped on the line
    — would then flap in and out with every frame's sub-pixel jitter, emitting a
    stream of ``zone_enter``/``zone_exit`` pairs. Inclusive-and-deterministic is not
    obviously the *right* convention, but it is a convention, and having one is what
    stops the flapping.
    """
    if len(polygon) < 3:
        return False
    x, y = point

    for i in range(len(polygon)):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % len(polygon)]
        if _on_segment(x, y, ax, ay, bx, by):
            return True

    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Half-open comparison on y: a vertex at exactly the ray's height is counted
        # by one of its two edges and not both. Without the asymmetry, a ray passing
        # through a vertex crosses twice and the parity flips back, which puts points
        # horizontally level with any vertex on the wrong side.
        if (yi > y) != (yj > y):
            t = (y - yi) / (yj - yi)
            if x < xi + t * (xj - xi):
                inside = not inside
        j = i
    return inside


def _on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > _EDGE_EPS:
        return False
    # Collinear; now check it lies between the endpoints rather than on the infinite
    # line through them.
    return (
        min(ax, bx) - _EDGE_EPS <= px <= max(ax, bx) + _EDGE_EPS
        and min(ay, by) - _EDGE_EPS <= py <= max(ay, by) + _EDGE_EPS
    )


def side_of_line(
    a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]
) -> float:
    """Signed 2-D cross product: which side of directed line ``a -> b`` is ``p`` on?

    Positive is left of the direction of travel from ``a`` to ``b``, negative is
    right, zero is on the line. The *sign* is the whole reason line crossing gives
    wrong-way detection for free: an unsigned "something crossed" is a fact no rule
    can act on.

    Image coordinates have y increasing downwards, so "left" here is left as seen on
    screen when travelling from ``a`` towards ``b``. The convention only has to be
    consistent, and naming the two directions is the operator's job — see
    ``CrossingLine.positive_name``.
    """
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def segments_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> bool:
    """True when segment ``p1p2`` crosses segment ``q1q2``.

    Needed in addition to the sign change: a track moving parallel to a crossing line
    but well beyond its end will change sign relative to the *infinite* line through
    it without ever crossing the segment the operator drew. Checking only the sign is
    the classic bug that makes a counting line count traffic on the next road over.
    """
    d1 = side_of_line(q1, q2, p1)
    d2 = side_of_line(q1, q2, p2)
    d3 = side_of_line(p1, p2, q1)
    d4 = side_of_line(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # Touching counts. A track whose sampled position lands exactly on the line is
    # crossing it; requiring a strict straddle would miss it entirely at low frame
    # rates, which is precisely when the sample is most likely to land there.
    return any(
        abs(d) <= _EDGE_EPS and _on_segment(pt[0], pt[1], s1[0], s1[1], s2[0], s2[1])
        for d, pt, s1, s2 in (
            (d1, p1, q1, q2),
            (d2, p2, q1, q2),
            (d3, q1, p1, p2),
            (d4, q2, p1, p2),
        )
    )


def project_to_ground(
    point: tuple[float, float], homography: Homography
) -> tuple[float, float] | None:
    """Map a normalised image point to ground-plane metres.

    Returns ``None`` when the point projects to or behind the horizon, where the
    homography's denominator goes to zero and the mapped position runs to infinity.
    Silently returning a huge number instead is how a vehicle near the vanishing point
    acquires a speed of 40,000 km/h.
    """
    m = homography.matrix
    x, y = point
    denom = m[2][0] * x + m[2][1] * y + m[2][2]
    if abs(denom) < 1e-9:
        return None
    gx = (m[0][0] * x + m[0][1] * y + m[0][2]) / denom
    gy = (m[1][0] * x + m[1][1] * y + m[1][2]) / denom
    if not (math.isfinite(gx) and math.isfinite(gy)):
        return None
    return gx, gy


# ---------------------------------------------------------------------------
# Per-track state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ZoneState:
    entered_t: float
    entered_ts: datetime
    entry_point: tuple[float, float]
    last_dwell_t: float = float("-inf")
    max_displacement: float = 0.0


@dataclass(slots=True)
class _PairState:
    first_close_t: float
    last_close_t: float
    last_emitted_t: float = float("-inf")
    samples: int = 0
    correlation_sum: float = 0.0

    @property
    def mean_correlation(self) -> float:
        return self.correlation_sum / self.samples if self.samples else 0.0


@dataclass(slots=True)
class SpeedResult:
    """A speed estimate with its uncertainty attached. Both, always."""

    kmph: float
    error_kmph: float
    seconds: float
    metres: float
    samples: int


class SpeedEstimator:
    """Ground-plane speed from a track's foot points and a camera homography.

    **Read the error bar.** This is a four-point manual homography on a wide-angle
    CCTV view, and it is not a calibrated traffic sensor. The dominant error sources,
    in rough order of size:

    * *Calibration residual.* ``Homography.rms_error_m`` is the fit's own residual,
      typically 0.3-1.5 m on a manual calibration against features an operator could
      identify in both the image and a map. It applies to each endpoint of the
      displacement independently.
    * *Foot-point error.* The bounding box's bottom edge is taken as the point where
      the object meets the ground. It is not, quite: it is wherever the detector put
      the box edge, which on a partially occluded or motion-blurred vehicle can be
      half a metre out, and which shifts systematically as the vehicle's aspect
      changes through a turn.
    * *Perspective compression.* Near the horizon a pixel is metres. A vehicle
      100 m out has its position quantised so coarsely that its speed is barely
      constrained by the measurement at all.

    So ``error_kmph`` is reported on every estimate, and it is not decoration.
    Thresholding at 61 km/h in a 60 zone is not a distinction this measurement can
    support; "travelling at roughly twice the posted limit" is. The rule engine is
    expected to compare against ``kmph - error_kmph`` when the consequence of a false
    positive is an enforcement action, and this is stated on the ``speed`` primitive's
    payload so a rule author cannot claim not to have known.
    """

    __slots__ = ("_homography", "_config")

    def __init__(self, homography: Homography | None, config: PrimitiveConfig) -> None:
        self._homography = homography
        self._config = config

    @property
    def available(self) -> bool:
        return self._homography is not None

    def estimate(self, track: Track, t: float) -> SpeedResult | None:
        """Speed over the last ``speed_window_seconds`` of the track's history.

        Net displacement between the window's endpoints, not the summed path length.
        Summing consecutive steps accumulates the per-frame jitter of the box
        regressor, which is roughly unbiased in direction but strictly positive in
        magnitude — so a *stationary* vehicle acquires a speed proportional to the
        frame rate. That is a spectacular failure mode: it reports parked cars
        speeding, and it gets worse the better your camera is.
        """
        homography = self._homography
        if homography is None or len(track.history) < self._config.speed_min_samples:
            return None

        window = self._config.speed_window_seconds
        points = [p for p in track.history if (t - p.t) <= window]
        if len(points) < self._config.speed_min_samples:
            return None

        first, last = points[0], points[-1]
        dt = last.t - first.t
        if dt < self._config.speed_min_dt_seconds:
            # Not enough elapsed time to divide by. "Not enough evidence" must not be
            # reported as "very fast", which is exactly what a small denominator does.
            return None

        a = project_to_ground(first.foot, homography)
        b = project_to_ground(last.foot, homography)
        if a is None or b is None:
            return None
        metres = math.hypot(b[0] - a[0], b[1] - a[1])
        kmph = (metres / dt) * 3.6

        # Two independent endpoint errors, each the calibration residual, combined in
        # quadrature. This is a floor, not a full budget: it ignores foot-point error
        # and perspective compression, both of which are larger at distance.
        position_error_m = homography.rms_error_m * math.sqrt(2.0)
        error_kmph = (position_error_m / dt) * 3.6
        return SpeedResult(
            kmph=kmph,
            error_kmph=error_kmph,
            seconds=dt,
            metres=metres,
            samples=len(points),
        )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class PrimitiveEngine:
    """All geometric primitives for one camera. One instance per camera.

    Called once per analysed frame with the currently live confirmed tracks. Holds
    per-track and per-pair state, all of which is cleared by ``reset`` on a scene
    change — a zone occupancy or a proximity streak measured across a feed loop is
    two different scenes' geometry added together.
    """

    __slots__ = (
        "camera_id",
        "_config",
        "_zones",
        "_lines",
        "_crowd_regions",
        "_homography",
        "_speed",
        "_zone_state",
        "_line_side",
        "_pairs",
        "_last_crowd_t",
        "_last_speed_t",
        "_abandoned_emitted",
    )

    def __init__(self, camera: CameraConfig, config: PrimitiveConfig | None = None) -> None:
        self.camera_id = camera.camera_id
        self._config = config or PrimitiveConfig()
        self._zones: tuple[Zone, ...] = camera.zones
        self._lines: tuple[CrossingLine, ...] = camera.lines
        self._crowd_regions: tuple[Zone, ...] = camera.crowd_regions
        self._homography = camera.homography
        self._speed = SpeedEstimator(camera.homography, self._config)
        self._zone_state: dict[tuple[str, str], _ZoneState] = {}
        self._line_side: dict[tuple[str, str], float] = {}
        self._pairs: dict[tuple[str, str], _PairState] = {}
        self._last_crowd_t = float("-inf")
        self._last_speed_t: dict[str, float] = {}
        self._abandoned_emitted: set[str] = set()

    def reset(self) -> None:
        self._zone_state.clear()
        self._line_side.clear()
        self._pairs.clear()
        self._last_crowd_t = float("-inf")
        self._last_speed_t.clear()
        self._abandoned_emitted.clear()

    def update(
        self, tracks: Sequence[Track], t: float, ts: datetime
    ) -> list[PrimitiveEvent]:
        events: list[PrimitiveEvent] = []
        live = {track.track_id for track in tracks}
        self._forget_dead(live)

        for track in tracks:
            events.extend(self._zones_for(track, t, ts))
            events.extend(self._lines_for(track, ts))
            events.extend(self._speed_for(track, t, ts))
            events.extend(self._abandoned_for(track, tracks, t, ts))

        events.extend(self._crowd(tracks, t, ts))
        events.extend(self._proximity(tracks, t, ts))
        return events

    # -- zones -------------------------------------------------------------

    def _zones_for(self, track: Track, t: float, ts: datetime) -> list[PrimitiveEvent]:
        events: list[PrimitiveEvent] = []
        # Foot point, not centre: a zone is a region of *ground*, and a tall vehicle's
        # box centre is metres above the ground it is standing on. Using the centre
        # makes a bus appear to enter a zone before it does and leave after it has.
        point = track.bbox.foot
        for zone in self._zones:
            key = (track.track_id, zone.zone_id)
            inside = point_in_polygon(point, zone.polygon)
            state = self._zone_state.get(key)

            if inside and state is None:
                self._zone_state[key] = _ZoneState(
                    entered_t=t, entered_ts=ts, entry_point=point
                )
                events.append(
                    self._event(
                        "zone_enter",
                        ts,
                        track.track_id,
                        {"zone_id": zone.zone_id, "class_label": track.class_label},
                    )
                )
                continue
            if not inside and state is not None:
                del self._zone_state[key]
                events.append(
                    self._event(
                        "zone_exit",
                        ts,
                        track.track_id,
                        {
                            "zone_id": zone.zone_id,
                            "class_label": track.class_label,
                            "duration_seconds": round(t - state.entered_t, 3),
                        },
                    )
                )
                continue
            if not inside or state is None:
                continue

            displacement = math.hypot(
                point[0] - state.entry_point[0], point[1] - state.entry_point[1]
            )
            state.max_displacement = max(state.max_displacement, displacement)
            threshold = zone.dwell_seconds or self._config.dwell_seconds
            elapsed = t - state.entered_t
            if elapsed < threshold:
                continue
            if (t - state.last_dwell_t) < self._config.dwell_repeat_seconds:
                continue
            state.last_dwell_t = t
            events.append(
                self._event(
                    "dwell",
                    ts,
                    track.track_id,
                    {
                        "zone_id": zone.zone_id,
                        "class_label": track.class_label,
                        "dwell_seconds": round(elapsed, 2),
                        # ``still`` separates "occupied" from "stationary". A busy
                        # junction is permanently occupied and that is not an
                        # incident; a vehicle that has not moved in a bus lane for
                        # four minutes is. They are different facts and a rule needs
                        # both available rather than one inferred from the other.
                        "still": state.max_displacement < self._config.still_movement,
                        "max_displacement": round(state.max_displacement, 4),
                    },
                )
            )
        return events

    # -- lines -------------------------------------------------------------

    def _lines_for(self, track: Track, ts: datetime) -> list[PrimitiveEvent]:
        events: list[PrimitiveEvent] = []
        if len(track.history) < 2:
            return events
        previous = track.history[-2].foot
        current = track.history[-1].foot

        for line in self._lines:
            key = (track.track_id, line.line_id)
            side_now = side_of_line(line.a, line.b, current)
            side_before = self._line_side.get(key)
            self._line_side[key] = side_now
            if side_before is None or side_before == 0.0 or side_now == 0.0:
                continue
            if (side_before > 0) == (side_now > 0):
                continue
            if not segments_intersect(previous, current, line.a, line.b):
                # Sign flipped relative to the infinite line but the track never
                # touched the segment the operator drew. Emitting here is the bug that
                # makes a counting line count the next road over.
                continue
            direction = 1 if side_now > 0 else -1
            events.append(
                self._event(
                    "line_cross",
                    ts,
                    track.track_id,
                    {
                        "line_id": line.line_id,
                        "class_label": track.class_label,
                        "direction": direction,
                        "direction_name": (
                            line.positive_name if direction > 0 else line.negative_name
                        ),
                        # Where on the line it crossed, as a fraction from ``a`` to
                        # ``b``. A lane-level rule needs it: on a four-lane
                        # carriageway one line spans all four, and "crossed at 0.85"
                        # is the outside lane.
                        "position": round(_fraction_along(line, current), 4),
                    },
                )
            )
        return events

    # -- speed -------------------------------------------------------------

    def _speed_for(self, track: Track, t: float, ts: datetime) -> list[PrimitiveEvent]:
        if not self._speed.available:
            return []
        last = self._last_speed_t.get(track.track_id, float("-inf"))
        if (t - last) < self._config.speed_window_seconds:
            return []
        result = self._speed.estimate(track, t)
        if result is None:
            return []
        self._last_speed_t[track.track_id] = t
        return [
            self._event(
                "speed",
                ts,
                track.track_id,
                {
                    "class_label": track.class_label,
                    "kmph": round(result.kmph, 2),
                    "error_kmph": round(result.error_kmph, 2),
                    # Spelled out on the payload so a rule author cannot claim not to
                    # have known. See SpeedEstimator's docstring for the full list of
                    # error sources this bound does *not* include.
                    "lower_bound_kmph": round(max(0.0, result.kmph - result.error_kmph), 2),
                    "window_seconds": round(result.seconds, 3),
                    "metres": round(result.metres, 2),
                    "samples": result.samples,
                    "method": "ground_plane_homography",
                },
            )
        ]

    # -- crowd -------------------------------------------------------------

    def _crowd(
        self, tracks: Sequence[Track], t: float, ts: datetime
    ) -> list[PrimitiveEvent]:
        if not self._crowd_regions:
            return []
        if (t - self._last_crowd_t) < self._config.crowd_seconds:
            return []
        self._last_crowd_t = t
        events: list[PrimitiveEvent] = []
        for region in self._crowd_regions:
            people = 0
            total = 0
            for track in tracks:
                if not point_in_polygon(track.bbox.foot, region.polygon):
                    continue
                total += 1
                if track.class_label == "person":
                    people += 1
            events.append(
                self._event(
                    "crowd",
                    ts,
                    None,
                    {
                        "zone_id": region.zone_id,
                        # A count of *tracks*, and named as such. It is not a count of
                        # people: at any real density the detector misses occluded
                        # individuals and the tracker merges adjacent ones, so this
                        # under-reports, increasingly so as the density rises. A
                        # crowd-counting model (density regression) is the right tool
                        # for a genuine headcount and is not what this is.
                        "person_tracks": people,
                        "all_tracks": total,
                        "method": "track_count_in_polygon",
                    },
                )
            )
        return events

    # -- abandoned ---------------------------------------------------------

    def _abandoned_for(
        self, track: Track, tracks: Sequence[Track], t: float, ts: datetime
    ) -> list[PrimitiveEvent]:
        cfg = self._config
        if track.class_label not in _ABANDONABLE:
            return []
        if track.track_id in self._abandoned_emitted:
            # Once per track. The object is going to keep not moving, and a repeat
            # every frame for the next hour is not additional information.
            return []
        if (t - track.first_t) < cfg.abandoned_seconds:
            return []
        if len(track.history) < 2:
            return []

        window = [p for p in track.history if (t - p.t) <= cfg.abandoned_seconds]
        if len(window) < 2:
            return []
        xs = [p.foot[0] for p in window]
        ys = [p.foot[1] for p in window]
        movement = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        if movement > cfg.abandoned_movement:
            return []

        owner = _nearest_person(track, tracks, cfg.abandoned_owner_distance)
        if owner is not None:
            # Somebody is standing next to it. An attended object is not abandoned,
            # and this single check removes the overwhelming majority of what would
            # otherwise be false positives: parked vehicles with their driver present,
            # street vendors' carts, a bag beside its owner on a platform.
            return []

        self._abandoned_emitted.add(track.track_id)
        return [
            self._event(
                "abandoned",
                ts,
                track.track_id,
                {
                    "class_label": track.class_label,
                    "static_seconds": round(t - track.first_t, 1),
                    "movement": round(movement, 5),
                    "bbox": [round(v, 5) for v in track.bbox.as_tuple()],
                },
            )
        ]

    # -- proximity ---------------------------------------------------------

    def _proximity(
        self, tracks: Sequence[Track], t: float, ts: datetime
    ) -> list[PrimitiveEvent]:
        """Two tracks sustaining close distance with correlated trajectories.

        This is the behaviour-based substitute for demographic inference described at
        the bottom of ``services/common/events.py``, and the three conditions are all
        necessary:

        * *Close* — two people in a crowd are close by accident constantly.
        * *Sustained* — over ``proximity_seconds``, so a passing encounter does not
          qualify.
        * *Correlated heading* — they are moving the same way. Two strangers passing
          in opposite directions are close and uncorrelated; somebody following
          somebody is close and correlated. This is the condition that does the real
          work, and it is why the primitive is not simply a distance threshold.

        What it deliberately does not do is judge. It emits "these two tracks moved
        together for this long at this distance" and stops. Whether that is a person
        being followed, two friends walking home, or a parent and child is a question
        for a rule with context — time of day, location, whether either track is
        otherwise alone — and for an operator looking at the video. Encoding the
        judgement here would bury it where nobody can tune or audit it.
        """
        cfg = self._config
        if len(tracks) < 2:
            self._pairs.clear()
            return []

        events: list[PrimitiveEvent] = []
        seen: set[tuple[str, str]] = set()
        for i in range(len(tracks)):
            for j in range(i + 1, len(tracks)):
                a, b = tracks[i], tracks[j]
                key = _pair_key(a.track_id, b.track_id)
                distance, in_metres = self._pair_distance(a, b)
                if distance is None:
                    continue
                limit = cfg.proximity_distance_m if in_metres else cfg.proximity_distance
                if distance > limit:
                    self._pairs.pop(key, None)
                    continue

                correlation = _heading_correlation(a, b)
                state = self._pairs.get(key)
                if state is None:
                    state = _PairState(first_close_t=t, last_close_t=t)
                    self._pairs[key] = state
                if (t - state.last_close_t) > cfg.proximity_seconds:
                    # The pair separated for longer than the qualifying window and
                    # came back. That is a new encounter, not a continuation, and
                    # treating it as one would let two independent brief encounters
                    # add up to a "sustained" event that never happened.
                    state.first_close_t = t
                    state.samples = 0
                    state.correlation_sum = 0.0
                state.last_close_t = t
                state.samples += 1
                state.correlation_sum += correlation
                seen.add(key)

                elapsed = t - state.first_close_t
                if elapsed < cfg.proximity_seconds:
                    continue
                if state.mean_correlation < cfg.proximity_correlation:
                    continue
                if (t - state.last_emitted_t) < cfg.proximity_repeat_seconds:
                    continue
                state.last_emitted_t = t
                events.append(
                    self._event(
                        "proximity",
                        ts,
                        a.track_id,
                        {
                            "track_ids": [a.track_id, b.track_id],
                            "class_labels": [a.class_label, b.class_label],
                            "seconds": round(elapsed, 2),
                            "distance": round(distance, 4),
                            "distance_unit": "metres" if in_metres else "normalised",
                            "mean_heading_correlation": round(state.mean_correlation, 3),
                            "samples": state.samples,
                        },
                    )
                )
        for key in [k for k in self._pairs if k not in seen]:
            # Drop pairs that were not close this frame. Kept for one frame's grace by
            # the re-encounter check above; holding them indefinitely on a busy
            # junction is O(tracks^2) of state that never gets read again.
            state = self._pairs[key]
            if (t - state.last_close_t) > (cfg.proximity_seconds + cfg.proximity_repeat_seconds):
                del self._pairs[key]
        return events

    def _pair_distance(self, a: Track, b: Track) -> tuple[float | None, bool]:
        """Ground-plane metres when the camera is calibrated, normalised units if not.

        Metres are strongly preferred and the difference is not cosmetic: the same
        image distance is several metres near the top of a road-facing frame and tens
        of centimetres at the bottom, so a normalised threshold means something
        different in every part of the image. The fallback exists because an
        uncalibrated camera should still produce *something* usable, and the emitted
        payload names which unit was used so a consumer is never guessing.
        """
        if self._homography is not None:
            ga = project_to_ground(a.bbox.foot, self._homography)
            gb = project_to_ground(b.bbox.foot, self._homography)
            if ga is not None and gb is not None:
                return math.hypot(ga[0] - gb[0], ga[1] - gb[1]), True
        fa, fb = a.bbox.foot, b.bbox.foot
        return math.hypot(fa[0] - fb[0], fa[1] - fb[1]), False

    # -- plumbing ----------------------------------------------------------

    def _forget_dead(self, live: set[str]) -> None:
        for key in [k for k in self._zone_state if k[0] not in live]:
            del self._zone_state[key]
        for key in [k for k in self._line_side if k[0] not in live]:
            del self._line_side[key]
        for key in [k for k in self._pairs if k[0] not in live and k[1] not in live]:
            del self._pairs[key]
        for tid in [k for k in self._last_speed_t if k not in live]:
            del self._last_speed_t[tid]

    def _event(
        self, kind: str, ts: datetime, track_id: str | None, payload: dict[str, object]
    ) -> PrimitiveEvent:
        return PrimitiveEvent(
            kind=kind, camera_id=self.camera_id, ts=ts, track_id=track_id, payload=payload
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _fraction_along(line: CrossingLine, point: tuple[float, float]) -> float:
    ax, ay = line.a
    bx, by = line.b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return 0.0
    t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / length_sq
    return min(max(t, 0.0), 1.0)


def _heading_correlation(a: Track, b: Track, window: int = 8) -> float:
    """Cosine similarity of the two tracks' recent displacement vectors.

    Displacement over a window rather than the instantaneous step, because a single
    frame's box jitter dominates the step vector of a slow-moving pedestrian — two
    people walking side by side would score near zero. Returns 0.0 when either track
    is essentially stationary, which is correct: two objects that are not moving have
    no heading to correlate, and returning 1.0 for that case would make every pair of
    parked cars a sustained proximity event.
    """
    va = _displacement(a, window)
    vb = _displacement(b, window)
    na = math.hypot(*va)
    nb = math.hypot(*vb)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return (va[0] * vb[0] + va[1] * vb[1]) / (na * nb)


def _displacement(track: Track, window: int) -> tuple[float, float]:
    history = track.history
    if len(history) < 2:
        return (0.0, 0.0)
    recent = list(history)[-window:]
    first, last = recent[0].foot, recent[-1].foot
    return (last[0] - first[0], last[1] - first[1])


def _nearest_person(
    track: Track, tracks: Iterable[Track], limit: float
) -> Track | None:
    best: Track | None = None
    best_distance = limit
    origin = track.bbox.foot
    for other in tracks:
        if other.track_id == track.track_id or other.class_label != "person":
            continue
        point = other.bbox.foot
        distance = math.hypot(point[0] - origin[0], point[1] - origin[1])
        if distance <= best_distance:
            best_distance = distance
            best = other
    return best
