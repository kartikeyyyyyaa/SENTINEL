"""Worker configuration, loaded from environment.

Plain dataclasses rather than pydantic. The registry service already pays for
pydantic because it validates untrusted request bodies; this worker validates
only operator-supplied environment, and one fewer import is one fewer thing that
fails to install on the edge box the night before the demo.

Configuration is grouped per cascade stage, and every stage carries its own
``enabled`` flag. That is not decoration: the sizing numbers in the submission
are produced by running the same binary with stages switched off and comparing
throughput, so "disable stage 4 and re-measure" has to be a config change rather
than a code change.

Two knobs deserve their own explanation because they are the difference between
a worker that survives 50 streams and one that does not:

* ``SamplingConfig`` — the worker raises its own frame stride when it detects it
  is falling behind real time. See ``capture.AdaptiveSampler``.
* ``CaptureConfig.backoff_*`` — a reconnect storm from 50 workers hitting a
  wobbling gateway is indistinguishable from an attack. Jittered exponential
  backoff keeps us a good citizen on someone else's sandbox.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# Ordered stage names. The cascade is a funnel and the order is the whole point:
# each stage is roughly an order of magnitude more expensive than the one before,
# so each must see far fewer items than the one before.
STAGE_ORDER: tuple[str, ...] = ("motion", "detect", "plate", "ocr")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class MotionConfig:
    """Stage 1. Cheap frame differencing, run on every decoded frame.

    Everything here is chosen to keep the gate itself near-free. Differencing at
    full resolution costs more than the gate saves on a quiet camera, so frames
    are shrunk to ``work_width`` first; at 320 px wide a 1080p frame is ~20x less
    memory traffic and the motion signal we care about (a vehicle, a person)
    survives the downscale intact.
    """

    enabled: bool = True
    work_width: int = 320
    blur_kernel: int = 5  # Suppresses sensor noise, which otherwise trips the gate at night.
    diff_threshold: int = 18  # Per-pixel absolute difference, 0-255.
    min_area_fraction: float = 0.0015  # Fraction of the downscaled frame that must change.
    # Frames to observe before the gate is allowed to block anything. The first
    # frames after a connect are a replayed GOP with no usable reference.
    warmup_frames: int = 3
    # A gate that has blocked this long forces one frame through anyway, so a
    # slow-moving scene (traffic at a standstill, a stationary crowd) still
    # produces periodic detections instead of going silent.
    max_gap_seconds: float = 5.0

    @staticmethod
    def from_env() -> MotionConfig:
        return MotionConfig(
            enabled=_env_bool("ANALYTICS_MOTION_ENABLED", True),
            work_width=_env_int("ANALYTICS_MOTION_WORK_WIDTH", 320),
            blur_kernel=_env_int("ANALYTICS_MOTION_BLUR", 5),
            diff_threshold=_env_int("ANALYTICS_MOTION_THRESHOLD", 18),
            min_area_fraction=_env_float("ANALYTICS_MOTION_MIN_AREA_FRACTION", 0.0015),
            warmup_frames=_env_int("ANALYTICS_MOTION_WARMUP_FRAMES", 3),
            max_gap_seconds=_env_float("ANALYTICS_MOTION_MAX_GAP_SECONDS", 5.0),
        )


@dataclass(frozen=True, slots=True)
class DetectConfig:
    """Stage 2. YOLOv8n, int8, only on frames the gate let through.

    ``input_size`` is a single number because the model wants a square input, and
    the streams do not supply one: the sandbox mixes H.264 with H.265 and mixes
    resolutions, so there is no fixed-shape batch to be had. Every frame is
    letterboxed to this size individually. See ``stages.detect.letterbox``.
    """

    enabled: bool = True
    model_path: str = "models/yolov8n.onnx"
    input_size: int = 640
    conf_threshold: float = 0.30
    # ByteTrack's whole idea: detections between low_conf and conf_threshold are
    # too weak to start a track but good enough to continue one. Keeping them is
    # what stops IDs flickering when a vehicle is briefly occluded.
    low_conf_threshold: float = 0.10
    nms_iou: float = 0.45
    device: str = "cpu"
    # COCO class names we care about. Everything else is discarded before it can
    # reach the tracker and cost us anything.
    vehicle_classes: tuple[str, ...] = ("car", "motorcycle", "bus", "truck", "bicycle")
    person_classes: tuple[str, ...] = ("person",)

    @property
    def classes(self) -> tuple[str, ...]:
        return self.vehicle_classes + self.person_classes

    @staticmethod
    def from_env() -> DetectConfig:
        return DetectConfig(
            enabled=_env_bool("ANALYTICS_DETECT_ENABLED", True),
            model_path=_env("ANALYTICS_DETECT_MODEL", "models/yolov8n.onnx"),
            input_size=_env_int("ANALYTICS_DETECT_INPUT_SIZE", 640),
            conf_threshold=_env_float("ANALYTICS_DETECT_CONF", 0.30),
            low_conf_threshold=_env_float("ANALYTICS_DETECT_LOW_CONF", 0.10),
            nms_iou=_env_float("ANALYTICS_DETECT_NMS_IOU", 0.45),
            device=_env("ANALYTICS_DETECT_DEVICE", "cpu"),
            vehicle_classes=_env_list(
                "ANALYTICS_VEHICLE_CLASSES", ("car", "motorcycle", "bus", "truck", "bicycle")
            ),
            person_classes=_env_list("ANALYTICS_PERSON_CLASSES", ("person",)),
        )


@dataclass(frozen=True, slots=True)
class PlateConfig:
    """Stage 3. Crop candidate plate regions out of vehicle boxes.

    Deliberately geometric rather than learned. A plate detector would be a fifth
    model to ship, quantise and keep loaded; the cheap prior — plates sit low and
    central on a vehicle and have a known aspect ratio — throws away enough
    non-plate area to keep stage 4 affordable, which is the only thing stage 3
    exists to do.
    """

    enabled: bool = True
    # Vertical band of the vehicle box to search, as fractions of box height from
    # the top. Front/rear plates sit in the lower half on essentially every
    # vehicle type on an Indian road.
    band_top: float = 0.45
    band_bottom: float = 1.0
    # Plate width as a fraction of vehicle box width, and the aspect ratio the
    # crop is shaped to. These size the crop window that gets handed to stage 4;
    # they are a prior, not a measurement.
    width_fraction: float = 0.45
    target_aspect: float = 3.0
    # Plates smaller than this in the source frame will not survive OCR, so
    # cropping them only buys a wasted stage-4 call.
    min_plate_width_px: int = 48
    min_vehicle_box_px: int = 64
    aspect_min: float = 1.6  # Indian plates run ~2:1 single-row, ~1:1 two-row.
    aspect_max: float = 6.0
    max_crops_per_frame: int = 4  # Bounds worst-case stage-4 fan-out on a busy frame.
    pad_fraction: float = 0.06  # A tight crop clips glyph edges and wrecks OCR.
    # Refine the crop position by horizontal-gradient energy within the band.
    # Plate glyphs are dense vertical edges, so the highest-energy window in the
    # band is usually the plate. Cheap enough to always be worth it; disable to
    # measure what the refinement is actually buying.
    refine_by_edges: bool = True

    @staticmethod
    def from_env() -> PlateConfig:
        return PlateConfig(
            enabled=_env_bool("ANALYTICS_PLATE_ENABLED", True),
            band_top=_env_float("ANALYTICS_PLATE_BAND_TOP", 0.45),
            band_bottom=_env_float("ANALYTICS_PLATE_BAND_BOTTOM", 1.0),
            width_fraction=_env_float("ANALYTICS_PLATE_WIDTH_FRACTION", 0.45),
            target_aspect=_env_float("ANALYTICS_PLATE_TARGET_ASPECT", 3.0),
            min_plate_width_px=_env_int("ANALYTICS_PLATE_MIN_WIDTH_PX", 48),
            min_vehicle_box_px=_env_int("ANALYTICS_PLATE_MIN_VEHICLE_PX", 64),
            aspect_min=_env_float("ANALYTICS_PLATE_ASPECT_MIN", 1.6),
            aspect_max=_env_float("ANALYTICS_PLATE_ASPECT_MAX", 6.0),
            max_crops_per_frame=_env_int("ANALYTICS_PLATE_MAX_CROPS", 4),
            pad_fraction=_env_float("ANALYTICS_PLATE_PAD_FRACTION", 0.06),
            refine_by_edges=_env_bool("ANALYTICS_PLATE_REFINE_EDGES", True),
        )


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """Stage 4. The expensive one. Target is ~2% of decoded frames.

    ``once_per_track`` is the single biggest saving in the whole pipeline: a
    vehicle crossing the frame is 40-plus frames, and reading its plate 40 times
    produces 40 chances to read it wrong. One confident read per track, retried
    only while confidence is below ``accept_confidence``, both cuts cost and
    improves accuracy.
    """

    enabled: bool = True
    engine: str = "paddle"
    lang: str = "en"
    device: str = "cpu"
    min_confidence: float = 0.45  # Below this the read is not emitted at all.
    accept_confidence: float = 0.80  # At or above this, stop re-reading the track.
    once_per_track: bool = True
    max_attempts_per_track: int = 5
    # Gujarat/Indian format: 2 letters, 2 digits, 1-3 letters, 4 digits. Used to
    # reject garbage, never to rewrite a read.
    plate_pattern: str = r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$"

    @staticmethod
    def from_env() -> OcrConfig:
        return OcrConfig(
            enabled=_env_bool("ANALYTICS_OCR_ENABLED", True),
            engine=_env("ANALYTICS_OCR_ENGINE", "paddle"),
            lang=_env("ANALYTICS_OCR_LANG", "en"),
            device=_env("ANALYTICS_OCR_DEVICE", "cpu"),
            min_confidence=_env_float("ANALYTICS_OCR_MIN_CONF", 0.45),
            accept_confidence=_env_float("ANALYTICS_OCR_ACCEPT_CONF", 0.80),
            once_per_track=_env_bool("ANALYTICS_OCR_ONCE_PER_TRACK", True),
            max_attempts_per_track=_env_int("ANALYTICS_OCR_MAX_ATTEMPTS", 5),
            plate_pattern=_env(
                "ANALYTICS_OCR_PLATE_PATTERN", r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$"
            ),
        )


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Multi-object tracker.

    ``max_age_seconds`` and not ``max_age_frames``. Frame intervals on these
    streams are not uniform and the worker deliberately skips frames when it
    falls behind, so "three frames old" means anything between 0.1 s and several
    seconds. Ageing in seconds is the only definition that stays correct when the
    adaptive sampler changes the stride underneath the tracker.
    """

    iou_threshold: float = 0.30
    max_age_seconds: float = 1.5
    min_hits: int = 3  # Hits before a track is confirmed and allowed to emit.
    max_tracks: int = 200  # Hard ceiling; a pathological frame must not exhaust memory.
    # Trajectory history retained per track, in seconds. Enough for a speed
    # estimate and a turn, not enough to be a memory leak on a busy junction.
    history_seconds: float = 10.0

    @staticmethod
    def from_env() -> TrackerConfig:
        return TrackerConfig(
            iou_threshold=_env_float("ANALYTICS_TRACK_IOU", 0.30),
            max_age_seconds=_env_float("ANALYTICS_TRACK_MAX_AGE_SECONDS", 1.5),
            min_hits=_env_int("ANALYTICS_TRACK_MIN_HITS", 3),
            max_tracks=_env_int("ANALYTICS_TRACK_MAX_TRACKS", 200),
            history_seconds=_env_float("ANALYTICS_TRACK_HISTORY_SECONDS", 10.0),
        )


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """RTSP capture. Every default here answers a specific documented failure.

    See ``capture.py`` for the reasoning behind each. In short: TCP because UDP
    silently corrupts, PTS because arrival time lies at join, backoff because 50
    workers reconnecting in lockstep is a denial of service against a sandbox we
    do not own.
    """

    transport: str = "tcp"  # Never udp. See capture.ffmpeg_capture_options().
    open_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 10.0
    # Consecutive failed reads before we give up on the socket and reconnect.
    # One failed read is normal on a lossy link; twenty in a row is a dead stream.
    max_read_failures: int = 20
    backoff_initial_seconds: float = 2.0
    backoff_factor: float = 2.0
    backoff_cap_seconds: float = 30.0
    backoff_jitter: float = 0.25  # +/-25%, so a fleet-wide outage does not resynchronise.
    # A backwards PTS step larger than this is a loop cut, not B-frame reordering.
    # Reordering wobbles by a frame or two; the sandbox loop jumps back seconds.
    loop_jump_tolerance_seconds: float = 1.0
    # Nominal gap inserted into the monotonic timeline at a loop cut, so the two
    # segments cannot be mistaken for continuous motion.
    segment_gap_seconds: float = 1.0

    @staticmethod
    def from_env() -> CaptureConfig:
        return CaptureConfig(
            transport=_env("ANALYTICS_RTSP_TRANSPORT", "tcp"),
            open_timeout_seconds=_env_float("ANALYTICS_OPEN_TIMEOUT_SECONDS", 10.0),
            read_timeout_seconds=_env_float("ANALYTICS_READ_TIMEOUT_SECONDS", 10.0),
            max_read_failures=_env_int("ANALYTICS_MAX_READ_FAILURES", 20),
            backoff_initial_seconds=_env_float("ANALYTICS_BACKOFF_INITIAL_SECONDS", 2.0),
            backoff_factor=_env_float("ANALYTICS_BACKOFF_FACTOR", 2.0),
            backoff_cap_seconds=_env_float("ANALYTICS_BACKOFF_CAP_SECONDS", 30.0),
            backoff_jitter=_env_float("ANALYTICS_BACKOFF_JITTER", 0.25),
            loop_jump_tolerance_seconds=_env_float("ANALYTICS_LOOP_TOLERANCE_SECONDS", 1.0),
            segment_gap_seconds=_env_float("ANALYTICS_SEGMENT_GAP_SECONDS", 1.0),
        )


