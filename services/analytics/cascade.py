"""The compute cascade. This module is the reason the platform is affordable.

The problem, stated in the only terms that matter: running a detector plus ANPR on
every frame of every camera in a district-scale deployment needs hundreds of GPUs.
Nobody is going to buy them. So the pipeline is arranged as four progressively
narrower funnels, each one cheap enough to decide whether the next, more expensive
one is worth running:

    frames  --[stage 0: motion gate]-->    ~8% of frames
            --[stage 1: detection]-->      objects
            --[stage 2: plate crop]-->     plate candidates (tiny boxes rejected)
            --[stage 3: OCR]-->            plate reads (one per vehicle, not per frame)

Each stage removes work that the next stage would have wasted. A frame of an empty
road never reaches the detector. A pedestrian never reaches the plate localiser. A
vehicle 80 metres away, whose plate is nine pixels wide, never reaches OCR. A vehicle
already read at 0.94 confidence is never read again.

**The instrumentation is the deliverable, not a debug aid.** A submission that says
"our cascade gives roughly a 10x saving" is an assertion. ``CascadeStats`` measures
it: units in and out of every stage, drop reasons with counts, and cumulative wall
time per stage. ``funnel()`` reports pass-through ratios and mean cost per stage with
no modelling assumption in it at all. ``effective_speedup()`` divides the cost of the
hypothetical "run every stage on every frame" pipeline by the cost actually
incurred, using per-unit costs this process measured on this hardware. That number is
what turns "~500 GPUs" into "~50 GPUs" in the cost-benefit analysis, and it has to
survive somebody asking how it was obtained.

The one modelling assumption in ``effective_speedup`` is named in its docstring and
nowhere hidden. ``funnel()`` is assumption-free and is the number to fall back on if
anybody disputes it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import numpy as np

from services.common.events import PLATED_CLASSES, PrimitiveEvent, Sighting

from .config import WorkerConfig
from .stages.detect import Detection, Detector, build_detector
from .stages.motion import MotionDecision, MotionGate
from .stages.plate import (
    OcrReader,
    PlateCrop,
    PlateLocaliser,
    PlateRead,
    PlateReadStage,
    build_reader,
)
from .tracker import Track, Tracker

#: What one "unit of work" means for each stage. The distinction is load-bearing in
#: ``effective_speedup``: a frame-unit stage's hypothetical cost scales with the
#: number of frames, an object-unit stage's with the number of objects.
FRAME_UNIT = "frame"
OBJECT_UNIT = "object"

_STAGE_UNITS = {
    "motion": FRAME_UNIT,
    "detect": FRAME_UNIT,
    "plate": OBJECT_UNIT,
    "ocr": OBJECT_UNIT,
}


@dataclass(slots=True)
class StageStats:
    """Measured counters for one stage. Every field is incremented, never estimated.

    ``items_in`` versus ``units_worked`` is a deliberate distinction. ``items_in`` is
    what was offered to the stage; ``units_worked`` is what it actually spent an
    expensive operation on. For the motion gate the two are equal. For OCR they are
    very different, and the ratio between them is the dedupe cache's contribution.
    Collapsing them would make the per-unit cost — and therefore the whole speedup
    calculation — wrong by an order of magnitude.
    """

    name: str
    unit: str
    items_in: int = 0
    items_out: int = 0
    units_worked: int = 0
    seconds: float = 0.0
    drops: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str, count: int = 1) -> None:
        if count:
            self.drops[reason] = self.drops.get(reason, 0) + count

    @property
    def pass_ratio(self) -> float:
        """Fraction of offered items that survived this stage."""
        return self.items_out / self.items_in if self.items_in else 0.0

    @property
    def mean_ms(self) -> float:
        """Mean wall time per *offered* item, in milliseconds.

        Per offered rather than per worked, because this is the number that answers
        "what does this stage cost me per frame of video", which is the question the
        sizing exercise asks.
        """
        return (self.seconds * 1000.0 / self.items_in) if self.items_in else 0.0

    @property
    def cost_per_unit_seconds(self) -> float:
        """Measured cost of one unit of real work. ``0.0`` when nothing was worked.

        Zero is returned rather than raising, and the caller is expected to treat it
        as "unknown" rather than "free". ``CascadeStats.speedup_detail`` lists which
        stages were unmeasured so a zero can never be quietly read as a saving.
        """
        return self.seconds / self.units_worked if self.units_worked else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "unit": self.unit,
            "items_in": self.items_in,
            "items_out": self.items_out,
            "units_worked": self.units_worked,
            "pass_ratio": round(self.pass_ratio, 5),
            "seconds": round(self.seconds, 4),
            "mean_ms": round(self.mean_ms, 4),
            "cost_per_unit_ms": round(self.cost_per_unit_seconds * 1000.0, 4),
            "drops": dict(sorted(self.drops.items())),
        }


@dataclass(slots=True)
class CascadeStats:
    """The funnel, measured. One instance per camera; ``merge`` aggregates them.

    The population counters (``frames_offered`` and friends) are kept alongside the
    per-stage stats rather than being derived from them, because the derivation would
    silently break the first time a stage is disabled: with stage 1 off,
    ``detect.items_in`` is zero and every ratio computed from it is a lie, whereas
    ``frames_offered`` is still the truth.
    """

    frames_offered: int = 0
    frames_analysed: int = 0
    objects_detected: int = 0
    objects_plated: int = 0
    crops_made: int = 0
    reads_made: int = 0
    reads_emitted: int = 0
    sightings_emitted: int = 0
    scene_changes: int = 0
    wall_seconds: float = 0.0
    stages: dict[str, StageStats] = field(default_factory=dict)

    def stage(self, name: str) -> StageStats:
        stats = self.stages.get(name)
        if stats is None:
            stats = StageStats(name=name, unit=_STAGE_UNITS.get(name, FRAME_UNIT))
            self.stages[name] = stats
        return stats

    # -- the two headline reports -----------------------------------------

    def funnel(self) -> dict[str, object]:
        """Pass-through ratio and mean cost per stage. No assumptions whatsoever.

        This is pure measurement: every number is a counter divided by another
        counter this process incremented. If ``effective_speedup`` is ever disputed,
        this is the fallback, because there is nothing in it to dispute.

        ``cumulative_ratio`` is the interesting column. It is the fraction of the
        original frames that reach each stage, and reading down it is reading the
        cascade's whole argument: 100% of frames enter, 8% reach the detector, and
        the OCR engine — the most expensive thing in the pipeline by a wide margin —
        runs on a small fraction of one percent of them.
        """
        rows: list[dict[str, object]] = []
        cumulative = 1.0
        for name in ("motion", "detect", "plate", "ocr"):
            stats = self.stages.get(name)
            if stats is None or stats.items_in == 0:
                continue
            cumulative *= stats.pass_ratio
            row = stats.as_dict()
            row["cumulative_ratio"] = round(cumulative, 6)
            row["share_of_measured_cost"] = (
                round(stats.seconds / self.measured_seconds(), 5)
                if self.measured_seconds() > 0
                else 0.0
            )
            rows.append(row)

        return {
            "frames_offered": self.frames_offered,
            "frames_analysed": self.frames_analysed,
            "frame_pass_ratio": round(
                self.frames_analysed / self.frames_offered, 5
            ) if self.frames_offered else 0.0,
            "objects_detected": self.objects_detected,
            "objects_plated": self.objects_plated,
            "crops_made": self.crops_made,
            "ocr_calls": self.reads_made,
            "plate_reads_emitted": self.reads_emitted,
            "sightings_emitted": self.sightings_emitted,
            "scene_changes": self.scene_changes,
            "ocr_calls_per_1000_frames": round(
                self.reads_made * 1000.0 / self.frames_offered, 3
            ) if self.frames_offered else 0.0,
            "measured_seconds": round(self.measured_seconds(), 4),
            "stages": rows,
        }

    def measured_seconds(self) -> float:
        """Wall time actually spent inside the four stages."""
        return sum(s.seconds for s in self.stages.values())

    def hypothetical_seconds(self) -> float:
        """Cost of the same video with no cascade at all.

        The baseline is "detect on every frame, crop every plated box, OCR every
        crop, every frame" — i.e. what a straightforward implementation of the same
        feature set would cost. Every per-unit cost is one this process measured on
        this hardware; nothing is taken from a datasheet.

        The motion gate contributes **zero** to the baseline, because a pipeline
        without a cascade has no gate to pay for. That makes the comparison
        conservative in our favour's *opposite* direction: we pay for stage 0 in the
        measured figure and get no credit for it in the baseline.

        The one assumption, stated plainly: the frames the gate dropped are taken to
        contain plated objects at the same rate as the frames it passed. On a road
        scene that is close to true — a frame with no *motion* still contains parked
        and queuing vehicles — but where it is not, this overstates the baseline and
        therefore the speedup. ``funnel()`` has no such assumption in it.
        """
        total = 0.0
        detect = self.stages.get("detect")
        if detect is not None and detect.units_worked:
            total += detect.cost_per_unit_seconds * self.frames_offered

        plated_per_frame = (
            self.objects_plated / self.frames_analysed if self.frames_analysed else 0.0
        )
        hypothetical_objects = self.frames_offered * plated_per_frame
        for name in ("plate", "ocr"):
            stats = self.stages.get(name)
            if stats is not None and stats.units_worked:
                total += stats.cost_per_unit_seconds * hypothetical_objects
        return total

    def effective_speedup(self) -> float:
        """Measured cost reduction: hypothetical / measured. Returns 0.0 if unknown.

        This is the single number the cost-benefit section of the submission rests
        on, so it is computed from counters rather than asserted, and
        ``speedup_detail()`` shows the working — including which stages could not be
        priced because they never did any work.
        """
        measured = self.measured_seconds()
        if measured <= 0:
            return 0.0
        return self.hypothetical_seconds() / measured

    def speedup_detail(self) -> dict[str, object]:
        """The arithmetic behind ``effective_speedup``, term by term.

        Exists so the number can be checked by hand from a stats dump, and so a
        reviewer can see immediately when a stage was unpriced. An unpriced stage
        makes the speedup an *under*-estimate, which is the safe direction, but the
        reader still deserves to be told.
        """
        plated_per_frame = (
            self.objects_plated / self.frames_analysed if self.frames_analysed else 0.0
        )
        hypothetical_objects = self.frames_offered * plated_per_frame
        terms: list[dict[str, object]] = []
        unpriced: list[str] = []
        for name in ("motion", "detect", "plate", "ocr"):
            stats = self.stages.get(name)
            if stats is None:
                continue
            if not stats.units_worked and stats.items_in:
                unpriced.append(name)
            if name == "motion":
                hypothetical_units = 0.0  # No gate exists in the baseline.
            elif stats.unit == FRAME_UNIT:
                hypothetical_units = float(self.frames_offered)
            else:
                hypothetical_units = hypothetical_objects
            terms.append(
                {
                    "stage": name,
                    "unit": stats.unit,
                    "measured_units": stats.units_worked,
                    "measured_seconds": round(stats.seconds, 4),
                    "cost_per_unit_ms": round(stats.cost_per_unit_seconds * 1000.0, 4),
                    "hypothetical_units": round(hypothetical_units, 2),
                    "hypothetical_seconds": round(
                        stats.cost_per_unit_seconds * hypothetical_units, 4
                    ),
                }
            )
        return {
            "plated_objects_per_analysed_frame": round(plated_per_frame, 4),
            "terms": terms,
            "measured_seconds": round(self.measured_seconds(), 4),
            "hypothetical_seconds": round(self.hypothetical_seconds(), 4),
            "effective_speedup": round(self.effective_speedup(), 3),
            "unpriced_stages": unpriced,
            "assumption": (
                "Frames dropped by the motion gate are assumed to contain plated "
                "objects at the same rate as frames that passed it. The motion gate "
                "itself is charged to the measured side and credited nothing on the "
                "hypothetical side."
            ),
        }

    def merge(self, other: CascadeStats) -> None:
        """Fold another camera's counters in. Used for the fleet-wide figure."""
        self.frames_offered += other.frames_offered
        self.frames_analysed += other.frames_analysed
        self.objects_detected += other.objects_detected
        self.objects_plated += other.objects_plated
        self.crops_made += other.crops_made
        self.reads_made += other.reads_made
        self.reads_emitted += other.reads_emitted
        self.sightings_emitted += other.sightings_emitted
        self.scene_changes += other.scene_changes
        self.wall_seconds = max(self.wall_seconds, other.wall_seconds)
        for name, stats in other.stages.items():
            mine = self.stage(name)
            mine.items_in += stats.items_in
            mine.items_out += stats.items_out
            mine.units_worked += stats.units_worked
            mine.seconds += stats.seconds
            for reason, count in stats.drops.items():
                mine.drop(reason, count)

    def as_dict(self) -> dict[str, object]:
        return {"funnel": self.funnel(), "speedup": self.speedup_detail()}


