"""The compute cascade. This module is the project's efficiency argument.

Four stages, each roughly an order of magnitude more expensive than the one before
it, each seeing far fewer items than the one before it:

    decoded frames          100%    ─┐
      1. motion gate         ~30%    │  cheap frame differencing
      2. detect              ~30%    │  YOLOv8n int8, gated frames only
      3. plate crop          ~10%    │  arithmetic on vehicle boxes
      4. plate OCR            ~2%   ─┘  the expensive one

**The counters below are a deliverable, not debug output.** The measured
per-stage figures — items in, items out, seconds spent, share of total compute,
and the fraction of decoded frames each stage actually ran on — are the numbers
the infrastructure-sizing and cost-benefit sections of the submission are built
from. "How many cameras per edge box" and "what does this cost to run across
eighty thousand cameras" are answered by dividing measured throughput into the
fleet size, not by estimating. They are therefore reported on a schedule and at
shutdown, kept per camera and in aggregate, and computed the same way in the dry
run as against the live sandbox so that the two are comparable.

A stage is skipped entirely when its input is empty, so ``frames_reaching`` counts
only frames on which a stage was actually invoked. ``frames_with_work`` is stricter
again: stage 4 declines to re-read a plate it has already read confidently, and
those declines cost nothing and must not dilute the funnel percentage. The
percentages quoted in the submission come from ``frames_with_work``, which is the
only definition that survives someone checking the arithmetic.

The tracker runs between stages 2 and 3 rather than being a stage itself. It is
not part of the funnel — it consumes every detection and produces no input for the
next stage — and folding it in would corrupt the reduction arithmetic that is the
whole point of the instrumentation.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .capture import Frame
from .config import WorkerConfig
from .primitives import (
    KIND_CROWD_DENSITY,
    KIND_PLATE_READ,
    KIND_SPEED_ESTIMATE,
    KIND_TRACK,
    KIND_TRAJECTORY,
    KIND_ZONE_DWELL,
    CrowdDensity,
    Detection,
    PlateRead,
    PrimitiveEvent,
    Track,
    ZoneDwell,
    make_event,
    trajectory_from_track,
)
from .stages import Stage, StageContext
from .stages.detect import DetectStage, Detector
from .stages.motion import MotionGate
from .stages.ocr import OcrStage, PlateReader
from .stages.plate import PlateCropStage
from .tracker import IouTracker, estimate_speed, split_by_confidence

log = logging.getLogger(__name__)


@dataclass
class StageStats:
    """Measured cost and selectivity of one stage.

    ``items_in``/``items_out`` are in that stage's own units — frames for the gate,
    detections out of the detector, crops out of the plate stage, reads out of OCR.
    They are not comparable between stages and are not meant to be; what is
    comparable is ``fraction_of_frames``, which is what the sizing arithmetic uses.
    """

    name: str
    items_in: int = 0
    items_out: int = 0
    # Frames on which this stage was invoked with a non-empty input.
    frames_reaching: int = 0
    # Items the stage did expensive work on, and the frames on which it did any.
    # For three of the four stages these equal items_in and frames_reaching. Stage
    # 4 declines to re-read a plate it has already read confidently, so its work
    # figures are materially lower — and those are the figures the sizing section
    # quotes. See Stage.last_work_units.
    work_units: int = 0
    frames_with_work: int = 0
    seconds: float = 0.0
    errors: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def reduction_factor(self) -> float | None:
        """How many inputs this stage consumed per output it produced.

        None when nothing came out, rather than infinity: a stage that produced no
        output has an undefined reduction factor, and ``float('inf')`` serialises
        to a token that is not valid JSON and would break the registry's parser.
        """
        if self.items_out <= 0:
            return None
        return self.items_in / self.items_out

    @property
    def pass_rate(self) -> float:
        return self.items_out / self.items_in if self.items_in else 0.0

    @property
    def mean_ms(self) -> float:
        return (self.seconds / self.frames_reaching * 1000.0) if self.frames_reaching else 0.0

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "items_in": self.items_in,
            "items_out": self.items_out,
            "frames_reaching": self.frames_reaching,
            "work_units": self.work_units,
            "frames_with_work": self.frames_with_work,
            "seconds": round(self.seconds, 4),
            "mean_ms": round(self.mean_ms, 3),
            "pass_rate": round(self.pass_rate, 4),
            "reduction_factor": (
                round(self.reduction_factor, 3) if self.reduction_factor is not None else None
            ),
            "errors": self.errors,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class CascadeStats:
    """The measured cascade. Feeds the submission's sizing and cost sections.

    Kept per camera. Aggregating across cameras is the caller's job, because the
    interesting number for sizing is the *distribution*: one busy junction and one
    quiet lane average out to a figure that describes neither, and a district buys
    hardware for the busy one.
    """

    camera_id: str = ""
    worker_id: str = ""
    frames_offered: int = 0  # Frames delivered by capture.
    frames_skipped_by_sampler: int = 0  # Dropped before the cascade, adaptive sampling.
    frames_entered: int = 0  # Frames that entered stage 1.
    stages: list[StageStats] = field(default_factory=list)
    events_emitted: int = 0
    resets: int = 0
    wall_seconds: float = 0.0
    stream_seconds: float = 0.0  # PTS-derived span analysed. Not the same as wall.
    sampler: dict[str, Any] = field(default_factory=dict)
    tracker: dict[str, Any] = field(default_factory=dict)
    capture: dict[str, Any] = field(default_factory=dict)

    def stage(self, name: str) -> StageStats | None:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    @property
    def compute_seconds(self) -> float:
        return sum(s.seconds for s in self.stages)

    @property
    def end_to_end_reduction(self) -> float | None:
        """Decoded frames per frame on which the final stage did real work.

        The headline number: with the gate at ~30% and plate crops on ~10% of
        frames, OCR runs on a small single-digit percentage, and this is the factor
        by which the fleet's most expensive stage was made affordable.

        Computed from ``frames_with_work``, not ``frames_reaching``, so that the
        crops stage 4 declines to re-read do not flatter it in either direction.
        """
        if not self.stages:
            return None
        last = self.stages[-1]
        if last.frames_with_work <= 0:
            return None
        return self.frames_offered / last.frames_with_work

    @property
    def throughput_fps(self) -> float:
        """Frames per second of compute this camera's pipeline sustained.

        Divided by ``compute_seconds``, not wall seconds: this is the per-stream
        cost figure, and dividing by wall time would fold in however long the
        thread spent blocked on the network, which says nothing about how many
        cameras a box can carry.
        """
        return self.frames_entered / self.compute_seconds if self.compute_seconds else 0.0

    @property
    def realtime_factor(self) -> float:
        """Stream seconds analysed per second of compute.

        Above 1.0 means this camera is affordable in real time on this hardware,
        and the value is roughly how many such cameras one core can carry. This is
        the number the sizing section divides the fleet by.
        """
        return self.stream_seconds / self.compute_seconds if self.compute_seconds else 0.0

    @property
    def cost_share(self) -> dict[str, float]:
        """Fraction of compute spent in each stage.

        The other half of the cost-benefit argument: it is not enough to show that
        OCR ran on 2% of frames if OCR is still 80% of the compute. This is where
        the next optimisation should go, and it is measured rather than guessed.
        """
        total = self.compute_seconds
        if total <= 0:
            return {}
        return {s.name: round(s.seconds / total, 4) for s in self.stages}

    @property
    def fraction_of_frames(self) -> dict[str, float]:
        """Fraction of decoded frames each stage did real work on. The funnel.

        This dict is the funnel diagram in the submission. It is measured on the
        same code path in the dry run and against the live sandbox, so the two are
        directly comparable.
        """
        if self.frames_offered <= 0:
            return {}
        return {
            s.name: round(s.frames_with_work / self.frames_offered, 5) for s in self.stages
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "worker_id": self.worker_id,
            "frames_offered": self.frames_offered,
            "frames_skipped_by_sampler": self.frames_skipped_by_sampler,
            "frames_entered": self.frames_entered,
            "events_emitted": self.events_emitted,
            "resets": self.resets,
            "wall_seconds": round(self.wall_seconds, 3),
            "stream_seconds": round(self.stream_seconds, 3),
            "compute_seconds": round(self.compute_seconds, 4),
            "throughput_fps": round(self.throughput_fps, 2),
            "realtime_factor": round(self.realtime_factor, 3),
            "end_to_end_reduction": (
                round(self.end_to_end_reduction, 2)
                if self.end_to_end_reduction is not None
                else None
            ),
            "fraction_of_frames": self.fraction_of_frames,
            "cost_share": self.cost_share,
            "stages": [s.as_dict() for s in self.stages],
            "sampler": self.sampler,
            "tracker": self.tracker,
            "capture": self.capture,
        }


@dataclass
class CascadeOutcome:
    """What one frame produced. Returned rather than pushed to the sink.

    The cascade does not own the sink. Keeping emission out of here means the whole
    pipeline can be driven in a test with no sink at all, and means a sink stall
    cannot be mistaken for a slow stage in the timings above.
    """

    events: list[PrimitiveEvent] = field(default_factory=list)
    detections: list[Detection] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    plate_reads: list[PlateRead] = field(default_factory=list)
    gated: bool = False  # False means the motion gate blocked this frame.
    was_reset: bool = False
    seconds: float = 0.0


class Cascade:
    """Runs the stages for one camera and measures what they cost.

    One instance per camera, not shared. Every stage holds per-camera state — the
    gate's reference frame, OCR's per-track memory — and sharing an instance across
    cameras would difference one camera's frame against another's.
    """

    def __init__(
        self,
        stages: list[Stage],
        tracker: IouTracker,
        config: WorkerConfig,
        camera_id: str,
        meters_per_pixel: float | None = None,
        zones: dict[str, tuple[tuple[float, float], ...]] | None = None,
        track_emit_interval: float = 1.0,
        density_emit_interval: float = 2.0,
    ) -> None:
        self.stages = stages
        self.tracker = tracker
        self.config = config
        self.camera_id = camera_id
        self.meters_per_pixel = meters_per_pixel
        # Zone polygons are per-camera registry data. Empty here by default: the
        # registry owns the geometry and the rule engine owns what a zone means.
        # The primitive is produced as soon as polygons are supplied, so adding
        # zones later needs no schema change and no worker change.
        self.zones = zones or {}
        # Emission is throttled per track. A confirmed track on a 25 fps stream
        # would otherwise put 25 events per second per vehicle on the bus, which at
        # fifty cameras is tens of thousands of events a second describing motion
        # that has not meaningfully changed. Once a second per track is enough for
        # every rule the catalogue needs, and the trajectory summary at track death
        # carries the full path anyway.
        self.track_emit_interval = track_emit_interval
        self.density_emit_interval = density_emit_interval

        self.stats = CascadeStats(camera_id=camera_id, worker_id=config.worker_id)
        self.stats.stages = [StageStats(name=s.name) for s in stages]
        self._stats_by_name = {s.name: st for s, st in zip(stages, self.stats.stages)}
        self._last_track_emit: dict[int, float] = {}
        self._last_density_emit = -math.inf
        self._zone_state: dict[tuple[int, str], dict[str, float]] = {}
        self._detect_conf = config.detect.conf_threshold
        self._person_labels = set(config.detect.person_classes)
        self._first_t: float | None = None
        self._last_t: float | None = None
        self._started = time.monotonic()

    def reset(self, segment_id: int) -> list[PrimitiveEvent]:
        """Discard everything that must not survive a scene cut.

        Returns the trajectory events for the tracks that were alive at the cut, so
        a vehicle that was mid-frame when the feed looped still yields a completed
        trajectory rather than silently disappearing.
        """
        events: list[PrimitiveEvent] = []
        for stage in self.stages:
            stage.reset()
        for track in self.tracker.reset(segment_id):
            events.append(self._trajectory_event(track))
        self._last_track_emit.clear()
        self._zone_state.clear()
        self._last_density_emit = -math.inf
        self.stats.resets += 1
        return events

    def offer(self, frame: Frame) -> None:
        """Count a frame the sampler dropped before the cascade saw it.

        Counted separately from ``frames_entered`` because the sizing arithmetic
        needs both: the reduction factors are relative to decoded frames, while
        throughput is relative to frames actually processed, and conflating the two
        would overstate the cascade's selectivity by whatever the stride happens to
        be.
        """
        self.stats.frames_offered += 1
        self.stats.frames_skipped_by_sampler += 1

    def process_frame(self, frame: Frame) -> CascadeOutcome:
        """Run one frame through the funnel."""
        started = time.perf_counter()
        outcome = CascadeOutcome()
        self.stats.frames_offered += 1
        self.stats.frames_entered += 1

        if frame.timing.discontinuity:
            # Rule 3 in capture.py: at a loop cut we start over rather than
            # interpolate. Doing it here, before the stages run, means the gate's
            # reference frame is gone before it can difference across the cut.
            outcome.events.extend(self.reset(frame.segment_id))
            outcome.was_reset = True

        self._track_stream_span(frame)

        ctx = StageContext(
            frame=frame,
            camera_id=self.camera_id,
            t=frame.t,
            segment_id=frame.segment_id,
        )

        items: list[Any] = [frame]
        for stage in self.stages:
            stat = self._stats_by_name[stage.name]
            if not items:
                # Nothing to do. Skipped rather than invoked with an empty list, so
                # the stage's frames_reaching stays a measure of real work.
                break
            n_in = len(items)
            stat.frames_reaching += 1
            stat.items_in += n_in
            stage.last_work_units = None
            t0 = time.perf_counter()
            try:
                items = stage.run(ctx, items)
            except Exception as exc:  # noqa: BLE001 - a stage must not kill the camera
                stat.errors += 1
                log.warning("camera %s stage %s failed: %s", self.camera_id, stage.name, exc)
                items = []
            finally:
                stat.seconds += time.perf_counter() - t0
            stat.items_out += len(items)
            # A stage that does not report its own work count did work on
            # everything it was handed, which is true of every stage except OCR.
            work = n_in if stage.last_work_units is None else stage.last_work_units
            stat.work_units += work
            if work > 0:
                stat.frames_with_work += 1

            if stage.name == "motion":
                outcome.gated = bool(items)
            elif stage.name == "detect":
                detections = [d for d in items if isinstance(d, Detection)]
                outcome.detections = detections
                # The tracker sits here: after detection, before the plate stage,
                # which needs track ids so OCR can read once per vehicle.
                outcome.events.extend(self._run_tracker(ctx, detections, outcome))
                outcome.tracks = list(outcome.tracks)
            elif stage.name == "ocr":
                reads = [r for r in items if isinstance(r, PlateRead)]
                outcome.plate_reads = reads
                for read in reads:
                    outcome.events.append(
                        make_event(
                            self.camera_id,
                            KIND_PLATE_READ,
                            read.t,
                            ctx.segment_id,
                            read.as_dict(),
                            wall_time=time.time(),
                            worker_id=self.config.worker_id,
                        )
                    )

        if outcome.gated or not self._has_stage("motion"):
            self._emit_density(ctx, outcome)

        outcome.seconds = time.perf_counter() - started
        self.stats.events_emitted += len(outcome.events)
        self.stats.wall_seconds = time.monotonic() - self._started
        return outcome

    def _has_stage(self, name: str) -> bool:
        return name in self._stats_by_name

    def _track_stream_span(self, frame: Frame) -> None:
        """Accumulate PTS-derived stream time analysed, skipping segment gaps.

        Measured on the stream timeline rather than the wall clock, because
        ``realtime_factor`` is meant to answer "how many streams can one core
        carry" and wall time at join is compressed by the gateway's GOP replay.
        Using wall time there would flatter the result by the replay factor.
        """
        t = frame.t
        if self._last_t is None or frame.timing.discontinuity:
            self._first_t = t
            self._last_t = t
            return
        dt = t - self._last_t
        self._last_t = t
        if 0 < dt < 5.0:  # Ignore implausible jumps; a gap is not time we analysed.
            self.stats.stream_seconds += dt

    def _run_tracker(
        self, ctx: StageContext, detections: list[Detection], outcome: CascadeOutcome
    ) -> list[PrimitiveEvent]:
        strong, weak = split_by_confidence(detections, self._detect_conf)
        update = self.tracker.update(
            strong, ctx.t, segment_id=ctx.segment_id, weak_detections=weak
        )
        # The plate stage resolves a detection's track by overlap against these.
        ctx.extras["track_boxes"] = [
            (tr.track_id, tr.latest_box) for tr in update.tracks if tr.latest_box is not None
        ]
        outcome.tracks = update.tracks

        events: list[PrimitiveEvent] = []
        for track in update.tracks:
            events.extend(self._emit_track(ctx, track))
        for track in update.lost:
            events.append(self._trajectory_event(track))
            self._last_track_emit.pop(track.track_id, None)
            events.extend(self._close_zones(track))
        return events

    def _emit_track(self, ctx: StageContext, track: Track) -> list[PrimitiveEvent]:
        events: list[PrimitiveEvent] = []
        last = self._last_track_emit.get(track.track_id, -math.inf)
        if ctx.t - last < self.track_emit_interval:
            # Still measured, still tracked, just not re-announced. Throttling
            # emission does not throttle the tracker.
            self._update_zones(ctx, track)
            return events
        self._last_track_emit[track.track_id] = ctx.t
        wall = time.time()
        events.append(
            make_event(
                self.camera_id,
                KIND_TRACK,
                track.last_seen,
                track.segment_id,
                track.as_dict(),
                wall_time=wall,
                worker_id=self.config.worker_id,
            )
        )
        speed = estimate_speed(track, meters_per_pixel=self.meters_per_pixel)
        if speed is not None:
            events.append(
                make_event(
                    self.camera_id,
                    KIND_SPEED_ESTIMATE,
                    speed.t,
                    track.segment_id,
                    speed.as_dict(),
                    wall_time=wall,
                    worker_id=self.config.worker_id,
                )
            )
        events.extend(self._update_zones(ctx, track))
        return events

    def _trajectory_event(self, track: Track) -> PrimitiveEvent:
        trajectory = trajectory_from_track(track)
        return make_event(
            self.camera_id,
            KIND_TRAJECTORY,
            trajectory.t_end,
            track.segment_id,
            trajectory.as_dict(),
            wall_time=time.time(),
            worker_id=self.config.worker_id,
        )

    def _emit_density(self, ctx: StageContext, outcome: CascadeOutcome) -> None:
        if ctx.t - self._last_density_emit < self.density_emit_interval:
            return
        people = [d for d in outcome.detections if d.label in self._person_labels]
        if not people:
            return
        self._last_density_emit = ctx.t
        mean_conf = sum(d.confidence for d in people) / len(people)
        density = CrowdDensity(
            segment_id=ctx.segment_id,
            t=ctx.t,
            person_count=len(people),
            region_id="frame",
            region_area_px=float(ctx.frame.width * ctx.frame.height),
            mean_confidence=mean_conf,
        )
        outcome.events.append(
            make_event(
                self.camera_id,
                KIND_CROWD_DENSITY,
                ctx.t,
                ctx.segment_id,
                density.as_dict(),
                wall_time=time.time(),
                worker_id=self.config.worker_id,
            )
        )

    def _update_zones(self, ctx: StageContext, track: Track) -> list[PrimitiveEvent]:
        """Accumulate dwell for a track inside each configured polygon.

        ``max_movement_px`` is tracked alongside the dwell because "present in the
        zone" and "not moving in the zone" are different facts and a busy junction
        is permanently the first. Which of the two matters is a rule's decision.
        """
        if not self.zones or not track.points:
            return []
        point = track.points[-1]
        events: list[PrimitiveEvent] = []
        for zone_id, polygon in self.zones.items():
            key = (track.track_id, zone_id)
            inside = point_in_polygon(point.x, point.y, polygon)
            state = self._zone_state.get(key)
            if inside and state is None:
                self._zone_state[key] = {
                    "entered_t": point.t,
                    "last_seen_t": point.t,
                    "x": point.x,
                    "y": point.y,
                    "max_movement": 0.0,
                }
            elif inside and state is not None:
                moved = math.hypot(point.x - state["x"], point.y - state["y"])
                state["max_movement"] = max(state["max_movement"], moved)
                state["last_seen_t"] = point.t
            elif not inside and state is not None:
                events.append(self._zone_event(track, zone_id, state))
                del self._zone_state[key]
        return events

    def _close_zones(self, track: Track) -> list[PrimitiveEvent]:
        """Emit dwell for a retired track that was still inside a zone.

        Without this a vehicle that parked in a no-parking zone and stayed there
        until its track aged out would never produce a dwell primitive — the exact
        case the primitive exists for.
        """
        events: list[PrimitiveEvent] = []
        for key in [k for k in self._zone_state if k[0] == track.track_id]:
            events.append(self._zone_event(track, key[1], self._zone_state.pop(key)))
        return events

    def _zone_event(
        self, track: Track, zone_id: str, state: dict[str, float]
    ) -> PrimitiveEvent:
        dwell = ZoneDwell(
            track_id=track.track_id,
            label=track.label,
            segment_id=track.segment_id,
            zone_id=zone_id,
            entered_t=state["entered_t"],
            last_seen_t=state["last_seen_t"],
            dwell_seconds=state["last_seen_t"] - state["entered_t"],
            still=state["max_movement"] < 12.0,
            max_movement_px=state["max_movement"],
        )
        return make_event(
            self.camera_id,
            KIND_ZONE_DWELL,
            dwell.last_seen_t,
            track.segment_id,
            dwell.as_dict(),
            wall_time=time.time(),
            worker_id=self.config.worker_id,
        )

    def snapshot_stats(self) -> CascadeStats:
        """Fill in the derived sub-reports and hand back the stats object.

        Not a copy. The caller reads it immediately and serialises it; copying a
        stats tree on a 30 s timer for fifty cameras is pointless allocation.
        """
        for stage in self.stages:
            extra = stage.stats()
            if extra:
                self._stats_by_name[stage.name].extra = extra
        self.stats.tracker = self.tracker.stats()
        return self.stats


def point_in_polygon(x: float, y: float, polygon: tuple[tuple[float, float], ...]) -> bool:
    """Ray-casting point-in-polygon. Handles concave zones; no dependency.

    Zone polygons come from an operator drawing on the console, which means they
    are frequently concave (a zone that follows a kerb around a corner) and
    occasionally self-intersecting. Ray casting handles the first correctly and
    degrades predictably on the second, which is better than a convex-hull test
    that would silently include the road.
    """
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            # x coordinate where the edge crosses the horizontal ray through y.
            t = (y - y1) / (y2 - y1) if y2 != y1 else 0.0
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def build_stages(
    config: WorkerConfig, detector: Detector, reader: PlateReader
) -> list[Stage]:
    """Assemble the enabled stages in cascade order.

    A disabled stage is dropped, not made a no-op. Where the dropped stage is
    type-preserving (only the motion gate is) the chain continues; otherwise it
    truncates there, because the stages after it consume a type nothing is
    producing. That is enforced here rather than left as a trap: switching off
    detection and expecting plate reads should produce a clear log line, not a
    worker that runs and emits nothing.
    """
    from .config import STAGE_ORDER  # noqa: PLC0415 - avoids a circular import at module load

    built: list[Stage] = []
    for name in STAGE_ORDER:
        enabled = config.stage_enabled(name)
        if name == "motion":
            if enabled:
                built.append(MotionGate(config.motion))
            else:
                # Passthrough: the funnel simply starts at stage 2. Useful for
                # measuring what the gate is actually buying.
                log.info("motion gate disabled: every decoded frame will reach the detector")
            continue
        if not enabled:
            log.info("stage %s disabled: cascade truncates here", name)
            break
        if name == "detect":
            built.append(DetectStage(config.detect, detector))
        elif name == "plate":
            built.append(PlateCropStage(config.plate, config.detect.vehicle_classes))
        elif name == "ocr":
            built.append(OcrStage(config.ocr, reader))
    return built


def build_cascade(
    config: WorkerConfig,
    camera_id: str,
    detector: Detector,
    reader: PlateReader,
    meters_per_pixel: float | None = None,
    zones: dict[str, tuple[tuple[float, float], ...]] | None = None,
) -> Cascade:
    return Cascade(
        stages=build_stages(config, detector, reader),
        tracker=IouTracker(config.tracker),
        config=config,
        camera_id=camera_id,
        meters_per_pixel=meters_per_pixel,
        zones=zones,
    )