@dataclass(frozen=True, slots=True)
class SamplingConfig:
    """Adaptive frame skipping. This is what makes 50 streams survivable.

    The failure mode we are avoiding: processing takes longer than real time, the
    decode queue grows without bound, memory climbs, and every event the worker
    emits is minutes stale by the time it lands. The fix is to give up on frames
    on purpose. A worker that analyses every third frame and stays current is
    strictly more useful than one that analyses every frame and is ten minutes
    behind.

    ``target_duty`` is the fraction of stream time we are willing to spend on
    compute. Leaving headroom (0.7, not 1.0) matters because the cost per frame
    is bursty: one frame with fifteen vehicles costs far more than the mean.
    """

    enabled: bool = True
    min_stride: int = 1
    max_stride: int = 15
    target_duty: float = 0.70
    high_watermark: float = 0.85  # Above this, widen the stride.
    low_watermark: float = 0.50  # Below this, narrow it again.
    ewma_alpha: float = 0.2  # Slow enough that one heavy frame does not thrash the stride.
    # Frames to accumulate before the first stride change, so the decision is not
    # made from the join-time GOP burst.
    min_samples: int = 15

    @staticmethod
    def from_env() -> SamplingConfig:
        return SamplingConfig(
            enabled=_env_bool("ANALYTICS_ADAPTIVE_SAMPLING", True),
            min_stride=_env_int("ANALYTICS_MIN_STRIDE", 1),
            max_stride=_env_int("ANALYTICS_MAX_STRIDE", 15),
            target_duty=_env_float("ANALYTICS_TARGET_DUTY", 0.70),
            high_watermark=_env_float("ANALYTICS_DUTY_HIGH", 0.85),
            low_watermark=_env_float("ANALYTICS_DUTY_LOW", 0.50),
            ewma_alpha=_env_float("ANALYTICS_DUTY_ALPHA", 0.2),
            min_samples=_env_int("ANALYTICS_DUTY_MIN_SAMPLES", 15),
        )