@dataclass(slots=True)
class FrameOutcome:
    """Everything one frame produced. Empty is the common and correct case.

    ``motion`` is carried even when the frame was dropped, so the caller can log
    *why* nothing came out. "No detections" and "the gate never opened" are different
    facts and an operator debugging a camera needs to tell them apart.
    """

    analysed: bool
    motion: MotionDecision
    detections: list[Detection] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    crops: list[PlateCrop] = field(default_factory=list)
    reads: list[PlateRead] = field(default_factory=list)
    sightings: list[Sighting] = field(default_factory=list)
    events: list[PrimitiveEvent] = field(default_factory=list)


class Cascade:
    """The assembled funnel for one camera.

    Owns the tracker as well as the four stages, because stages 2 and 3 need track
    identity to work: the plate localiser has to attribute a crop to a track, and the
    OCR dedupe cache is keyed on track id. Putting the tracker outside the cascade
    would mean either passing it in on every call or re-deriving the association a
    second time — and a second, independent association can disagree with the first,
    which means a plate read attributed to the wrong vehicle.
    """

    __slots__ = (
        "camera_id",
        "_config",
        "_motion",
        "_detector",
        "_plate",
        "_ocr",
        "_tracker",
        "stats",
    )

    def __init__(
        self,
        camera_id: int,
        config: WorkerConfig,
        *,
        detector: Detector | None = None,
        reader: OcrReader | None = None,
    ) -> None:
        self.camera_id = int(camera_id)
        self._config = config
        self.stats = CascadeStats()
        self._motion = MotionGate(config.motion)
        # Injectable so the worker can share one model across cameras behind a lock
        # — fifty copies of a detector session is fifty copies of the weights, which
        # on hardware a district can afford is the difference between running and not.
        self._detector = detector if detector is not None else build_detector(config.detect)
        self._plate = PlateLocaliser(config.plate)
        self._ocr = PlateReadStage(config.ocr, reader if reader is not None else build_reader(config.ocr))
        self._tracker = Tracker(
            camera_id,
            config.tracker,
            update_interval_seconds=config.primitives.track_update_seconds,
        )

    @property
    def tracker(self) -> Tracker:
        return self._tracker

    @property
    def detector_name(self) -> str:
        return getattr(self._detector, "name", "unknown")

    @property
    def ocr_name(self) -> str:
        return getattr(self._ocr.reader, "name", "unknown")

    def reset_scene(self, t: float, ts: datetime, reason: str = "pts_discontinuity") -> list[PrimitiveEvent]:
        """Handle a hard scene discontinuity: the sandbox feed looped, or we
        reconnected at a different point in it.

        All three pieces of per-scene state are reset here, in one place, because
        forgetting any one of them produces a different silent corruption: a stale
        motion reference blinds the gate for several frames, a stale tracker
        attributes the new scene's objects to the old scene's ids, and a stale OCR
        dedupe cache attributes the old scene's plate to a new vehicle.
        """
        self.stats.scene_changes += 1
        self._motion.reset()
        self._ocr.reset()
        events = self._tracker.reset_scene(t, ts)
        events.append(
            PrimitiveEvent(
                kind="scene_change",
                camera_id=self.camera_id,
                ts=ts,
                payload={
                    "reason": reason,
                    # Ended track ids are listed so a consumer can close its own
                    # per-track state. Without this a correlation layer holding open
                    # tracks would wait out its own timeout on every loop of the
                    # demo feed.
                    "ended_tracks": [e.track_id for e in events if e.kind == "track_end"],
                },
            )
        )
        return events

    def process(
        self, frame: np.ndarray, *, t: float, ts: datetime, pts: float, frame_index: int
    ) -> FrameOutcome:
        """Run one frame through the funnel.

        ``t`` is stream seconds for ageing and heartbeats, ``ts`` is absolute UTC for
        events, ``pts`` is recorded on each sighting as provenance so an evidence
        export can re-extract the exact source frame.
        """
        stats = self.stats
        stats.frames_offered += 1
        cfg = self._config

        # -- stage 0 -------------------------------------------------------
        motion_stats = stats.stage("motion")
        motion_stats.items_in += 1
        motion_stats.units_worked += 1
        started = time.perf_counter()
        decision = self._motion.evaluate(frame, t)
        motion_stats.seconds += time.perf_counter() - started
        if not decision.passed:
            motion_stats.drop(decision.reason)
            return FrameOutcome(analysed=False, motion=decision)
        motion_stats.items_out += 1
        stats.frames_analysed += 1

        # -- stage 1 -------------------------------------------------------
        detections: list[Detection] = []
        if cfg.stage_enabled("detect"):
            detect_stats = stats.stage("detect")
            detect_stats.items_in += 1
            detect_stats.units_worked += 1
            started = time.perf_counter()
            detections = self._detector.detect(frame, frame_index)
            detect_stats.seconds += time.perf_counter() - started
            detections = [d for d in detections if d.confidence >= cfg.tracker.low_confidence]
            if detections:
                detect_stats.items_out += 1
            else:
                # A frame the gate passed but the detector emptied is worth counting:
                # a high ratio here means the gate is opening on something that is
                # not an object, which is the first symptom of the illumination guard
                # being defeated by a camera with a failing iris.
                detect_stats.drop("no_objects")
            stats.objects_detected += len(detections)

        # -- tracking ------------------------------------------------------
        update = self._tracker.update(detections, t, ts)
        events = list(update.events)

        # -- stage 2 -------------------------------------------------------
        crops: list[PlateCrop] = []
        plated_count = sum(1 for d in detections if d.class_label in PLATED_CLASSES)
        stats.objects_plated += plated_count
        if cfg.stage_enabled("plate") and detections:
            plate_stats = stats.stage("plate")
            plate_stats.items_in += len(detections)
            before_drops = sum(self._plate.drops.values())
            started = time.perf_counter()
            crops = self._plate.locate(frame, detections, update.assignment)
            plate_stats.seconds += time.perf_counter() - started
            plate_stats.items_out += len(crops)
            plate_stats.units_worked += len(crops)
            stats.crops_made += len(crops)
            # Drop reasons are read back off the stage rather than recomputed, so the
            # counter and the code that rejects share one source of truth.
            new_drops = sum(self._plate.drops.values()) - before_drops
            if new_drops:
                _sync_drops(plate_stats, self._plate.drops)

        # -- stage 3 -------------------------------------------------------
        reads: list[PlateRead] = []
        if cfg.stage_enabled("ocr") and crops:
            ocr_stats = stats.stage("ocr")
            ocr_stats.items_in += len(crops)
            calls_before = _reader_calls(self._ocr.reader)
            started = time.perf_counter()
            reads = self._ocr.read_all(crops)
            ocr_stats.seconds += time.perf_counter() - started
            ocr_stats.items_out += len(reads)
            calls = _reader_calls(self._ocr.reader) - calls_before
            ocr_stats.units_worked += calls
            stats.reads_made += calls
            stats.reads_emitted += len(reads)
            _sync_drops(ocr_stats, self._ocr.drops)

        # -- emission ------------------------------------------------------
        reads_by_track = {r.track_id: r for r in reads if r.track_id}
        sightings = self._sightings(update.assignment, detections, reads_by_track, ts, pts)
        stats.sightings_emitted += len(sightings)
        for read in reads:
            events.append(self._anpr_event(read, ts))

        return FrameOutcome(
            analysed=True,
            motion=decision,
            detections=detections,
            tracks=update.active,
            crops=crops,
            reads=reads,
            sightings=sightings,
            events=events,
        )

    # -- emission helpers --------------------------------------------------

    def _sightings(
        self,
        assignment: dict[int, str],
        detections: Sequence[Detection],
        reads_by_track: dict[str, PlateRead],
        ts: datetime,
        pts: float,
    ) -> list[Sighting]:
        """One ``Sighting`` per *tracked* detection. Untracked ones are dropped.

        A detection with no track has not been confirmed as an object yet — most such
        detections are single-frame detector noise. Emitting them would put a
        sighting with a fabricated identity on the bus, and ``Sighting.track_id`` is
        not optional in the shared contract precisely because a sighting without
        identity cannot be correlated with anything.

        A plate read is attached to the sighting when the *track* has one, not only
        when this frame produced one. That is the dedupe paying off twice: the plate
        is read once and then travels with every subsequent sighting of that vehicle,
        so a consumer never has to join sightings to reads itself.
        """
        out: list[Sighting] = []
        for idx, det in enumerate(detections):
            track_id = assignment.get(idx)
            if track_id is None:
                continue
            track = self._tracker.track_by_id(track_id)
            if track is None or not track.confirmed:
                continue
            read = reads_by_track.get(track_id) or self._ocr.known_read(track_id)
            out.append(
                Sighting(
                    camera_id=self.camera_id,
                    ts=ts,
                    track_id=track_id,
                    # The track's voted label, not this frame's. A single frame
                    # calling a car a truck should not produce a sighting that
                    # contradicts the forty around it.
                    class_label=track.class_label,
                    confidence=float(det.confidence),
                    bbox=det.bbox,
                    plate_text=read.text if read else None,
                    plate_text_raw=read.text_raw if read else None,
                    plate_confidence=read.confidence if read else None,
                    frame_pts=pts,
                    detector=self.detector_name,
                )
            )
        return out

    def _anpr_event(self, read: PlateRead, ts: datetime) -> PrimitiveEvent:
        return PrimitiveEvent(
            kind="anpr",
            camera_id=self.camera_id,
            ts=ts,
            track_id=read.track_id,
            payload={
                "plate_text": read.text,
                # Both forms, always. Normalisation never invents a character, so the
                # two differ only in separators — but an operator reviewing evidence
                # needs to see the machine's actual output, not a tidied version.
                "plate_text_raw": read.text_raw,
                "confidence": round(read.confidence, 4),
                "format_valid": read.format_valid,
                "sharpness": round(read.sharpness, 2),
                "engine": self.ocr_name,
                "bbox": [round(v, 5) for v in read.bbox.as_tuple()] if read.bbox else None,
            },
        )


def _sync_drops(stats: StageStats, source: dict[str, int]) -> None:
    """Copy a stage's cumulative drop counters into the stats block.

    Assignment rather than accumulation: the stage's own dict is already cumulative
    for the life of the stage, so adding would double-count on every frame. This is
    the kind of thing that silently inflates a reported drop count by a factor of a
    thousand and makes a funnel table look impossible.
    """
    for reason, count in source.items():
        stats.drops[reason] = count


def _reader_calls(reader: object) -> int:
    """Real inference count from the reader itself.

    Read off the backend rather than counted here, because the number that matters is
    how many times the *model* ran, and the stage's dedupe and quality gates mean
    that is not the same as how many crops it was handed. Both stub and Paddle
    backends expose ``calls``; a third-party reader that does not will report zero,
    which ``speedup_detail`` surfaces as an unpriced stage rather than as free work.
    """
    return int(getattr(reader, "calls", 0) or 0)
