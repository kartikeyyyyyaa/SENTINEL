"""Multi-object tracker. IOU association, ByteTrack-flavoured, no dependencies.

Pure Python and numpy, deliberately. The tracker is the component most likely to
be wrong in a way that produces confident nonsense — a swapped id turns two
vehicles into one that teleported — so it has to be testable without model
weights, without a GPU and without the sandbox. Everything here runs on synthetic
boxes in microseconds.

**What is borrowed from ByteTrack and why.** The one idea worth taking is the
two-pass association over detection confidence. A detection too weak to start a
track is often still good enough to *continue* one: a vehicle passing behind a
pole drops from 0.9 to 0.15 for three frames and comes back. Classic
high-threshold trackers kill the track and mint a new id, and the vehicle count
for that junction is then wrong by one, permanently. So high-confidence
detections associate first, then low-confidence detections are offered only to
tracks that are still unmatched, and low-confidence detections never create a
track. That last clause is what stops the low threshold from filling the scene
with phantom vehicles.

**What is not borrowed.** No Kalman filter and no re-ID embedding. A Kalman
filter's constant-velocity prediction needs a stable dt, and dt here is neither
stable nor known in advance — the intervals are non-uniform and the adaptive
sampler changes the stride underneath the tracker while it runs. An IOU
association that reads the actual timestamps is less clever and does not quietly
degrade when the assumption it was built on stops holding. Re-ID would be a fifth
model to ship for a marginal gain on a task where the real bottleneck is
plumbing.

**Ageing is in seconds, never frames.** See ``TrackerConfig``. "Three frames old"
means anything from 0.1 s to several seconds once the stride moves.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import TrackerConfig
from .primitives import Box, Detection, SpeedEstimate, Track, TrackPoint

log = logging.getLogger(__name__)

PERSON_GROUP = "person"
VEHICLE_GROUP = "vehicle"


def default_group_of(label: str) -> str:
    """Which detections are allowed to associate with each other.

    Grouping rather than exact label matching, because YOLO flips between ``car``
    and ``truck`` on the same vehicle from frame to frame as the box tightens, and
    that flip must not break the track. A person/vehicle flip, on the other hand,
    means the association is simply wrong, so those are kept apart.
    """
    return PERSON_GROUP if label == PERSON_GROUP else VEHICLE_GROUP


class _ActiveTrack:
    """Mutable tracker-internal state. Snapshotted to a frozen ``Track`` on emit.

    Two representations on purpose: the tracker needs to mutate, and the sink
    needs an immutable object it can buffer and retry without the tracker
    changing it in between.
    """

    __slots__ = (
        "track_id",
        "group",
        "box",
        "points",
        "hits",
        "misses",
        "first_t",
        "last_t",
        "confirmed",
        "segment_id",
        "_label_votes",
        "last_confidence",
    )

    def __init__(
        self, track_id: int, detection: Detection, segment_id: int, history: int
    ) -> None:
        self.track_id = track_id
        self.group = default_group_of(detection.label)
        self.box = detection.box
        self.points: deque[TrackPoint] = deque(maxlen=history)
        self.hits = 1
        self.misses = 0
        self.first_t = detection.t
        self.last_t = detection.t
        self.confirmed = False
        self.segment_id = segment_id
        self._label_votes: dict[str, float] = {}
        self.last_confidence = detection.confidence
        self._record(detection)

    def _record(self, detection: Detection) -> None:
        x, y = detection.box.foot
        self.points.append(
            TrackPoint(t=detection.t, x=x, y=y, box=detection.box, confidence=detection.confidence)
        )
        # Confidence-weighted label vote. The strongest observations of an object
        # are the ones where it is large and unoccluded, which are also the ones
        # most likely to have the class right, so a weighted vote is a better
        # estimate of "what is this" than either the first or the latest label.
        self._label_votes[detection.label] = (
            self._label_votes.get(detection.label, 0.0) + detection.confidence
        )

    @property
    def label(self) -> str:
        if not self._label_votes:
            return ""
        return max(self._label_votes.items(), key=lambda kv: kv[1])[0]

    def update(self, detection: Detection) -> None:
        self.box = detection.box
        self.hits += 1
        self.misses = 0
        self.last_t = detection.t
        self.last_confidence = detection.confidence
        self._record(detection)

    def mark_missed(self) -> None:
        self.misses += 1

    def age_at(self, t: float) -> float:
        return t - self.last_t

    def snapshot(self, lost: bool = False) -> Track:
        return Track(
            track_id=self.track_id,
            label=self.label,
            segment_id=self.segment_id,
            points=tuple(self.points),
            hits=self.hits,
            age_seconds=self.last_t - self.first_t,
            confirmed=self.confirmed,
            lost=lost,
        )


@dataclass
class TrackerUpdate:
    """Result of one ``update`` call.

    ``lost`` carries final snapshots of retired tracks. Emitting a track only
    while it is alive means the rule engine never sees the completed trajectory,
    which is exactly what a dwell or wrong-way rule needs; so retirement is an
    event, not a silent deletion.
    """

    tracks: list[Track] = field(default_factory=list)  # Confirmed and currently visible.
    new_ids: list[int] = field(default_factory=list)
    lost: list[Track] = field(default_factory=list)
    matched: int = 0
    unmatched_detections: int = 0
    was_reset: bool = False


class IouTracker:
    """Greedy IOU association with birth/death and a hard reset.

    Greedy rather than Hungarian: with a strict IOU gate the two agree on almost
    every real frame, and greedy is O(n log n) with no dependency. Where they
    disagree is dense overlapping boxes, where a Kalman-less tracker is already
    the wrong tool and a re-ID model would be the answer, not a better solver.
    """

    def __init__(
        self,
        config: TrackerConfig,
        group_of: Callable[[str], str] = default_group_of,
        history_points: int = 256,
    ) -> None:
        self.config = config
        self._group_of = group_of
        self._history_points = history_points
        self._tracks: list[_ActiveTrack] = []
        self._next_id = 1
        self._segment_id = 0
        self.resets = 0
        self.total_tracks_created = 0
        self.total_tracks_retired = 0

    @property
    def active_count(self) -> int:
        return len(self._tracks)

    @property
    def segment_id(self) -> int:
        return self._segment_id

    def reset(self, segment_id: int | None = None) -> list[Track]:
        """Drop all state at a loop cut or reconnect. Returns final snapshots.

        Track ids are **not** reused after a reset — ``_next_id`` keeps climbing.
        A downstream consumer correlating events across a loop cut must be able to
        tell that track 41 in segment 2 is a different object from track 41 in
        segment 1, and the cheapest way to guarantee that is to never issue the
        same id twice in one worker's lifetime.
        """
        final = [t.snapshot(lost=True) for t in self._tracks if t.confirmed]
        self.total_tracks_retired += len(self._tracks)
        self._tracks.clear()
        self.resets += 1
        if segment_id is not None:
            self._segment_id = segment_id
        return final

    def update(
        self,
        detections: Iterable[Detection],
        t: float,
        segment_id: int | None = None,
        weak_detections: Iterable[Detection] = (),
    ) -> TrackerUpdate:
        """Associate one frame's detections and age everything else.

        ``t`` is the frame's stream-timeline position, and it is passed separately
        from the detections because a frame with *no* detections still has to age
        the existing tracks. A tracker that only advances when something is
        detected keeps a stale track alive indefinitely on an empty road.

        ``weak_detections`` are the below-threshold boxes for the second
        association pass. The split is the caller's job, not the tracker's,
        because the two confidence thresholds belong to the detect stage's config
        and the tracker has no business reading them. Callers that do not care
        (the tests, a future stage producing boxes directly) pass nothing and get
        single-pass behaviour.
        """
        result = TrackerUpdate()
        if segment_id is not None and segment_id != self._segment_id:
            # Segment changed without an explicit reset. Treat it as one: the
            # alternative is associating across a scene cut.
            result.lost.extend(self.reset(segment_id))
            result.was_reset = True

        strong_dets = list(detections)
        weak_dets = list(weak_detections)

        # Pass 1: strong detections against every live track.
        matched_tracks, unmatched_tracks, unmatched_strong = self._associate(
            self._tracks, strong_dets
        )
        for track, det in matched_tracks:
            track.update(det)
        result.matched += len(matched_tracks)

        # Pass 2: weak detections offered only to tracks nothing else claimed.
        # This is the ByteTrack step. A weak box may continue a track; it may
        # never start one.
        matched_weak, still_unmatched, _unused_weak = self._associate(unmatched_tracks, weak_dets)
        for track, det in matched_weak:
            track.update(det)
        result.matched += len(matched_weak)

        for track in still_unmatched:
            track.mark_missed()

        # Births, from strong detections only.
        for det in unmatched_strong:
            if len(self._tracks) >= self.config.max_tracks:
                # A frame with hundreds of boxes is a detector failure, not a
                # crowd. Refusing to grow keeps a bad frame from becoming a
                # memory problem that outlives it.
                log.warning("track ceiling %d reached; dropping births", self.config.max_tracks)
                break
            track = _ActiveTrack(self._next_id, det, self._segment_id, self._history_points)
            self._next_id += 1
            self.total_tracks_created += 1
            self._tracks.append(track)
            result.new_ids.append(track.track_id)
        result.unmatched_detections = len(unmatched_strong)

        # Confirmation, then retirement. Order matters: a track that reaches
        # min_hits on the frame it also ages out should still be reported as a
        # completed track rather than vanishing.
        for track in self._tracks:
            if not track.confirmed and track.hits >= self.config.min_hits:
                track.confirmed = True

        survivors: list[_ActiveTrack] = []
        for track in self._tracks:
            if track.age_at(t) > self.config.max_age_seconds:
                self.total_tracks_retired += 1
                if track.confirmed:
                    result.lost.append(track.snapshot(lost=True))
                continue
            survivors.append(track)
        self._tracks = survivors

        result.tracks = [tr.snapshot() for tr in self._tracks if tr.confirmed]
        return result

    def _associate(
        self, tracks: list[_ActiveTrack], detections: list[Detection]
    ) -> tuple[list[tuple[_ActiveTrack, Detection]], list[_ActiveTrack], list[Detection]]:
        """Greedy highest-IOU-first assignment under the group constraint."""
        if not tracks or not detections:
            return [], list(tracks), list(detections)

        iou = np.zeros((len(tracks), len(detections)), dtype=np.float32)
        for i, track in enumerate(tracks):
            for j, det in enumerate(detections):
                if self._group_of(det.label) != track.group:
                    continue  # Leave at 0: below any sane threshold, so never picked.
                iou[i, j] = track.box.iou(det.box)

        pairs: list[tuple[_ActiveTrack, Detection]] = []
        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        # Sort candidate pairs once and walk them, rather than repeatedly scanning
        # for the maximum. Same result, and it does not degrade on a busy frame.
        order = np.argsort(iou, axis=None)[::-1]
        for flat in order:
            score = float(iou.flat[flat])
            if score < self.config.iou_threshold:
                break
            i, j = divmod(int(flat), len(detections))
            if i in used_tracks or j in used_dets:
                continue
            used_tracks.add(i)
            used_dets.add(j)
            pairs.append((tracks[i], detections[j]))

        unmatched_tracks = [t for i, t in enumerate(tracks) if i not in used_tracks]
        unmatched_dets = [d for j, d in enumerate(detections) if j not in used_dets]
        return pairs, unmatched_tracks, unmatched_dets

    def stats(self) -> dict[str, Any]:
        return {
            "active": self.active_count,
            "created": self.total_tracks_created,
            "retired": self.total_tracks_retired,
            "resets": self.resets,
            "segment_id": self._segment_id,
        }


# Detections arrive already split into strong and weak by the caller, which is
# the only component that knows the two confidence thresholds. Splitting here
# rather than inside the tracker keeps the threshold in exactly one config object.
def split_by_confidence(
    detections: Iterable[Detection], threshold: float
) -> tuple[list[Detection], list[Detection]]:
    """Partition detections for the two-pass association.

    A separate function rather than a flag on ``Detection``, because "strong" is a
    tracker concept and the primitive vocabulary is shared with the rule engine,
    which has no opinion on association passes. The primitive keeps the raw
    confidence; the interpretation lives here.
    """
    strong: list[Detection] = []
    weak: list[Detection] = []
    for det in detections:
        (strong if det.confidence >= threshold else weak).append(det)
    return strong, weak


def estimate_speed(
    track: Track,
    meters_per_pixel: float | None = None,
    window_seconds: float = 1.0,
    min_samples: int = 3,
    min_dt_seconds: float = 0.15,
) -> SpeedEstimate | None:
    """Average speed over the most recent window of a track.

    **Net displacement over elapsed time, not summed path length over elapsed
    time.** Per-frame box jitter of a few pixels adds path length that is pure
    noise; dividing that noise by a short interval is how a parked car reports
    200 km/h. Net displacement cancels the jitter.

    **The interval is measured, never assumed.** Every point carries its own
    PTS-derived timestamp, so a window containing intervals of 33 ms, 52 ms and
    410 ms — which is what the stream actually delivers once the adaptive sampler
    widens the stride — gives the same answer as a uniform one.

    ``min_dt_seconds`` rejects windows too short to divide by. Returns None rather
    than a large number, because "not enough evidence" and "very fast" must not be
    the same output.
    """
    if len(track.points) < min_samples:
        return None
    last_t = track.points[-1].t
    window = [p for p in track.points if last_t - p.t <= window_seconds]
    if len(window) < min_samples:
        # Widen once to the last min_samples points. After a stride increase the
        # window can legitimately hold fewer samples than required, and refusing
        # to estimate at all would mean the worker stops reporting speed exactly
        # when it is under load.
        window = list(track.points)[-min_samples:]
    first, last = window[0], window[-1]
    dt = last.t - first.t
    if dt < min_dt_seconds:
        return None
    dist = math.hypot(last.x - first.x, last.y - first.y)
    pps = dist / dt
    kmph = None
    if meters_per_pixel is not None and meters_per_pixel > 0:
        kmph = pps * meters_per_pixel * 3.6
    return SpeedEstimate(
        track_id=track.track_id,
        label=track.label,
        segment_id=track.segment_id,
        t=last.t,
        pixels_per_second=pps,
        dt_seconds=dt,
        sample_count=len(window),
        kmph=kmph,
        meters_per_pixel=meters_per_pixel,
    )


def boxes_to_detections(
    boxes: Iterable[tuple[float, float, float, float]],
    label: str,
    t: float,
    confidence: float = 0.9,
) -> list[Detection]:
    """Convenience for tests and for the dry run. Not used on the hot path."""
    return [
        Detection(box=Box(*b), label=label, confidence=confidence, t=t) for b in boxes
    ]