@dataclass(frozen=True, slots=True)
class SinkConfig:
    """Where primitive events go.

    Unconfigured means JSONL on stdout, which is what the tests and the dry run
    use. There is no default registry URL: a worker that silently posts to
    somebody's endpoint because a default was left in is worse than one that
    prints to a terminal.
    """

    registry_url: str = ""
    api_token: str = ""
    jsonl_path: str = ""  # Empty and no registry_url -> stdout.
    batch_size: int = 32
    flush_interval_seconds: float = 2.0
    # Bounded on purpose. If the registry is down for an hour we keep the newest
    # 5000 events and drop the rest, rather than becoming the reason the edge box
    # runs out of memory. See sink.BufferedEventSink.
    buffer_capacity: int = 5000
    post_timeout_seconds: float = 5.0
    max_post_attempts: int = 3
    # Thumbnails are small JPEGs attached to an event. Continuous video never
    # leaves the edge under any configuration; there is no flag for that.
    send_thumbnails: bool = True
    thumbnail_max_width: int = 320
    thumbnail_jpeg_quality: int = 70

    @staticmethod
    def from_env() -> SinkConfig:
        return SinkConfig(
            registry_url=_env("ANALYTICS_REGISTRY_URL", ""),
            api_token=_env("ANALYTICS_API_TOKEN", ""),
            jsonl_path=_env("ANALYTICS_JSONL_PATH", ""),
            batch_size=_env_int("ANALYTICS_SINK_BATCH_SIZE", 32),
            flush_interval_seconds=_env_float("ANALYTICS_SINK_FLUSH_SECONDS", 2.0),
            buffer_capacity=_env_int("ANALYTICS_SINK_BUFFER_CAPACITY", 5000),
            post_timeout_seconds=_env_float("ANALYTICS_SINK_TIMEOUT_SECONDS", 5.0),
            max_post_attempts=_env_int("ANALYTICS_SINK_MAX_ATTEMPTS", 3),
            send_thumbnails=_env_bool("ANALYTICS_SEND_THUMBNAILS", True),
            thumbnail_max_width=_env_int("ANALYTICS_THUMBNAIL_MAX_WIDTH", 320),
            thumbnail_jpeg_quality=_env_int("ANALYTICS_THUMBNAIL_QUALITY", 70),
        )


