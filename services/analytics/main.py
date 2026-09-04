"""Worker entrypoint. One process per N cameras, one thread per camera.

**Why not one process per camera.** Fifty processes each holding their own copy of
a detector session is fifty copies of the weights and fifty interpreter heaps. On
hardware a district can actually afford, that is the difference between running
and not running. So: one process, several camera threads, one shared detector and
one shared OCR reader.

**Why threads work despite the GIL.** Almost all of the wall time in a camera
thread is spent inside code that releases the GIL — the FFmpeg decode via OpenCV,
the numpy frame differencing, the ONNX/torch/Paddle inference. The Python-level
work between those calls is small. Threads are also the only option that lets
several cameras share one model instance, which is the memory saving above.

**Why inference is serialised behind a lock.** Ultralytics and PaddleOCR sessions
are not safe to call concurrently from several threads, and even if they were,
oversubscribing one CPU with eight simultaneous inferences is slower in aggregate
than running them one at a time — the same work, plus cache thrash and scheduler
churn. ``SerialisedDetector`` / ``SerialisedReader`` make that explicit rather than
accidental. What it costs is queueing, and queueing is exactly what the adaptive
sampler (inside each camera's own ``source.FrameSource``) measures and responds to
by widening that camera's stride.

**What owns what.** Each camera gets its own ``Cascade`` (motion, detect, plate,
ocr and the tracker — see ``cascade.py``) and its own ``PrimitiveEngine`` (zones,
lines, speed, crowd, abandoned, proximity — see ``primitives.py``), because both
hold per-camera state that a scene-change reset must clear without disturbing any
other camera. Frame capture, PTS-derived timing and adaptive frame shedding all
live inside the camera's ``source.FrameSource`` already and are not duplicated
here; this module's job is to drive that generator, run each accepted frame
through the cascade and the primitive engine, and hand the resulting events to the
sink.

**Run modes.**

    python -m services.analytics.main                 # config from environment
    python -m services.analytics.main --dry-run        # synthetic frames, stub models, no network
    python -m services.analytics.main --self-test      # dry run plus assertions on the cascade

``--dry-run`` and ``--self-test`` need no GPU, no model weights and no route to the
sandbox, which is what makes the pipeline testable on a development machine and in
CI. The measured numbers they print are computed by exactly the same ``Cascade``
and ``PrimitiveEngine`` code as the live path, so the two are comparable.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from services.common.alerts import Alert
from services.common.events import PrimitiveEvent

from .cascade import Cascade, CascadeStats
from .config import CameraConfig, WorkerConfig, Zone, resolve_config
from .primitives import PrimitiveEngine
from .rules import RuleConfig, RuleEngine
from .sink import (
    AlertSink,
    BufferedEventSink,
    MemoryAlertTransport,
    MemoryTransport,
    build_alert_sink,
    build_sink,
)
from .source import Frame, FrameSource, SyntheticSource, build_source
from .stages.detect import Detector, StubDetector, build_detector, validate_class_map
from .stages.face import FaceConfig, FaceMatcher, FaceStage, build_face_matcher
from .stages.plate import OcrReader, StubOcrReader, build_reader
from .watchlist import WatchlistIndex, WatchlistRefresher, resolve_watchlist

log = logging.getLogger("services.analytics.main")


class SerialisedDetector:
    """One model, one caller at a time. See the module docstring.

    Also records queueing time, separately from inference time. Without that
    split, a worker that is oversubscribed looks identical in the stage timings to
    one whose model is slow, and the two call for opposite responses: fewer
    cameras per process versus a smaller model.
    """

    def __init__(self, inner: Detector) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.name = f"serialised:{getattr(inner, 'name', type(inner).__name__)}"
        self.wait_seconds = 0.0

    def detect(self, frame: Any, frame_index: int = 0) -> list:
        t0 = time.perf_counter()
        with self._lock:
            self.wait_seconds += time.perf_counter() - t0
            return self._inner.detect(frame, frame_index)


class SerialisedReader:
    """Same argument as ``SerialisedDetector``, for the OCR engine.

    ``calls`` is delegated rather than re-derived, because ``cascade.Cascade``
    reads it straight off ``PlateReadStage.reader`` to count real inferences for
    the funnel report — see ``cascade._reader_calls``. A wrapper that did not
    expose it would make every camera's OCR cost look unpriced.
    """

    def __init__(self, inner: OcrReader) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.name = f"serialised:{getattr(inner, 'name', type(inner).__name__)}"
        self.wait_seconds = 0.0
        self.calls = 0

    def read(self, image: Any) -> tuple[str, float]:
        t0 = time.perf_counter()
        with self._lock:
            self.wait_seconds += time.perf_counter() - t0
            self.calls += 1
            return self._inner.read(image)


class SerialisedFaceMatcher:
    """Same argument as ``SerialisedDetector``/``SerialisedReader``, for the face
    embedding backend — a real ONNX session is no more safe to call from several
    threads at once than the detector or the OCR reader are."""

    def __init__(self, inner: FaceMatcher) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.name = f"serialised:{getattr(inner, 'name', type(inner).__name__)}"
        self.wait_seconds = 0.0

    def embed(self, image: Any):
        t0 = time.perf_counter()
        with self._lock:
            self.wait_seconds += time.perf_counter() - t0
            return self._inner.embed(image)


@dataclass
class CameraWorker:
    """Drives one camera: capture, cascade, primitives, emit.

    Every exception is caught and logged rather than propagated. One camera with a
    broken stream must not take down the other seven in the process, and on a
    fifty-camera live test at least one camera will be broken.
    """

    camera: CameraConfig
    config: WorkerConfig
    cascade: Cascade
    primitives: PrimitiveEngine
    sink: BufferedEventSink
    stop: threading.Event
    #: Shared across cameras (see ``Worker.__init__``); ``rules`` carries its own
    #: per-camera-keyed cooldown state so one instance is correct to share, the
    #: same reasoning as the shared detector and OCR reader above. ``face_stage``
    #: is per-camera because its dedupe cache is keyed on bare track ids, which
    #: are only unique within one camera — same reasoning as
    #: ``PlateReadStage``'s per-camera instantiation in ``cascade.Cascade``.
    rules: RuleEngine
    face_stage: FaceStage
    alert_sink: AlertSink

    def run(self) -> None:
        source = build_source(self.camera, self.config.source, self.config.sampling)
        log.info(
            "camera %s starting (%s)", self.camera.camera_id, type(source).__name__
        )
        watchdog = threading.Thread(
            target=self._watch_stall,
            args=(source,),
            name=f"cam-{self.camera.camera_id}-watchdog",
            daemon=True,
        )
        watchdog.start()
        try:
            for frame in source.frames():
                if self.stop.is_set():
                    break
                self._handle_frame(frame)
        except Exception as exc:  # noqa: BLE001 - one camera, not the process
            log.exception("camera %s thread failed: %s", self.camera.camera_id, exc)
        finally:
            source.close()
            watchdog.join(timeout=5.0)
            log.info("camera %s stopped", self.camera.camera_id)

    def _handle_frame(self, frame: Frame) -> None:
        timing = frame.timing
        events: list[PrimitiveEvent] = []

        if frame.gap_seconds is not None:
            # The worker was not watching for this many seconds of wall clock: a
            # reconnect, not a scene event. See Frame.gap_seconds's docstring —
            # "the camera saw nothing" and "the worker was not watching" are
            # different facts and a rule reasoning about a missed exit needs both.
            events.append(self._gap_event(timing.ts, frame.gap_seconds, "reconnect"))

        if timing.discontinuity:
            # The sandbox feed looped, or we reconnected at a different point in
            # it. Reset the cascade (motion reference, tracker, OCR dedupe) and the
            # primitive engine (zone/line/pair state) together, in that order,
            # before this frame is processed against either.
            events.extend(self.cascade.reset_scene(timing.pts, timing.ts))
            self.primitives.reset()
            self.face_stage.reset()

        try:
            outcome = self.cascade.process(
                frame.image,
                t=timing.pts,
                ts=timing.ts,
                pts=timing.pts,
                frame_index=timing.frame_index,
            )
        except Exception as exc:  # noqa: BLE001 - one frame, not the camera
            log.exception("camera %s frame failed: %s", self.camera.camera_id, exc)
            if events:
                self.sink.emit_many(events)
            return

        events.extend(outcome.events)
        alerts: list[Alert] = []
        if outcome.analysed:
            # Primitives only see frames the cascade actually looked at. A frame
            # the motion gate dropped produced no tracks to reason about, and
            # feeding it an empty track list would just churn the pair-state
            # bookkeeping for zero benefit.
            events.extend(self.primitives.update(outcome.tracks, timing.pts, timing.ts))

            # Person-watchlist matching. Runs directly against the frame here,
            # not through a primitive event — see the module docstring on
            # watchlist.py for why an embedding never travels as wire metadata.
            # Same "only frames the cascade analysed" gate as primitives, for the
            # same reason: a dropped frame produced no tracks to match.
            for track in outcome.tracks:
                if track.class_label != "person":
                    continue
                embedding = self.face_stage.embed_person(frame.image, track.track_id, track.bbox)
                if embedding is None:
                    continue
                match = self.rules.watchlist.match_face(embedding)
                if match is None:
                    continue
                entry, similarity = match
                alerts.append(
                    self.rules.person_watchlist_alert(
                        self.camera, timing.ts, track.track_id, entry, similarity
                    )
                )

            # Every other rule reads only the primitives already computed above —
            # anpr, proximity, dwell, zone_enter, crowd, abandoned, speed. See
            # rules.RuleEngine.evaluate.
            alerts.extend(self.rules.evaluate(self.camera, events, outcome.tracks, timing.pts))
        else:
            # Even an unanalysed frame can carry a gap/scene_change primitive
            # (appended above), and a prolonged gap is itself alert-worthy —
            # rule_stream_gap does not require outcome.analysed.
            alerts.extend(self.rules.evaluate(self.camera, events, (), timing.pts))

        if events:
            self.sink.emit_many(events)
        self.sink.flush()
        if alerts:
            self.alert_sink.emit_many(alerts)
        self.alert_sink.flush()

    def _watch_stall(self, source: FrameSource) -> None:
        """Poll for a stalled stream from outside the (blocking) frame loop.

        ``frames()`` is blocked inside a socket read while stalled and cannot
        notice its own silence; only a second observer can. See
        ``source.RtspSource.stall``'s docstring. ``SyntheticSource`` has no
        ``stall`` attribute, and this simply has nothing to do for one.
        """
        stall = getattr(source, "stall", None)
        if stall is None:
            return
        while not self.stop.is_set():
            duration = stall.check(time.monotonic())
            if duration is not None:
                gap_event = self._gap_event(datetime.now(timezone.utc), duration, "stalled")
                self.sink.emit(gap_event)
                alerts = self.rules.evaluate(self.camera, [gap_event], (), time.monotonic())
                if alerts:
                    self.alert_sink.emit_many(alerts)
                    self.alert_sink.flush()
            if self.stop.wait(2.0):
                break

    def _gap_event(self, ts: datetime, seconds: float, reason: str) -> PrimitiveEvent:
        return PrimitiveEvent(
            kind="stream_gap",
            camera_id=self.camera.camera_id,
            ts=ts,
            payload={"reason": reason, "gap_seconds": round(seconds, 2)},
        )


class Worker:
    """A process's worth of cameras."""

    def __init__(self, config: WorkerConfig) -> None:
        config.validate()
        self.config = config
        self.stop = threading.Event()
        self.sink = build_sink(config.sink)
        self.alert_sink = build_alert_sink(config.sink)
        # Built once per process and shared. This is the whole reason the worker
        # is threaded rather than forked; see the module docstring.
        self.detector: Detector = SerialisedDetector(build_detector(config.detect))
        self.reader: OcrReader = SerialisedReader(build_reader(config.ocr))
        self.face_config = FaceConfig.from_env()
        self.face_matcher: FaceMatcher = SerialisedFaceMatcher(build_face_matcher(self.face_config))
        # The watchlist: loaded once at start, then refreshed on a background
        # thread — see watchlist.WatchlistRefresher. RuleEngine holds the same
        # index object, so a refresh takes effect for every camera on its next
        # frame with no restart and no lock the hot path has to take.
        self.watchlist: WatchlistIndex = resolve_watchlist(
            json_path=config.watchlist_path,
            registry_url=config.registry_url,
            registry_token=config.registry_token,
            face_threshold=self.face_config.match_threshold,
        )
        self.watchlist_refresher: WatchlistRefresher | None = None
        if config.registry_url and not config.watchlist_path:
            self.watchlist_refresher = WatchlistRefresher(
                index=self.watchlist,
                base_url=config.registry_url,
                token=config.registry_token,
                interval_seconds=config.watchlist_refresh_seconds,
            )
        self.rules = RuleEngine(RuleConfig.from_env(), self.watchlist)
        self.cascades: dict[int, Cascade] = {}
        self.workers: list[CameraWorker] = []
        self.threads: list[threading.Thread] = []

    def install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: Any) -> None:
            log.info("signal %s received, stopping", signum)
            self.stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    def start(self) -> None:
        if not self.config.cameras:
            raise SystemExit(
                "no cameras configured. Set ANALYTICS_CAMERAS to a comma-separated "
                "list of camera_id=url pairs, or pass --cameras, or use --dry-run."
            )
        for camera in self.config.cameras:
            cascade = Cascade(
                camera.camera_id, self.config, detector=self.detector, reader=self.reader
            )
            self.cascades[camera.camera_id] = cascade
            worker = CameraWorker(
                camera=camera,
                config=self.config,
                cascade=cascade,
                primitives=PrimitiveEngine(camera, self.config.primitives),
                sink=self.sink,
                stop=self.stop,
                rules=self.rules,
                face_stage=FaceStage(self.face_config, self.face_matcher),
                alert_sink=self.alert_sink,
            )
            self.workers.append(worker)
            thread = threading.Thread(
                target=worker.run, name=f"cam-{camera.camera_id}", daemon=True
            )
            self.threads.append(thread)
            thread.start()
        if self.watchlist_refresher is not None:
            self.watchlist_refresher.start()

    def run(self) -> dict[str, Any]:
        self.start()
        deadline = (
            time.monotonic() + self.config.run_seconds
            if self.config.run_seconds > 0
            else None
        )
        next_report = time.monotonic() + self.config.stats_interval_seconds
        try:
            while not self.stop.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    log.info("run duration reached, stopping")
                    break
                if not any(t.is_alive() for t in self.threads):
                    break
                if time.monotonic() >= next_report:
                    next_report = time.monotonic() + self.config.stats_interval_seconds
                    self.report()
                self.sink.flush()
                self.alert_sink.flush()
                self.stop.wait(0.25)
        finally:
            self.stop.set()
            for thread in self.threads:
                thread.join(timeout=10.0)
            if self.watchlist_refresher is not None:
                self.watchlist_refresher.stop()
            report = self.report()
            self.sink.close()
            self.alert_sink.close()
        return report

    def report(self) -> dict[str, Any]:
        """Log and return the measured cascade numbers, fleet-wide and per camera.

        Reported on a timer rather than only at shutdown. A worker killed when the
        demo ends must still have reported the figures the submission needs, and
        "we had them but the process was SIGKILLed" is not a recoverable position.
        """
        fleet = CascadeStats()
        per_camera: list[dict[str, Any]] = []
        tracker_totals = {
            "tracks_started": 0,
            "tracks_ended": 0,
            "id_switches_prevented": 0,
            "scene_resets": 0,
        }
        for camera_id, cascade in self.cascades.items():
            fleet.merge(cascade.stats)
            snapshot = cascade.stats.as_dict()
            snapshot["camera_id"] = camera_id
            per_camera.append(snapshot)
            tracker = cascade.tracker
            tracker_totals["tracks_started"] += tracker.tracks_started
            tracker_totals["tracks_ended"] += tracker.tracks_ended
            tracker_totals["id_switches_prevented"] += tracker.id_switches_prevented
            tracker_totals["scene_resets"] += tracker.scene_resets

        report = fleet.as_dict()
        report["cameras"] = len(self.cascades)
        report["tracker"] = tracker_totals
        report["sink"] = self.sink.stats.as_dict()
        report["alerts"] = {
            "delivered": self.alert_sink.delivered,
            "dropped": self.alert_sink.dropped,
        }
        report["watchlist_entries"] = self.watchlist.size
        report["model_queueing_seconds"] = {
            "detector": round(getattr(self.detector, "wait_seconds", 0.0), 3),
            "reader": round(getattr(self.reader, "wait_seconds", 0.0), 3),
            "face": round(getattr(self.face_matcher, "wait_seconds", 0.0), 3),
        }
        report["per_camera"] = per_camera
        log.info("cascade report: %s", json.dumps(report, separators=(",", ":"), default=str))
        return report


