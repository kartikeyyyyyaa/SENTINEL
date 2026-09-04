"""Worker entrypoint. One process per N cameras, one thread per camera.

**Why not one process per camera.** Fifty processes each holding its own copy of a
YOLO session is fifty copies of the weights and fifty interpreter heaps. On the
hardware a district can actually afford that is the difference between running and
not running. So: one process, several camera threads, one shared model.

**Why threads work despite the GIL.** Almost all of the wall time in a camera
thread is spent inside code that releases the GIL — the FFmpeg decode, the numpy
frame differencing, the ONNX/torch inference. The Python-level work between those
calls is small. Threads are also the only option that lets several cameras share one
model instance, which is the memory saving above.

**Why inference is serialised behind a lock.** Ultralytics models are not safe to
call concurrently from several threads, and even if they were, oversubscribing one
CPU with eight simultaneous inferences is slower in aggregate than running them one
at a time — the same work, plus cache thrash and scheduler churn. The lock makes
that explicit rather than accidental. What it costs is queueing, and queueing is
exactly what the adaptive sampler measures and responds to.

**Run modes.**

    python -m worker.main                 # from services/analytics, config from env
    python -m worker.main --dry-run       # synthetic frames, stub models, no network
    python -m worker.main --self-test     # dry run plus assertions on the cascade

``--dry-run`` and ``--self-test`` need no GPU, no model weights and no route to the
sandbox, which is what makes the pipeline testable on a development machine and in
CI. The measured numbers they print are computed by exactly the same code as the
live path, so the two are comparable.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from .capture import AdaptiveSampler, SyntheticSource, build_source
from .cascade import Cascade, CascadeStats, build_cascade
from .config import CameraSpec, WorkerConfig
from .sink import BufferedEventSink, JsonlTransport, build_sink
from .stages.detect import Detector, StubDetector, build_detector
from .stages.ocr import PlateReader, StubReader, build_reader

log = logging.getLogger("worker")


class SerialisedDetector:
    """One model, one caller at a time. See the module docstring.

    Also records queueing time, separately from inference time. Without that split,
    a worker that is oversubscribed looks identical in the stage timings to one
    whose model is slow, and the two call for opposite responses: fewer cameras per
    process versus a smaller model.
    """

    def __init__(self, inner: Detector) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.name = f"serialised:{getattr(inner, 'name', type(inner).__name__)}"
        self.wait_seconds = 0.0
        self.calls = 0

    def infer(self, image: Any) -> list[tuple[tuple[float, float, float, float], str, float]]:
        t0 = time.perf_counter()
        with self._lock:
            self.wait_seconds += time.perf_counter() - t0
            self.calls += 1
            return self._inner.infer(image)


class SerialisedReader:
    """Same argument as ``SerialisedDetector``, for the OCR engine."""

    def __init__(self, inner: PlateReader) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.name = f"serialised:{getattr(inner, 'name', type(inner).__name__)}"
        self.wait_seconds = 0.0

    def read(self, crop: Any) -> tuple[str, float] | None:
        t0 = time.perf_counter()
        with self._lock:
            self.wait_seconds += time.perf_counter() - t0
            return self._inner.read(crop)


@dataclass
class CameraWorker:
    """Drives one camera: capture, sample, cascade, emit.

    Every exception is caught and logged rather than propagated. One camera with a
    broken stream must not take down the other seven in the process, and on a
    fifty-camera live test at least one camera will be broken.
    """

    spec: CameraSpec
    config: WorkerConfig
    cascade: Cascade
    sink: BufferedEventSink
    sampler: AdaptiveSampler
    stop: threading.Event

    def run(self) -> None:
        source = build_source(self.spec, self.config.capture, stop=self.stop)
        log.info("camera %s starting (%s)", self.spec.camera_id, type(source).__name__)
        try:
            for frame in source.frames():
                if self.stop.is_set():
                    break
                if not self.sampler.should_process(frame.timing):
                    # Counted, not analysed. The cascade needs to know it happened
                    # so that its reduction factors stay relative to decoded frames.
                    self.cascade.offer(frame)
                    continue
                if frame.timing.discontinuity:
                    # The inserted segment gap is not an interval we spent compute
                    # on, and feeding it to the duty controller would look like a
                    # sudden abundance of headroom.
                    self.sampler.reset_timeline()
                t0 = time.perf_counter()
                try:
                    outcome = self.cascade.process_frame(frame)
                except Exception as exc:  # noqa: BLE001 - one frame, not the camera
                    log.exception("camera %s frame failed: %s", self.spec.camera_id, exc)
                    continue
                self.sampler.observe(time.perf_counter() - t0, frame.t)
                if outcome.events:
                    self.sink.emit_many(outcome.events)
                self.sink.flush()
        except Exception as exc:  # noqa: BLE001 - one camera, not the process
            log.exception("camera %s thread failed: %s", self.spec.camera_id, exc)
        finally:
            source.close()
            self._merge_capture_stats(source)
            log.info("camera %s stopped", self.spec.camera_id)

    def _merge_capture_stats(self, source: Any) -> None:
        stats = getattr(source, "stats", None)
        if stats is not None and hasattr(stats, "as_dict"):
            self.cascade.stats.capture = stats.as_dict()
        self.cascade.stats.sampler = self.sampler.as_dict()


class Worker:
    """A process's worth of cameras."""

    def __init__(self, config: WorkerConfig, dry_run: bool = False) -> None:
        config.validate()
        self.config = config
        self.dry_run = dry_run
        self.stop = threading.Event()
        self.sink = build_sink(config.sink)
        # Built once per process and shared. This is the whole reason the worker is
        # threaded rather than forked; see the module docstring.
        self.detector: Detector = SerialisedDetector(
            StubDetector() if dry_run else build_detector(config.detect)
        )
        self.reader: PlateReader = SerialisedReader(
            StubReader() if dry_run else build_reader(config.ocr)
        )
        self.cascades: dict[str, Cascade] = {}
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
        for spec in self.config.cameras:
            cascade = build_cascade(
                self.config,
                spec.camera_id,
                self.detector,
                self.reader,
                meters_per_pixel=spec.meters_per_pixel,
            )
            self.cascades[spec.camera_id] = cascade
            worker = CameraWorker(
                spec=spec,
                config=self.config,
                cascade=cascade,
                sink=self.sink,
                sampler=AdaptiveSampler(self.config.sampling),
                stop=self.stop,
            )
            self.workers.append(worker)
            thread = threading.Thread(
                target=worker.run, name=f"cam-{spec.camera_id}", daemon=True
            )
            self.threads.append(thread)
            thread.start()

    def run(self) -> dict[str, Any]:
        self.start()
        deadline = (
            time.monotonic() + self.config.run_seconds if self.config.run_seconds > 0 else None
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
                self.stop.wait(0.25)
        finally:
            self.stop.set()
            for thread in self.threads:
                thread.join(timeout=10.0)
            report = self.report()
            self.sink.close()
        return report

    def report(self) -> dict[str, Any]:
        """Log and return the measured cascade numbers.

        Reported on a timer rather than only at shutdown. A worker killed when the
        demo ends must still have reported the figures the submission needs, and
        "we had them but the process was SIGKILLed" is not a recoverable position.
        """
        per_camera = [c.snapshot_stats() for c in self.cascades.values()]
        report = aggregate_stats(per_camera)
        report["sink"] = self.sink.stats.as_dict()
        report["model_queueing_seconds"] = {
            "detector": round(getattr(self.detector, "wait_seconds", 0.0), 3),
            "reader": round(getattr(self.reader, "wait_seconds", 0.0), 3),
        }
        log.info("cascade report: %s", json.dumps(report, separators=(",", ":")))
        return report


def aggregate_stats(per_camera: list[CascadeStats]) -> dict[str, Any]:
    """Fleet-level rollup plus the per-camera detail.

    Both, deliberately. The rollup is what the cost-benefit section quotes; the
    per-camera detail is what makes it defensible, because a busy junction and a
    quiet lane average to a figure that describes neither and a district sizes
    hardware for the busy one. The per-camera minimum of ``realtime_factor`` is the
    number that actually bounds cameras-per-box.
    """
    stages: dict[str, dict[str, float]] = {}
    frames_offered = 0
    frames_entered = 0
    frames_skipped = 0
    events = 0
    compute = 0.0
    stream = 0.0
    for stats in per_camera:
        frames_offered += stats.frames_offered
        frames_entered += stats.frames_entered
        frames_skipped += stats.frames_skipped_by_sampler
        events += stats.events_emitted
        compute += stats.compute_seconds
        stream += stats.stream_seconds
        for stage in stats.stages:
            bucket = stages.setdefault(
                stage.name,
                {
                    "items_in": 0,
                    "items_out": 0,
                    "frames_reaching": 0,
                    "work_units": 0,
                    "frames_with_work": 0,
                    "seconds": 0.0,
                    "errors": 0,
                },
            )
            bucket["items_in"] += stage.items_in
            bucket["items_out"] += stage.items_out
            bucket["frames_reaching"] += stage.frames_reaching
            bucket["work_units"] += stage.work_units
            bucket["frames_with_work"] += stage.frames_with_work
            bucket["seconds"] += stage.seconds
            bucket["errors"] += stage.errors

    stage_rollup = []
    for name, bucket in stages.items():
        reaching = bucket["frames_reaching"]
        working = bucket["frames_with_work"]
        stage_rollup.append(
            {
                "name": name,
                "items_in": int(bucket["items_in"]),
                "items_out": int(bucket["items_out"]),
                "frames_reaching": int(reaching),
                "work_units": int(bucket["work_units"]),
                "frames_with_work": int(working),
                # The funnel percentage. From frames_with_work, so stage 4's
                # declined re-reads do not inflate it. See cascade.StageStats.
                "fraction_of_frames": (
                    round(working / frames_offered, 5) if frames_offered else 0.0
                ),
                "seconds": round(bucket["seconds"], 4),
                "mean_ms": round(bucket["seconds"] / reaching * 1000.0, 3) if reaching else 0.0,
                "cost_share": round(bucket["seconds"] / compute, 4) if compute else 0.0,
                "reduction_factor": (
                    round(bucket["items_in"] / bucket["items_out"], 3)
                    if bucket["items_out"]
                    else None
                ),
                "errors": int(bucket["errors"]),
            }
        )

    ocr = next((s for s in stage_rollup if s["name"] == "ocr"), None)
    realtime_factors = [s.realtime_factor for s in per_camera if s.compute_seconds > 0]
    return {
        "cameras": len(per_camera),
        "frames_offered": frames_offered,
        "frames_entered": frames_entered,
        "frames_skipped_by_sampler": frames_skipped,
        "events_emitted": events,
        "compute_seconds": round(compute, 4),
        "stream_seconds_analysed": round(stream, 3),
        # Streams of real time one core-equivalent of this machine can carry. The
        # sizing section divides the fleet by this.
        "realtime_factor_total": round(stream / compute, 3) if compute else 0.0,
        "realtime_factor_worst_camera": (
            round(min(realtime_factors), 3) if realtime_factors else 0.0
        ),
        "end_to_end_reduction": (
            round(frames_offered / ocr["frames_with_work"], 2)
            if ocr and ocr["frames_with_work"]
            else None
        ),
        "stages": stage_rollup,
        "per_camera": [s.as_dict() for s in per_camera],
    }


def dry_run_config(cameras: int = 2) -> WorkerConfig:
    """A no-network, no-weights configuration.

    Two cameras rather than one, so the shared-model lock and the shared sink are
    both actually exercised — a single-camera dry run would pass with a detector
    that is not thread-safe and a sink that is not.
    """
    base = WorkerConfig(
        worker_id="dry-run",
        cameras=tuple(
            CameraSpec(camera_id=f"cam-{i:02d}", url="stub://", stub=True, meters_per_pixel=0.05)
            for i in range(cameras)
        ),
        stats_interval_seconds=1e9,  # Report once, at the end.
    )
    base.validate()
    return base


def run_dry(frames: int = 240) -> dict[str, Any]:
    """Drive the real cascade over synthetic frames, in-process and single-threaded.

    Single-threaded and synchronous on purpose: this is the path the self-test and
    the unit tests use, and a deterministic frame count with no thread scheduling
    makes the reported reduction factors reproducible. Reproducibility matters
    because these numbers go into a submission.
    """
    config = dry_run_config()
    detector = StubDetector()
    reader = StubReader()
    sink = BufferedEventSink(JsonlTransport(""), config.sink)
    all_stats: list[CascadeStats] = []
    for spec in config.cameras:
        cascade = build_cascade(
            config, spec.camera_id, detector, reader, meters_per_pixel=spec.meters_per_pixel
        )
        sampler = AdaptiveSampler(config.sampling)
        source = SyntheticSource(
            camera_id=spec.camera_id,
            frame_count=frames,
            loop_at=frames // 2,  # Exercise the loop cut and the tracker reset.
            config=config.capture,
        )
        for frame in source.frames():
            if not sampler.should_process(frame.timing):
                cascade.offer(frame)
                continue
            if frame.timing.discontinuity:
                sampler.reset_timeline()
            t0 = time.perf_counter()
            outcome = cascade.process_frame(frame)
            sampler.observe(time.perf_counter() - t0, frame.t)
            sink.emit_many(outcome.events)
        cascade.stats.sampler = sampler.as_dict()
        cascade.stats.capture = source.stats.as_dict()
        all_stats.append(cascade.snapshot_stats())
    sink.flush(force=True)
    report = aggregate_stats(all_stats)
    report["sink"] = sink.stats.as_dict()
    return report


def self_test(frames: int = 240) -> int:
    """Dry run plus the assertions that matter. Returns a process exit code.

    Checks the two properties that would invalidate the submission's numbers if
    they silently stopped holding: that the cascade actually narrows, and that the
    counters are arithmetically consistent. A cascade that passes every frame to
    every stage would still "work" and would still emit events; it would just have
    no efficiency argument left, and nothing else would notice.
    """
    report = run_dry(frames)
    print(json.dumps(report, indent=2, default=str))
    problems: list[str] = []
    stages = {s["name"]: s for s in report["stages"]}
    if report["frames_offered"] <= 0:
        problems.append("no frames were offered")
    for earlier, later in (("motion", "detect"), ("detect", "plate"), ("plate", "ocr")):
        if earlier in stages and later in stages:
            if stages[later]["frames_with_work"] > stages[earlier]["frames_with_work"]:
                problems.append(f"{later} did work on more frames than {earlier}: not a cascade")
    if "ocr" in stages and stages["ocr"]["frames_with_work"] >= report["frames_offered"]:
        problems.append("ocr ran on every frame: the cascade is not narrowing")
    for name, stage in stages.items():
        if stage["items_out"] > stage["items_in"] and name != "detect":
            # detect legitimately fans out: one frame in, several boxes out.
            problems.append(f"{name} produced more items than it consumed")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="worker.main",
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
    config = WorkerConfig.from_env()
    logging.basicConfig(
        level=(args.log_level or config.log_level).upper(),
        format="%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s",
        # stderr, so that stdout stays a clean JSONL event stream when the sink is
        # unconfigured and something downstream is piping it.
        stream=sys.stderr,
    )

    if args.self_test:
        return self_test(args.frames)

    if args.dry_run:
        report = run_dry(args.frames)
        print(json.dumps(report, indent=2, default=str))
        _write_report(args.stats_json, report)
        return 0

    if args.cameras:
        config = config.with_cameras(
            tuple(CameraSpec.parse(s) for s in args.cameras.split(",") if s.strip())
        )
    if args.duration > 0:
        from dataclasses import replace  # noqa: PLC0415 - local, one use

        config = replace(config, run_seconds=args.duration)
    if args.shard >= 0:
        shards = config.shards()
        if args.shard >= len(shards):
            raise SystemExit(f"shard {args.shard} out of range: {len(shards)} shard(s)")
        config = config.with_cameras(shards[args.shard])
        log.info("running shard %d of %d: %d camera(s)", args.shard, len(shards), len(config.cameras))

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
