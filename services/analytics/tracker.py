"""Multi-object tracker: ByteTrack's idea, greedy association, no Kalman filter.

Identity across frames is where a plausible-looking bug does the most damage. A
tracker that swaps two ids when vehicles cross produces a trajectory in which one
car teleports across the road, and every primitive built on that trajectory — speed,
line crossing, wrong-way, proximity — inherits the error while still looking
entirely reasonable. So the failure modes are the design:

**Two-tier confidence (the ByteTrack idea).** A detection at 0.35 confidence may
start a new track. A detection at 0.15 may only *extend* an existing one. This is
the whole trick and it is worth stating why it works: when a vehicle is partially
occluded — passing behind a pole, a bus, a tree — its detection confidence collapses
but does not vanish. A single-threshold tracker discards the weak detection, ages
the track out, and issues a new id when the vehicle re-emerges. The two-tier scheme
uses the weak detection to carry the track through, while still refusing to let
detector noise create tracks out of nothing.

**Greedy association, not Hungarian.** The Hungarian algorithm finds the assignment
minimising total cost, and that global optimum is sometimes exactly wrong here: when
two objects cross, swapping their ids can reduce total cost, so the optimal
assignment *is* the ID switch. Greedy takes the highest-IoU pair first and commits,
which preferentially preserves the pairing the evidence is most confident about. It
is also O(n²) with a small constant on n ≤ 30 objects, which matters when this runs
on every accepted frame of fifty streams.

**No Kalman filter.** A constant-velocity Kalman filter on a bounding box needs
process and measurement noise tuned per camera geometry, and gets them wrong on a
camera looking down a road where apparent velocity varies by 5x between the near and
far ends of the frame. A plain constant-velocity extrapolation of the box centre,
used only to place the search region while coasting, captures most of the benefit
with no parameters to get wrong. The residual cost is a slightly larger IoU gate.

**Ageing in seconds, not frames.** The adaptive sampler changes the frame stride
underneath the tracker while it runs, so "three frames old" is anything from 0.1 s to
several seconds. Seconds is the only definition that stays correct.

**Ids are never reused.** The counter is monotonic for the process lifetime and is
*not* reset by a scene change. A reused id is a fabricated evidence record: it joins
one vehicle's sightings to another's, and no consumer downstream can detect that it
happened.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from services.common.events import BBox, PrimitiveEvent

from .config import TrackerConfig
from .stages.detect import Detection

#: Objects that behave like each other for association purposes. A detector
#: legitimately flips between ``car`` and ``truck`` on a van, frame to frame, and
#: refusing to associate across that flip breaks the track. It does not
#: legitimately flip between ``person`` and ``bus``, and associating across *that*
#: is a bug that produces a pedestrian travelling at 60 km/h.
_CLASS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"person"}),
    frozenset({"bicycle", "motorcycle"}),
    frozenset({"car", "auto_rickshaw", "bus", "truck", "tractor"}),
    frozenset({"animal"}),
)

#: IoU multiplier applied when two boxes are in different class groups. Not zero:
#: a genuine person-to-vehicle mislabel does happen on a motorcyclist, and a hard
#: block would end the track. A heavy penalty means such a match only wins when the
#: boxes overlap almost exactly and nothing better is available.
_CROSS_GROUP_PENALTY = 0.35

_END_REASON_LOST = "lost"
_END_REASON_SCENE_CHANGE = "scene_change"
_END_REASON_EVICTED = "evicted"


def _group_of(label: str) -> int:
    for i, group in enumerate(_CLASS_GROUPS):
        if label in group:
            return i
    return -1


@dataclass(slots=True)
class TrackPoint:
    """One observation on a track's history.

    ``foot`` rather than ``centre``, because the foot point is what maps to the
    ground plane: using the centre makes a tall vehicle appear further away than a
    short one at the same distance, which biases every speed estimate in the same
    direction rather than randomly. See ``BBox.foot``.
    """

    t: float
    ts: datetime
    foot: tuple[float, float]
    centre: tuple[float, float]
    bbox: BBox
    confidence: float


@dataclass(slots=True)
class Track:
    """One tracked object's state.

    ``label_votes`` accumulates confidence-weighted votes rather than taking the
    latest label. A single frame in which a car is called a truck should not rename
    the track — and it should not rename it *back* on the next frame either, because
    a track whose class oscillates makes every downstream count of vehicle types
    wrong in both directions at once.
    """

    track_id: str
    class_label: str
    confidence: float
    bbox: BBox
    first_t: float
    last_t: float
    first_ts: datetime
    last_ts: datetime
    hits: int = 1
    misses: int = 0
    confirmed: bool = False
    announced: bool = False
    last_update_emitted: float = -1e9
    velocity: tuple[float, float] = (0.0, 0.0)
    label_votes: dict[str, float] = field(default_factory=dict)
    history: deque[TrackPoint] = field(default_factory=lambda: deque(maxlen=256))

    @property
    def age_seconds(self) -> float:
        return self.last_t - self.first_t

    def predicted_bbox(self, t: float) -> BBox:
        """Extrapolate the box to time ``t`` at constant velocity.

        Used only while coasting, to place the search region for re-association
        after an occlusion. Extrapolation is capped because a two-second-old
        velocity estimate projected forward puts the box somewhere arbitrary, and an
        arbitrary box that happens to overlap another object is worse than no
        prediction at all.
        """
        dt = t - self.last_t
        if dt <= 0 or self.hits < 2:
            return self.bbox
        dt = min(dt, 0.5)
        dx = self.velocity[0] * dt
        dy = self.velocity[1] * dt
        try:
            return BBox(
                _clamp(self.bbox.x1 + dx),
                _clamp(self.bbox.y1 + dy),
                _clamp(self.bbox.x2 + dx),
                _clamp(self.bbox.y2 + dy),
            )
        except ValueError:
            # Clamping collapsed the predicted box against a frame edge; the
            # un-extrapolated box is still a usable search region.
            return self.bbox

    def dominant_label(self) -> str:
        if not self.label_votes:
            return self.class_label
        return max(self.label_votes.items(), key=lambda kv: kv[1])[0]


@dataclass(slots=True)
class TrackerUpdate:
    """The result of one frame's association.

    ``assignment`` maps a detection's index in the input list to the track id it was
    associated with. The plate stage needs this: re-deriving the association there
    by a second IoU pass would be an independent decision that can disagree with
    this one, and a disagreement means a plate read attributed to the wrong vehicle.
    """

    active: list[Track]
    started: list[Track]
    ended: list[Track]
    assignment: dict[int, str]
    events: list[PrimitiveEvent]


class Tracker:
    """One instance per camera. Not thread-safe.

    Per camera because track ids are only unique within a camera and because the
    tracker's entire state — including the id counter — must be resettable on that
    camera's scene change without disturbing any other.
    """

    __slots__ = (
        "_config",
        "_camera_id",
        "_tracks",
        "_next_seq",
        "_scene",
        "_update_interval",
        "id_switches_prevented",
        "scene_resets",
        "tracks_started",
        "tracks_ended",
    )

    def __init__(
        self,
        camera_id: int,
        config: TrackerConfig | None = None,
        *,
        update_interval_seconds: float = 1.0,
    ) -> None:
        self._config = config or TrackerConfig()
        self._camera_id = int(camera_id)
        self._tracks: list[Track] = []
        self._next_seq = 1
        self._scene = 0
        self._update_interval = update_interval_seconds
        self.id_switches_prevented = 0
        self.scene_resets = 0
        self.tracks_started = 0
        self.tracks_ended = 0

    @property
    def tracks(self) -> list[Track]:
        """Confirmed, currently-live tracks. Unconfirmed ones are not public.

        A track below ``min_hits`` has not yet earned an id anybody should act on:
        most of them are detector noise that disappears within two frames, and
        exposing them would put a ``track_start`` on the bus for every flicker.
        """
        return [t for t in self._tracks if t.confirmed]

    def track_by_id(self, track_id: str) -> Track | None:
        for t in self._tracks:
            if t.track_id == track_id:
                return t
        return None

    # -- lifecycle ---------------------------------------------------------

    def reset_scene(self, t: float, ts: datetime) -> list[PrimitiveEvent]:
        """End every live track. Called on a PTS discontinuity.

        The objects behind those tracks are gone — the sandbox feed has looped, or
        the stream reconnected at a different point. Carrying an id across that cut
        asserts continuity that does not exist, which is how a court ends up being
        told a vehicle was in two places.

        The id counter is deliberately *not* reset, so no id from the old scene can
        ever be issued again.
        """
        events = [self._end_event(track, ts, _END_REASON_SCENE_CHANGE) for track in self._tracks
                  if track.announced]
        self.tracks_ended += len(events)
        self._tracks.clear()
        self._scene += 1
        self.scene_resets += 1
        return events

    def update(
        self, detections: Sequence[Detection], t: float, ts: datetime
    ) -> TrackerUpdate:
        """Associate ``detections`` with existing tracks. One call per frame.

        ``t`` is stream time in seconds (PTS-derived) and drives ageing; ``ts`` is
        the absolute UTC instant and goes on emitted events. They are separate
        because ageing must be monotonic in stream time even when the absolute clock
        re-anchors, and because events must carry capture time even when stream time
        restarts.
        """
        cfg = self._config
        high: list[int] = []
        low: list[int] = []
        for i, det in enumerate(detections):
            if det.confidence >= cfg.high_confidence:
                high.append(i)
            elif det.confidence >= cfg.low_confidence:
                low.append(i)
            # Below low_confidence: ignored entirely. It cannot start a track and
            # extending one with it would let pure detector noise steer a
            # trajectory.

        assignment: dict[int, str] = {}
        matched_tracks: set[int] = set()

        # Pass 1: strong detections against every track, coasting ones included.
        pairs = self._associate(detections, high, self._tracks, set(), t)
        for det_idx, track_idx in pairs:
            self._absorb(self._tracks[track_idx], detections[det_idx], t, ts)
            assignment[det_idx] = self._tracks[track_idx].track_id
            matched_tracks.add(track_idx)

        # Pass 2: weak detections against whatever is left. Extend only.
        remaining_low = [i for i in low if i not in assignment]
        pairs = self._associate(detections, remaining_low, self._tracks, matched_tracks, t)
        for det_idx, track_idx in pairs:
            self._absorb(self._tracks[track_idx], detections[det_idx], t, ts)
            assignment[det_idx] = self._tracks[track_idx].track_id
            matched_tracks.add(track_idx)
            # Counted because it is the measurable payoff of the two-tier scheme: a
            # track that a single-threshold tracker would have dropped here.
            self.id_switches_prevented += 1

        started: list[Track] = []
        for det_idx in high:
            if det_idx in assignment:
                continue
            if len(self._tracks) >= self._config.max_tracks:
                # A frame with 200 unmatched strong detections is a detector having
                # a bad time on a corrupt frame, not a car park. Refusing to grow is
                # the difference between a degraded minute and an OOM kill that takes
                # the other cameras on the box down with it.
                break
            track = self._spawn(detections[det_idx], t, ts)
            self._tracks.append(track)
            assignment[det_idx] = track.track_id
            started.append(track)

        events: list[PrimitiveEvent] = []
        newly_announced: list[Track] = []
        for track in self._tracks:
            if track.confirmed and not track.announced:
                track.announced = True
                self.tracks_started += 1
                newly_announced.append(track)
                events.append(self._start_event(track, ts))

        ended = self._retire(t, ts, matched_tracks, events)

        for track in self._tracks:
            if not track.announced or track in newly_announced:
                continue
            if track.last_t != t:
                # Coasting. No heartbeat for an object we did not see this frame; a
                # ``track_update`` is an assertion that the object was observed.
                continue
            if (t - track.last_update_emitted) >= self._update_interval:
                track.last_update_emitted = t
                events.append(self._update_event(track, ts))

        return TrackerUpdate(
            active=[t_ for t_ in self._tracks if t_.confirmed],
            started=newly_announced,
            ended=ended,
            assignment=assignment,
            events=events,
        )

    # -- association -------------------------------------------------------

    def _associate(
        self,
        detections: Sequence[Detection],
        det_indices: Iterable[int],
        tracks: Sequence[Track],
        exclude: set[int],
        t: float,
    ) -> list[tuple[int, int]]:
        """Greedy highest-IoU-first matching. Returns ``(det_idx, track_idx)`` pairs."""
        threshold = self._config.iou_threshold
        candidates: list[tuple[float, int, int]] = []
        for det_idx in det_indices:
            det = detections[det_idx]
            det_group = _group_of(det.class_label)
            for track_idx, track in enumerate(tracks):
                if track_idx in exclude:
                    continue
                iou = det.bbox.iou(track.predicted_bbox(t))
                if iou <= 0.0:
                    continue
                if _group_of(track.dominant_label()) != det_group:
                    iou *= _CROSS_GROUP_PENALTY
                if iou >= threshold:
                    candidates.append((iou, det_idx, track_idx))

        # Sorted descending by IoU, then by indices for a deterministic tie-break.
        # Determinism matters: without it, two equally-good candidate pairs resolve
        # in dict order and a tracker test asserting "these ids are stable" becomes
        # a coin flip.
        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
        used_det: set[int] = set()
        used_track: set[int] = set(exclude)
        pairs: list[tuple[int, int]] = []
        for _iou, det_idx, track_idx in candidates:
            if det_idx in used_det or track_idx in used_track:
                continue
            used_det.add(det_idx)
            used_track.add(track_idx)
            pairs.append((det_idx, track_idx))
        return pairs

    def _absorb(self, track: Track, det: Detection, t: float, ts: datetime) -> None:
        dt = t - track.last_t
        old_centre = track.bbox.centre
        new_centre = det.bbox.centre
        if dt > 1e-6:
            # Velocity in normalised units per second, blended rather than replaced.
            # A single frame's jitter in box regression would otherwise dominate the
            # prediction used for the next occlusion.
            vx = (new_centre[0] - old_centre[0]) / dt
            vy = (new_centre[1] - old_centre[1]) / dt
            track.velocity = (
                0.5 * track.velocity[0] + 0.5 * vx,
                0.5 * track.velocity[1] + 0.5 * vy,
            )

        track.bbox = det.bbox
        track.confidence = det.confidence
        track.last_t = t
        track.last_ts = ts
        track.hits += 1
        track.misses = 0
        track.label_votes[det.class_label] = (
            track.label_votes.get(det.class_label, 0.0) + det.confidence
        )
        track.class_label = track.dominant_label()
        if track.hits >= self._config.min_hits:
            track.confirmed = True
        track.history.append(
            TrackPoint(
                t=t,
                ts=ts,
                foot=det.bbox.foot,
                centre=new_centre,
                bbox=det.bbox,
                confidence=det.confidence,
            )
        )

    def _spawn(self, det: Detection, t: float, ts: datetime) -> Track:
        # Scene number in the id, so a human reading a log can see at a glance that
        # two ids belong to different scenes. The sequence number alone would already
        # guarantee uniqueness; this makes it legible.
        track_id = f"c{self._camera_id}-s{self._scene}-{self._next_seq}"
        self._next_seq += 1
        track = Track(
            track_id=track_id,
            class_label=det.class_label,
            confidence=det.confidence,
            bbox=det.bbox,
            first_t=t,
            last_t=t,
            first_ts=ts,
            last_ts=ts,
            label_votes={det.class_label: det.confidence},
            history=deque(maxlen=max(8, self._config.history_points)),
        )
        track.history.append(
            TrackPoint(t, ts, det.bbox.foot, det.bbox.centre, det.bbox, det.confidence)
        )
        if self._config.min_hits <= 1:
            track.confirmed = True
        return track

    def _retire(
        self,
        t: float,
        ts: datetime,
        matched_tracks: set[int],
        events: list[PrimitiveEvent],
    ) -> list[Track]:
        max_age = self._config.max_age_seconds
        survivors: list[Track] = []
        ended: list[Track] = []
        for idx, track in enumerate(self._tracks):
            if idx not in matched_tracks:
                track.misses += 1
            if (t - track.last_t) > max_age:
                if track.announced:
                    events.append(self._end_event(track, ts, _END_REASON_LOST))
                    self.tracks_ended += 1
                    ended.append(track)
                continue
            survivors.append(track)
        self._tracks = survivors
        return ended

    # -- events ------------------------------------------------------------

    def _start_event(self, track: Track, ts: datetime) -> PrimitiveEvent:
        return PrimitiveEvent(
            kind="track_start",
            camera_id=self._camera_id,
            ts=ts,
            track_id=track.track_id,
            payload={
                "class_label": track.class_label,
                "confidence": round(track.confidence, 4),
                "bbox": [round(v, 5) for v in track.bbox.as_tuple()],
                "scene": self._scene,
            },
        )

    def _update_event(self, track: Track, ts: datetime) -> PrimitiveEvent:
        return PrimitiveEvent(
            kind="track_update",
            camera_id=self._camera_id,
            ts=ts,
            track_id=track.track_id,
            payload={
                "class_label": track.class_label,
                "confidence": round(track.confidence, 4),
                "bbox": [round(v, 5) for v in track.bbox.as_tuple()],
                "age_seconds": round(track.age_seconds, 3),
                "hits": track.hits,
            },
        )

    def _end_event(self, track: Track, ts: datetime, reason: str) -> PrimitiveEvent:
        return PrimitiveEvent(
            kind="track_end",
            camera_id=self._camera_id,
            ts=ts,
            track_id=track.track_id,
            payload={
                "class_label": track.class_label,
                "reason": reason,
                # Duration and hit count together let a consumer judge the track's
                # quality: 40 hits over 4 s is a solid observation, 4 hits over 4 s
                # is a track that coasted most of its life and should be trusted less.
                "duration_seconds": round(track.age_seconds, 3),
                "hits": track.hits,
                "last_bbox": [round(v, 5) for v in track.bbox.as_tuple()],
            },
        )


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v