def dry_run_config(cameras: int = 2) -> WorkerConfig:
    """A no-network, no-weights configuration.

    Two cameras rather than one, so the shared-model lock and the shared sink are
    both actually exercised — a single-camera dry run would pass with a detector
    that is not thread-safe and a sink that is not, and never notice.
    """
    config = WorkerConfig(
        worker_id="dry-run",
        cameras=tuple(
            CameraConfig(camera_id=i, url="stub://", stub=True) for i in range(1, cameras + 1)
        ),
        stats_interval_seconds=1e9,  # Report once, at the end.
    )
    config.validate()
    return config


def run_dry(frames: int = 240) -> dict[str, Any]:
    """Drive the real cascade and primitive engine over synthetic frames.

    Single-threaded and synchronous on purpose: this is the path the self-test and
    the unit tests use, and a deterministic frame count with no thread scheduling
    makes the reported reduction factors reproducible. Reproducibility matters
    because these numbers go into a submission. Events go to an in-memory
    transport rather than stdout, so running this from a test suite does not spam
    the test runner's output.
    """
    config = dry_run_config()
    detector = StubDetector(config.detect)
    reader = StubOcrReader()  # takes an optional confidence float, not an OcrConfig
    sink = BufferedEventSink(MemoryTransport(), config.sink)

    fleet = CascadeStats()
    per_camera: list[dict[str, Any]] = []
    tracker_totals = {
        "tracks_started": 0,
        "tracks_ended": 0,
        "id_switches_prevented": 0,
        "scene_resets": 0,
    }
    for camera in config.cameras:
        cascade = Cascade(camera.camera_id, config, detector=detector, reader=reader)
        primitives = PrimitiveEngine(camera, config.primitives)
        source = SyntheticSource(
            camera.camera_id,
            total_frames=frames,
            loop_frames=frames // 2,  # Exercise the loop cut and the tracker reset.
            source_config=config.source,
            sampling=config.sampling,
        )
        for frame in source.frames():
            timing = frame.timing
            events: list[PrimitiveEvent] = []
            if timing.discontinuity:
                events.extend(cascade.reset_scene(timing.pts, timing.ts))
                primitives.reset()
            outcome = cascade.process(
                frame.image,
                t=timing.pts,
                ts=timing.ts,
                pts=timing.pts,
                frame_index=timing.frame_index,
            )
            events.extend(outcome.events)
            if outcome.analysed:
                events.extend(primitives.update(outcome.tracks, timing.pts, timing.ts))
            sink.emit_many(events)
        fleet.merge(cascade.stats)
        snapshot = cascade.stats.as_dict()
        snapshot["camera_id"] = camera.camera_id
        per_camera.append(snapshot)
        tracker = cascade.tracker
        tracker_totals["tracks_started"] += tracker.tracks_started
        tracker_totals["tracks_ended"] += tracker.tracks_ended
        tracker_totals["id_switches_prevented"] += tracker.id_switches_prevented
        tracker_totals["scene_resets"] += tracker.scene_resets

    sink.flush(force=True)
    report = fleet.as_dict()
    report["cameras"] = len(config.cameras)
    report["tracker"] = tracker_totals
    report["sink"] = sink.stats.as_dict()
    report["per_camera"] = per_camera
    return report