@dataclass(frozen=True, slots=True)
class CameraSpec:
    """One stream this worker is responsible for.

    ``meters_per_pixel`` is per camera and optional. Speed in px/s is useless to
    an operator and speed in km/h is a lie unless somebody calibrated the view,
    so the worker emits the pixel figure always and the metric figure only where
    a calibration exists. Guessing a scale factor to make the demo look better is
    how an analytics platform loses a court case.
    """

    camera_id: str
    url: str
    label: str = ""
    meters_per_pixel: float | None = None
    stub: bool = False  # Synthetic frames instead of RTSP; dry runs and tests.

    @staticmethod
    def parse(spec: str) -> CameraSpec:
        """``camera_id=rtsp://...`` or a bare URL.

        A bare URL gets a camera id derived from it, which is fine for a manual
        smoke test and wrong for anything else, because event correlation
        downstream keys on camera_id matching the registry.
        """
        if "=" in spec:
            camera_id, _, url = spec.partition("=")
            return CameraSpec(camera_id=camera_id.strip(), url=url.strip())
        url = spec.strip()
        return CameraSpec(camera_id=url.rsplit("/", 1)[-1] or url, url=url)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Everything one worker process needs.

    One process per N cameras, threads within it. Not one process per camera:
    fifty Python processes each holding its own copy of a YOLO session is how you
    run out of RAM on hardware a district can actually afford. Not one thread per
    camera without a ceiling either — ``max_cameras_per_process`` exists so that
    the scaling story in the submission is a measured number of processes rather
    than a hope.
    """

    worker_id: str = "worker-0"
    cameras: tuple[CameraSpec, ...] = ()
    max_cameras_per_process: int = 8
    stages: tuple[str, ...] = STAGE_ORDER
    motion: MotionConfig = field(default_factory=MotionConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    plate: PlateConfig = field(default_factory=PlateConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    sink: SinkConfig = field(default_factory=SinkConfig)
    log_level: str = "INFO"
    # How often the cascade's measured counters are logged and emitted. These are
    # the numbers the submission's sizing section is built from, so they are
    # reported on a schedule rather than only at shutdown — a worker killed by the
    # demo ending must still have reported.
    stats_interval_seconds: float = 30.0
    # Wall-clock limit for a run. Zero means run until stopped. Used by the
    # rehearsal harness so a 50-camera test run terminates on its own.
    run_seconds: float = 0.0

    def stage_enabled(self, name: str) -> bool:
        if name not in self.stages:
            return False
        return bool(getattr(getattr(self, name), "enabled", True))

    def enabled_stages(self) -> tuple[str, ...]:
        return tuple(name for name in STAGE_ORDER if self.stage_enabled(name))

    def with_cameras(self, cameras: tuple[CameraSpec, ...]) -> WorkerConfig:
        return replace(self, cameras=cameras)

    def shards(self) -> tuple[tuple[CameraSpec, ...], ...]:
        """Split the camera list into per-process groups.

        Returned rather than acted on, so the caller (a supervisor, a systemd
        template, docker compose) decides how to spawn. The worker itself only
        ever handles one shard.
        """
        n = max(1, self.max_cameras_per_process)
        return tuple(
            tuple(self.cameras[i : i + n]) for i in range(0, len(self.cameras), n)
        )

    @staticmethod
    def from_env() -> WorkerConfig:
        specs = _env_list("ANALYTICS_CAMERAS")
        return WorkerConfig(
            worker_id=_env("ANALYTICS_WORKER_ID", "worker-0"),
            cameras=tuple(CameraSpec.parse(s) for s in specs),
            max_cameras_per_process=_env_int("ANALYTICS_MAX_CAMERAS_PER_PROCESS", 8),
            stages=_env_list("ANALYTICS_STAGES", STAGE_ORDER),
            motion=MotionConfig.from_env(),
            detect=DetectConfig.from_env(),
            plate=PlateConfig.from_env(),
            ocr=OcrConfig.from_env(),
            tracker=TrackerConfig.from_env(),
            capture=CaptureConfig.from_env(),
            sampling=SamplingConfig.from_env(),
            sink=SinkConfig.from_env(),
            log_level=_env("LOG_LEVEL", "INFO"),
            stats_interval_seconds=_env_float("ANALYTICS_STATS_INTERVAL_SECONDS", 30.0),
            run_seconds=_env_float("ANALYTICS_RUN_SECONDS", 0.0),
        )

    def validate(self) -> None:
        """Reject configurations that would produce plausible-looking nonsense."""
        unknown = [s for s in self.stages if s not in STAGE_ORDER]
        if unknown:
            raise ValueError(f"unknown stage(s) {unknown}; valid stages are {list(STAGE_ORDER)}")
        if self.capture.transport.lower() != "tcp":
            # Not a hard error — someone may be testing against a local file — but
            # it must be loud, because UDP failure is silent frame corruption that
            # looks like a bad model rather than a bad transport.
            raise ValueError(
                "RTSP transport must be 'tcp'. UDP drops packets silently on these "
                "streams and the resulting corrupt frames get misread as detection "
                "failures. Override only if you are not reading RTSP at all."
            )
        if not self.sampling.min_stride >= 1:
            raise ValueError("min_stride must be at least 1")
        if self.sampling.max_stride < self.sampling.min_stride:
            raise ValueError("max_stride must be >= min_stride")
        if not 0.0 < self.sampling.low_watermark < self.sampling.high_watermark:
            raise ValueError("require 0 < low_watermark < high_watermark")
        if self.capture.backoff_cap_seconds < self.capture.backoff_initial_seconds:
            raise ValueError("backoff cap must be >= initial delay")
        if not 0.0 <= self.capture.backoff_jitter < 1.0:
            raise ValueError("backoff jitter must be in [0, 1)")
        if self.ocr.accept_confidence < self.ocr.min_confidence:
            raise ValueError("ocr accept_confidence must be >= min_confidence")
        if not 0.0 <= self.plate.band_top < self.plate.band_bottom <= 1.0:
            raise ValueError("require 0 <= band_top < band_bottom <= 1")