def self_test(frames: int = 240) -> int:
    """Dry run plus the assertions that matter. Returns a process exit code.

    Checks the *structural* properties that would invalidate the submission's
    numbers if they silently stopped holding: that the cascade actually narrows,
    and that OCR is doing real, bounded work rather than none or all of it. Those
    are deterministic given a fixed synthetic input and a fixed configuration, so
    a failure here means the pipeline itself is wrong.

    ``effective_speedup`` is printed but deliberately not asserted on. It is a
    wall-clock measurement, and on this dry run every backend is a stub costing
    microseconds — the "hypothetical" and "measured" totals it divides are both
    dominated by scheduler jitter and CPU frequency scaling rather than by any
    real per-unit model cost, so the ratio can land on either side of 1.0 between
    two runs on the same unchanged code. That noise is a property of measuring
    real time with fake, near-zero-cost work, not a regression; the number means
    something once the detector and OCR backends are the real ones, and this
    dry run's cascade-narrowing checks below cover the property that must always
    hold regardless of backend speed.
    """
    validate_class_map()
    report = run_dry(frames)
    print(json.dumps(report, indent=2, default=str))

    problems: list[str] = []
    funnel = report["funnel"]
    stages = {s["name"]: s for s in funnel["stages"]}

    if funnel["frames_offered"] <= 0:
        problems.append("no frames were offered")
    if funnel["frames_analysed"] <= 0:
        problems.append("the motion gate passed nothing; the cascade never started")

    if "ocr" in stages:
        ocr = stages["ocr"]
        if ocr["cumulative_ratio"] >= 1.0:
            problems.append("ocr ran on every frame: the cascade is not narrowing")
        if ocr["items_out"] <= 0:
            problems.append("ocr never produced a read on the dry run; nothing was exercised")

    problems.extend(self_test_rules())

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


def self_test_rules() -> list[str]:
    """Deterministic, no-network checks for the correlation layer added in
    ``rules.py``/``watchlist.py``: a watchlist match fires for a listed plate and
    (with the stub face backend) a listed face, and the women's-safety composite
    rule fires on both of its documented patterns.

    Kept separate from ``run_dry`` because these check *logical* wiring — does a
    known, hand-built input produce the expected alert — rather than measured
    cascade throughput, and folding the two together would make a rules
    regression look like a cascade one, or vice versa.
    """
    import numpy as np

    from .stages.face import StubFaceMatcher
    from .watchlist import WatchlistEntry, WatchlistIndex

    problems: list[str] = []
    # 2026-09-03 23:00 UTC = 2026-09-04 04:30 IST: inside the default night
    # window (21:00-05:00 IST), which the "followed" pattern below depends on.
    ts = datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)

    # -- vehicle watchlist ----------------------------------------------------
    vehicle_watchlist = WatchlistIndex([
        WatchlistEntry(
            entry_id="v1", entry_type="stolen_vehicle", risk_level="critical",
            plate_number="GJ01AB1234", label="self-test fixture",
        )
    ])
    vehicle_rules = RuleEngine(RuleConfig(), vehicle_watchlist)
    camera1 = CameraConfig(camera_id=101, url="stub://", stub=True)
    anpr_event = PrimitiveEvent(
        kind="anpr", camera_id=101, ts=ts, track_id="t1",
        payload={"plate_text": "GJ 01 AB 1234"},  # unnormalised, as OCR emits it
    )
    hits = vehicle_rules.evaluate(camera1, [anpr_event], (), 0.0)
    if not any(a.kind == "watchlist_match_vehicle" for a in hits):
        problems.append("vehicle watchlist match did not fire for a listed plate")

    # -- person watchlist, stub face backend -----------------------------------
    matcher = StubFaceMatcher(embed_dim=16)
    listed_embedding = matcher.embed(np.full((60, 60, 3), 200, dtype=np.uint8))
    if listed_embedding is None:
        problems.append("stub face matcher produced no embedding for a valid crop")
    else:
        person_watchlist = WatchlistIndex([
            WatchlistEntry(
                entry_id="p1", entry_type="wanted_person", risk_level="high",
                embedding=listed_embedding, label="self-test fixture",
            )
        ])
        if person_watchlist.match_face(listed_embedding) is None:
            problems.append("face watchlist match did not fire for an identical embedding")
        unlisted_embedding = matcher.embed(np.zeros((60, 60, 3), dtype=np.uint8))
        if unlisted_embedding is not None and person_watchlist.match_face(unlisted_embedding) is not None:
            problems.append("face watchlist matched two dissimilar crops; threshold is not discriminating")

    # -- women's safety: encircled (one track, multiple simultaneous partners) --
    encircle_rules = RuleEngine(RuleConfig(), WatchlistIndex([]))
    camera2 = CameraConfig(camera_id=102, url="stub://", stub=True)
    encircle_events = [
        PrimitiveEvent(kind="proximity", camera_id=102, ts=ts, track_id="a",
                        payload={"track_ids": ["a", "b"], "class_labels": ["person", "person"]}),
        PrimitiveEvent(kind="proximity", camera_id=102, ts=ts, track_id="a",
                        payload={"track_ids": ["a", "c"], "class_labels": ["person", "person"]}),
    ]
    encircle_alerts = encircle_rules.evaluate(camera2, encircle_events, (), 0.0)
    if not any(a.kind == "women_safety_risk" and a.detail.get("pattern") == "encircled" for a in encircle_alerts):
        problems.append("women-safety 'encircled' pattern did not fire for a track with two simultaneous partners")

    # -- women's safety: followed, alone, in a flagged zone after dark --------
    follow_rules = RuleEngine(RuleConfig(), WatchlistIndex([]))
    camera3 = CameraConfig(
        camera_id=103, url="stub://", stub=True,
        zones=(Zone(zone_id="lane", polygon=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)), isolated=True),),
    )
    follow_events = [
        PrimitiveEvent(kind="zone_enter", camera_id=103, ts=ts, track_id="x",
                        payload={"zone_id": "lane", "class_label": "person"}),
        PrimitiveEvent(kind="proximity", camera_id=103, ts=ts, track_id="x",
                        payload={"track_ids": ["x", "y"], "class_labels": ["person", "person"],
                                 "seconds": 8.0, "mean_heading_correlation": 0.92}),
    ]
    follow_alerts = follow_rules.evaluate(camera3, follow_events, (), 0.0)
    if not any(a.kind == "women_safety_risk" and a.detail.get("pattern") == "followed" for a in follow_alerts):
        problems.append("women-safety 'followed' pattern did not fire in an isolated zone after dark")

    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="services.analytics.main",
        description="Sentinel edge analytics worker. Consumes RTSP, emits metadata.",
    )
    parser.add_argument(
        "--cameras",
        default="",
        help="comma-separated camera_id=rtsp://... pairs, overriding ANALYTICS_CAMERAS",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="synthetic frames and stub models; no network, no weights, no GPU",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="dry run, then assert the cascade actually narrows; exits non-zero if not",
    )
    parser.add_argument("--frames", type=int, default=240, help="frames per dry-run camera")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="stop after this many seconds (0 = forever)"
    )
    parser.add_argument(
        "--shard",
        type=int,
        default=-1,
        help="run only shard N of the camera list, split by max_cameras_per_process",
    )
    parser.add_argument("--stats-json", default="", help="write the final report to this path")
    parser.add_argument("--log-level", default="", help="override LOG_LEVEL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.self_test:
        # No env, no network: matches --dry-run's isolation. Logging is not set up
        # yet at this point on purpose, so a self-test run's only output is the
        # JSON report and any FAIL lines.
        return self_test(args.frames)

    config = resolve_config(args.cameras)
    logging.basicConfig(
        level=(args.log_level or config.log_level).upper(),
        format="%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s",
        # stderr, so that stdout stays a clean JSONL event stream when the sink is
        # unconfigured and something downstream is piping it.
        stream=sys.stderr,
    )

    if args.dry_run:
        report = run_dry(args.frames)
        print(json.dumps(report, indent=2, default=str))
        _write_report(args.stats_json, report)
        return 0

    if args.duration > 0:
        config = replace(config, run_seconds=args.duration)
    if args.shard >= 0:
        shards = config.shards()
        if args.shard >= len(shards):
            raise SystemExit(f"shard {args.shard} out of range: {len(shards)} shard(s)")
        config = config.with_cameras(shards[args.shard])
        log.info(
            "running shard %d of %d: %d camera(s)",
            args.shard, len(shards), len(config.cameras),
        )

    worker = Worker(config)
    worker.install_signal_handlers()
    report = worker.run()
    _write_report(args.stats_json, report)
    return 0


def _write_report(path: str, report: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
    log.info("wrote cascade report to %s", path)


if __name__ == "__main__":
    raise SystemExit(main())
