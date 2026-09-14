"""Worker configuration: environment first, registry second, defaults last.

Plain dataclasses rather than pydantic. The registry service already pays for
pydantic because it validates untrusted request bodies; this worker validates only
operator-supplied environment and its own registry's responses, and one fewer
wheel is one fewer thing that fails to install on an edge box the night before a
demo.

Three things here are load-bearing rather than decorative:

1. **Every stage carries its own ``enabled`` flag.** The sizing figures in the
   submission are produced by running the same binary with stages switched off and
   comparing measured throughput. "Disable stage 3 and re-measure" has to be a
   config change, not a code change, or the comparison is between two programs
   rather than between two configurations of one.

2. **Backends are selected by name, never by probing.** ``detector=stub`` and
   ``detector=ultralytics`` are both first-class. A worker that silently falls back
   to a stub when weights are missing is worse than one that refuses to start,
   because it reports healthy while analysing nothing. So the fallback is an
   explicit choice an operator makes and the log records.

3. **``camera_id`` is an int.** It is the registry's primary key, because
   ``Sighting.camera_id`` is, and every cross-camera correlation downstream joins
   on it. A worker that invents its own string id produces events that cannot be
   joined to a camera's location, which makes them useless for the one thing the
   platform exists to do.

Geometry in this module — zone polygons, crossing lines, homography source points
— is in **normalised [0, 1] frame coordinates**, matching
``services.common.events.BBox``. The grid mixes resolutions and renegotiates
profiles mid-stream, so a pixel coordinate does not mean the same thing on two
cameras or on one camera an hour apart.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

#: Cascade stage names in funnel order. Stage 0 is the cheapest and sees every
#: frame; stage 3 is the most expensive and should see almost none.
STAGE_ORDER: tuple[str, ...] = ("motion", "detect", "plate", "ocr")

#: Backends that need no optional dependency. Anything else is lazy-imported and
#: will raise an actionable error if its wheel is absent.
STUB_BACKENDS = frozenset({"stub"})


# ---------------------------------------------------------------------------
# Environment readers
# ---------------------------------------------------------------------------
# Each raises on a malformed value rather than falling back to the default. An
# operator who typed ANALYTICS_MOTION_THRESHOLD=eighteen deserves to be told, not
# to spend the demo wondering why the gate behaves as though they had not set it.


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


def _env_json(name: str, default: Any = None) -> Any:
    raw = _env(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Stage 0: motion gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MotionConfig:
    """Stage 0. Frame differencing on a downscaled greyscale frame.

    The cheapest stage, run on every decoded frame, and the reason the rest of the
    cascade is affordable at all. Everything downstream sees only what this stage
    passes, so a percentage point here is worth more than a percentage point of
    optimisation anywhere else.

    The two settings that stop this gate failing open are
    ``compensate_illumination`` and the grid. A cloud crossing the sun, a vehicle's
    headlights sweeping the scene, or a camera's IR-cut filter switching at dusk
    changes *every* pixel at once. A naive gate reports that as total motion, opens
    permanently, and the cascade quietly stops being a cascade — at the exact time
    of day when a crime-against-women use case needs it most. See
    ``stages.motion`` for how the compensation works and what it cannot fix.
    """

    enabled: bool = True

    #: Work resolution. Differencing 1080p costs more than the gate saves; at
    #: 320 px wide the memory traffic is ~20x lower and an object the size of a
    #: person still survives the downscale. A pixel of sensor noise does not,
    #: which is the point.
    work_width: int = 320
    blur_kernel: int = 5

    #: Per-pixel absolute residual, 0-255, after illumination compensation.
    diff_threshold: int = 18

    #: Fit and remove a global affine intensity change (gain and bias) before
    #: differencing. Off only to measure what it is buying.
    compensate_illumination: bool = True

    #: A residual this much larger than the compensated frame's own noise floor is
    #: what "changed" means. Guards against a scene whose noise is genuinely high.
    noise_sigmas: float = 3.0

    #: Per-region decision grid. Motion is *local*; a global intensity change is
    #: not. Requiring the change to be concentrated in a few cells is the second,
    #: independent defence against the dusk failure — even if the affine fit is
    #: defeated by a non-linear tone curve, a genuine illumination shift lights up
    #: every cell roughly equally and fails the concentration test.
    grid_rows: int = 6
    grid_cols: int = 8
    #: Fraction of a cell's pixels that must change for the cell to count.
    cell_min_fraction: float = 0.02
    #: Cells that must be active. One is enough: a distant pedestrian occupies one.
    min_active_cells: int = 1
    #: Above this fraction of *all* cells being active, the change is treated as
    #: global rather than as an object, and the frame is dropped with the
    #: ``illumination`` reason unless the frame-wide residual is also strong. A
    #: real object cannot activate 90% of the grid without also being enormous.
    max_active_cell_fraction: float = 0.9

    #: Frame-wide floor, as a fraction of the downscaled frame. Belt and braces
    #: against a grid tuned too permissively.
    min_area_fraction: float = 0.0008

    #: Frames to observe before the gate is allowed to block anything. The frames
    #: immediately after a connect are a replayed GOP whose reference pictures we
    #: never received, so their content is unreliable and calibrating against it is
    #: worse than passing a handful of frames per connection.
    warmup_frames: int = 3

    #: A gate that has blocked for this long forces one frame through anyway.
    #: Differencing detects *change*, not presence: a vehicle stopped at a red
    #: light stops generating change, and a gate with no heartbeat would go silent
    #: on exactly the stationary vehicle an "abandoned" or "stopped on the
    #: shoulder" rule exists to catch.
    heartbeat_seconds: float = 5.0

    @staticmethod
    def from_env() -> MotionConfig:
        return MotionConfig(
            enabled=_env_bool("ANALYTICS_MOTION_ENABLED", True),
            work_width=_env_int("ANALYTICS_MOTION_WORK_WIDTH", 320),
            blur_kernel=_env_int("ANALYTICS_MOTION_BLUR", 5),
            diff_threshold=_env_int("ANALYTICS_MOTION_THRESHOLD", 18),
            compensate_illumination=_env_bool("ANALYTICS_MOTION_COMPENSATE", True),
            noise_sigmas=_env_float("ANALYTICS_MOTION_NOISE_SIGMAS", 3.0),
            grid_rows=_env_int("ANALYTICS_MOTION_GRID_ROWS", 6),
            grid_cols=_env_int("ANALYTICS_MOTION_GRID_COLS", 8),
            cell_min_fraction=_env_float("ANALYTICS_MOTION_CELL_FRACTION", 0.02),
            min_active_cells=_env_int("ANALYTICS_MOTION_MIN_CELLS", 1),
            max_active_cell_fraction=_env_float("ANALYTICS_MOTION_MAX_CELL_FRACTION", 0.9),
            min_area_fraction=_env_float("ANALYTICS_MOTION_MIN_AREA_FRACTION", 0.0008),
            warmup_frames=_env_int("ANALYTICS_MOTION_WARMUP_FRAMES", 3),
            heartbeat_seconds=_env_float("ANALYTICS_MOTION_HEARTBEAT_SECONDS", 5.0),
        )


# ---------------------------------------------------------------------------
# Stage 1: object detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectConfig:
    """Stage 1. YOLOv8n int8, only on frames the gate let through.

    ``input_size`` is one number because the model wants a square input and the
    streams do not supply one: the grid mixes H.264 with H.265 and mixes
    resolutions, so there is no fixed-shape batch to be had. Every frame is
    letterboxed individually and the boxes are mapped back before they leave the
    stage. See ``stages.detect.letterbox``.

    ``conf_threshold`` and ``low_conf_threshold`` are two numbers on purpose and
    both are the tracker's business: detections in the band between them are too
    weak to start a track and good enough to continue one. Collapsing them into
    one threshold makes the tracker's second association pass a no-op and brings
    back the flickering-id behaviour it exists to remove.
    """

    enabled: bool = True

    #: ``stub`` or ``ultralytics``. Never probed — see the module docstring.
    backend: str = "stub"
    model_path: str = "models/yolov8n.onnx"
    input_size: int = 640
    conf_threshold: float = 0.30
    low_conf_threshold: float = 0.10
    nms_iou: float = 0.45
    device: str = "cpu"

    #: Classes to keep, as ``services.common.events.OBJECT_CLASSES`` names.
    #: Everything else is discarded inside the stage so it never costs the tracker
    #: anything.
    classes: tuple[str, ...] = (
        "person", "bicycle", "motorcycle", "car", "bus", "truck", "animal",
    )

    @staticmethod
    def from_env() -> DetectConfig:
        return DetectConfig(
            enabled=_env_bool("ANALYTICS_DETECT_ENABLED", True),
            backend=_env("ANALYTICS_DETECTOR", "stub").lower(),
            model_path=_env("ANALYTICS_DETECT_MODEL", "models/yolov8n.onnx"),
            input_size=_env_int("ANALYTICS_DETECT_INPUT_SIZE", 640),
            conf_threshold=_env_float("ANALYTICS_DETECT_CONF", 0.30),
            low_conf_threshold=_env_float("ANALYTICS_DETECT_LOW_CONF", 0.10),
            nms_iou=_env_float("ANALYTICS_DETECT_NMS_IOU", 0.45),
            device=_env("ANALYTICS_DETECT_DEVICE", "cpu"),
            classes=_env_list(
                "ANALYTICS_DETECT_CLASSES",
                ("person", "bicycle", "motorcycle", "car", "bus", "truck", "animal"),
            ),
        )


# ---------------------------------------------------------------------------
# Stage 2: plate localisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlateConfig:
    """Stage 2. Locate a plate region inside a vehicle box. Arithmetic, no model.

    PaddleOCR already contains a text detector, so handing it a region that
    certainly contains the plate and little else gets the localisation for free.
    A fourth model to ship, quantise, keep resident and validate, in order to find
    a region a geometric prior plus a gradient scan already finds, would cost real
    memory on every edge box for a marginal gain.

    ``min_vehicle_box_px`` and ``min_plate_width_px`` are the two gates that
    matter for cost, and they are applied *before* anything is cropped or scored.
    A 12-pixel-wide plate cannot be read by any OCR engine at any price, so
    cropping it, sharpening it and paying for an inference on it is pure waste —
    and it is the single most common way an ANPR pipeline's cost runs away, because
    a wide-angle road camera is mostly full of vehicles that are too far away.
    """

    enabled: bool = True

    #: Vertical band of the vehicle box to search, as fractions of box height from
    #: the top. Front and rear plates sit in the lower half of essentially every
    #: vehicle type on an Indian road.
    band_top: float = 0.45
    band_bottom: float = 1.0

    #: Plate width as a fraction of the vehicle box, and the aspect the crop is
    #: shaped to. A prior, not a measurement.
    width_fraction: float = 0.45
    target_aspect: float = 3.0
    pad_fraction: float = 0.06

    #: Rejection gates, in source-frame pixels. Pixels rather than normalised
    #: units because legibility is a function of how many photosites the glyphs
    #: landed on, which normalised coordinates deliberately hide.
    min_vehicle_box_px: int = 64
    min_plate_width_px: int = 48
    aspect_min: float = 1.6  # Indian plates: ~2:1 single-row, ~1:1 two-row.
    aspect_max: float = 6.0

    #: Bounds worst-case stage-3 fan-out on a frame with fifteen vehicles.
    max_crops_per_frame: int = 4

    #: Slide the window across the band and take the highest horizontal-gradient
    #: energy. Plate glyphs are dense vertical strokes; the body seams and shadow
    #: lines that dominate a vehicle's lower half are horizontal. Disable to
    #: measure what the refinement is buying.
    refine_by_edges: bool = True

    @staticmethod
    def from_env() -> PlateConfig:
        return PlateConfig(
            enabled=_env_bool("ANALYTICS_PLATE_ENABLED", True),
            band_top=_env_float("ANALYTICS_PLATE_BAND_TOP", 0.45),
            band_bottom=_env_float("ANALYTICS_PLATE_BAND_BOTTOM", 1.0),
            width_fraction=_env_float("ANALYTICS_PLATE_WIDTH_FRACTION", 0.45),
            target_aspect=_env_float("ANALYTICS_PLATE_TARGET_ASPECT", 3.0),
            pad_fraction=_env_float("ANALYTICS_PLATE_PAD_FRACTION", 0.06),
            min_vehicle_box_px=_env_int("ANALYTICS_PLATE_MIN_VEHICLE_PX", 64),
            min_plate_width_px=_env_int("ANALYTICS_PLATE_MIN_WIDTH_PX", 48),
            aspect_min=_env_float("ANALYTICS_PLATE_ASPECT_MIN", 1.6),
            aspect_max=_env_float("ANALYTICS_PLATE_ASPECT_MAX", 6.0),
            max_crops_per_frame=_env_int("ANALYTICS_PLATE_MAX_CROPS", 4),
            refine_by_edges=_env_bool("ANALYTICS_PLATE_REFINE_EDGES", True),
        )


# ---------------------------------------------------------------------------
# Stage 3: OCR
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """Stage 3. The most expensive stage in the pipeline. Target: ~2% of frames.

    Two mechanisms keep it there, and both improve accuracy as a side effect.

    ``min_laplacian_variance`` is a sharpness gate. A motion-blurred plate does
    not become readable by being handed to a better recogniser; it produces a
    confident wrong string, which is the failure mode that ends up in front of a
    magistrate. Variance of the Laplacian is the cheapest usable proxy for focus
    and it costs a single convolution on a crop of a few thousand pixels.

    ``once_per_track`` is the single biggest saving anywhere in this worker. A
    vehicle crossing frame is forty-plus frames; reading its plate forty times
    costs forty inferences and creates forty chances to emit a wrong string. One
    read per track, retried only while confidence is below ``accept_confidence``
    and capped at ``max_attempts_per_track``, cuts the cost by more than an order
    of magnitude and cuts the false-read rate with it.
    """

    enabled: bool = True

    #: ``stub`` or ``paddle``.
    backend: str = "stub"
    lang: str = "en"
    device: str = "cpu"

    #: Quality gate, applied to the crop before any inference is paid for.
    min_crop_width_px: int = 48
    min_crop_height_px: int = 14
    min_laplacian_variance: float = 25.0
    aspect_min: float = 1.6
    aspect_max: float = 6.0

    #: Below ``min_confidence`` the read is not emitted at all: a 0.2-confidence
    #: plate string is not evidence, and putting it on the bus invites a consumer
    #: to treat it as one. At or above ``accept_confidence`` the track is done and
    #: never re-read.
    min_confidence: float = 0.45
    accept_confidence: float = 0.80
    once_per_track: bool = True
    max_attempts_per_track: int = 5

    #: Indian format: 2 letters, 1-2 digits, 1-3 letters, 4 digits. Used to reject
    #: garbage and to set ``format_valid``, **never** to rewrite a read. See
    #: ``stages.plate.normalise``.
    plate_pattern: str = r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$"

    @staticmethod
    def from_env() -> OcrConfig:
        return OcrConfig(
            enabled=_env_bool("ANALYTICS_OCR_ENABLED", True),
            backend=_env("ANALYTICS_OCR", "stub").lower(),
            lang=_env("ANALYTICS_OCR_LANG", "en"),
            device=_env("ANALYTICS_OCR_DEVICE", "cpu"),
            min_crop_width_px=_env_int("ANALYTICS_OCR_MIN_CROP_WIDTH", 48),
            min_crop_height_px=_env_int("ANALYTICS_OCR_MIN_CROP_HEIGHT", 14),
            min_laplacian_variance=_env_float("ANALYTICS_OCR_MIN_SHARPNESS", 25.0),
            aspect_min=_env_float("ANALYTICS_OCR_ASPECT_MIN", 1.6),
            aspect_max=_env_float("ANALYTICS_OCR_ASPECT_MAX", 6.0),
            min_confidence=_env_float("ANALYTICS_OCR_MIN_CONF", 0.45),
            accept_confidence=_env_float("ANALYTICS_OCR_ACCEPT_CONF", 0.80),
            once_per_track=_env_bool("ANALYTICS_OCR_ONCE_PER_TRACK", True),
            max_attempts_per_track=_env_int("ANALYTICS_OCR_MAX_ATTEMPTS", 5),
            plate_pattern=_env(
                "ANALYTICS_OCR_PLATE_PATTERN", r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$"
            ),
        )


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Multi-object tracker.

    ``max_age_seconds``, not ``max_age_frames``. Frame intervals on these streams
    are not uniform and the worker deliberately skips frames when it falls behind,
    so "three frames old" means anything between 0.1 s and several seconds. Ageing
    in seconds is the only definition that stays correct when the adaptive sampler
    changes the stride underneath the tracker while it runs.
    """

    iou_threshold: float = 0.30
    #: Detections at or above this may start a track. Below it, they may only
    #: extend one. This is the ByteTrack idea and it is the whole reason a vehicle
    #: passing behind a pole keeps its id.
    high_confidence: float = 0.30
    #: Below this, a detection is ignored entirely.
    low_confidence: float = 0.10
    max_age_seconds: float = 1.5
    min_hits: int = 3
    max_tracks: int = 200
    #: Trajectory points retained per track. Enough for a speed estimate, a turn
    #: and a proximity correlation; not enough to be a memory leak on a junction.
    history_points: int = 256

    @staticmethod
    def from_env() -> TrackerConfig:
        return TrackerConfig(
            iou_threshold=_env_float("ANALYTICS_TRACK_IOU", 0.30),
            high_confidence=_env_float("ANALYTICS_TRACK_HIGH_CONF", 0.30),
            low_confidence=_env_float("ANALYTICS_TRACK_LOW_CONF", 0.10),
            max_age_seconds=_env_float("ANALYTICS_TRACK_MAX_AGE_SECONDS", 1.5),
            min_hits=_env_int("ANALYTICS_TRACK_MIN_HITS", 3),
            max_tracks=_env_int("ANALYTICS_TRACK_MAX_TRACKS", 200),
            history_points=_env_int("ANALYTICS_TRACK_HISTORY_POINTS", 256),
        )


# ---------------------------------------------------------------------------
# Capture and pacing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """RTSP capture. Every default answers a specific documented failure.

    See ``source.py`` for the reasoning behind each. In short: TCP because UDP
    corrupts frames silently and the symptom looks like a bad model; PTS because
    arrival time is compressed by the gateway's GOP replay at join; jittered
    backoff because fifty workers reconnecting in lockstep against a sandbox we do
    not own is indistinguishable from an attack.
    """

    transport: str = "tcp"  # Never udp. See source.ffmpeg_capture_options().
    open_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 10.0

    #: Consecutive failed reads before the socket is abandoned. One failed read is
    #: normal on a lossy link; twenty in a row is a dead stream.
    max_read_failures: int = 20

    #: Multiplier applied to the failure tolerance during the join grace window,
    #: where a run of failed reads is the expected consequence of a replayed GOP
    #: referencing pictures we never received.
    join_grace_seconds: float = 2.0
    join_grace_multiplier: int = 4

    backoff_initial_seconds: float = 2.0
    backoff_factor: float = 2.0
    backoff_cap_seconds: float = 30.0
    backoff_jitter: float = 0.25  # +/-25%, so a fleet-wide outage does not resynchronise.

    #: Passed to ``StreamClock`` overrides. A backwards PTS step smaller than the
    #: reorder tolerance is B-frame reordering and must not re-anchor; anything
    #: larger is the sandbox feed looping with a hard scene cut.
    reorder_tolerance_seconds: float = 0.5
    forward_gap_seconds: float = 10.0

    @staticmethod
    def from_env() -> SourceConfig:
        return SourceConfig(
            transport=_env("ANALYTICS_RTSP_TRANSPORT", "tcp").lower(),
            open_timeout_seconds=_env_float("ANALYTICS_OPEN_TIMEOUT_SECONDS", 10.0),
            read_timeout_seconds=_env_float("ANALYTICS_READ_TIMEOUT_SECONDS", 10.0),
            max_read_failures=_env_int("ANALYTICS_MAX_READ_FAILURES", 20),
            join_grace_seconds=_env_float("ANALYTICS_JOIN_GRACE_SECONDS", 2.0),
            join_grace_multiplier=_env_int("ANALYTICS_JOIN_GRACE_MULTIPLIER", 4),
            backoff_initial_seconds=_env_float("ANALYTICS_BACKOFF_INITIAL_SECONDS", 2.0),
            backoff_factor=_env_float("ANALYTICS_BACKOFF_FACTOR", 2.0),
            backoff_cap_seconds=_env_float("ANALYTICS_BACKOFF_CAP_SECONDS", 30.0),
            backoff_jitter=_env_float("ANALYTICS_BACKOFF_JITTER", 0.25),
            reorder_tolerance_seconds=_env_float("ANALYTICS_REORDER_TOLERANCE_SECONDS", 0.5),
            forward_gap_seconds=_env_float("ANALYTICS_FORWARD_GAP_SECONDS", 10.0),
        )


@dataclass(frozen=True, slots=True)
class SamplingConfig:
    """Adaptive frame shedding. This is what makes fifty streams survivable.

    The failure mode being avoided: per-frame cost exceeds the frame interval, the
    decode backlog grows without bound, RSS climbs, and every event emitted is
    minutes stale by the time it lands. Bounding a queue instead of the stride just
    moves the problem — you still process stale frames, you merely lose the newest
    ones, which is the wrong end to lose.

    The controller measures **lag**, not duty cycle: how far behind the live edge
    the frame currently being processed is, derived from PTS against the wall
    clock. Lag is the quantity an operator cares about and the only one that stays
    meaningful across a stride change. See ``clock.AdaptiveSampler``.
    """

    enabled: bool = True
    min_stride: int = 1
    max_stride: int = 15

    #: Above ``max_lag_seconds`` of measured lag, widen the stride. Below
    #: ``target_lag_seconds``, narrow it again. The gap between the two is
    #: deliberate hysteresis: without it the controller oscillates every frame.
    target_lag_seconds: float = 0.5
    max_lag_seconds: float = 2.0

    ewma_alpha: float = 0.25
    #: Samples to accumulate before the first stride change, so the decision is
    #: not made from the join-time GOP burst — where stream time runs several
    #: times faster than wall time and lag readings are meaningless.
    min_samples: int = 15

    @staticmethod
    def from_env() -> SamplingConfig:
        return SamplingConfig(
            enabled=_env_bool("ANALYTICS_ADAPTIVE_SAMPLING", True),
            min_stride=_env_int("ANALYTICS_MIN_STRIDE", 1),
            max_stride=_env_int("ANALYTICS_MAX_STRIDE", 15),
            target_lag_seconds=_env_float("ANALYTICS_TARGET_LAG_SECONDS", 0.5),
            max_lag_seconds=_env_float("ANALYTICS_MAX_LAG_SECONDS", 2.0),
            ewma_alpha=_env_float("ANALYTICS_LAG_ALPHA", 0.25),
            min_samples=_env_int("ANALYTICS_LAG_MIN_SAMPLES", 15),
        )


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrimitiveConfig:
    """Thresholds for the geometric primitives. No model touches any of these.

    Every value here is a threshold on a measurement, which is what makes the
    extensibility argument real: "loitering near a substation" and "parked in a
    no-parking zone" are the same primitive with different zone polygons and
    different ``dwell_seconds``, authored as data. Adding an incident type needs
    neither a labelled dataset nor a redeploy of this worker.
    """

    #: Seconds inside a zone before ``dwell`` is emitted, and how often it repeats
    #: while the track stays there.
    dwell_seconds: float = 10.0
    dwell_repeat_seconds: float = 30.0
    #: Normalised movement below which a track inside a zone counts as still. A
    #: busy junction is permanently *occupied*; only the stationary case is an
    #: incident, and the two are different facts.
    still_movement: float = 0.02

    #: ``track_update`` heartbeat. A confirmed track on a 25 fps stream would
    #: otherwise put 25 events per second per object on the bus, which at fifty
    #: cameras is tens of thousands of events a second describing motion that has
    #: not meaningfully changed.
    track_update_seconds: float = 1.0

    #: Crowd counting cadence, per region.
    crowd_seconds: float = 2.0

    #: Proximity: two tracks within ``proximity_distance`` (normalised) for
    #: ``proximity_seconds``, with trajectory correlation at or above
    #: ``proximity_correlation``. See ``primitives.ProximityMonitor`` and the
    #: deliberate-omission note at the bottom of ``services.common.events``.
    #:
    #: Two thresholds because there are two ways to measure the distance and they
    #: are not equally good. ``proximity_distance_m`` is in ground-plane metres and
    #: is used whenever the camera has a homography — three metres means three
    #: metres wherever in the frame it happens. ``proximity_distance`` is the
    #: normalised-image fallback for an uncalibrated camera, where the same image
    #: distance is a few metres at the top of the frame and a few centimetres at
    #: the bottom. The fallback is usable and it is not equivalent; a camera whose
    #: proximity events matter should be calibrated.
    proximity_distance: float = 0.08
    proximity_distance_m: float = 3.0
    proximity_seconds: float = 6.0
    proximity_correlation: float = 0.6
    proximity_repeat_seconds: float = 30.0

    #: Abandoned object: a track that has not moved for this long with no person
    #: track within ``abandoned_owner_distance``.
    abandoned_seconds: float = 45.0
    abandoned_movement: float = 0.015
    abandoned_owner_distance: float = 0.15

    #: Speed: window over which displacement is measured, and the minimum interval
    #: worth dividing by. Below the minimum the answer is "not enough evidence",
    #: which must not be reported as "very fast".
    speed_window_seconds: float = 1.0
    speed_min_dt_seconds: float = 0.2
    speed_min_samples: int = 3

    @staticmethod
    def from_env() -> PrimitiveConfig:
        return PrimitiveConfig(
            dwell_seconds=_env_float("ANALYTICS_DWELL_SECONDS", 10.0),
            dwell_repeat_seconds=_env_float("ANALYTICS_DWELL_REPEAT_SECONDS", 30.0),
            still_movement=_env_float("ANALYTICS_STILL_MOVEMENT", 0.02),
            track_update_seconds=_env_float("ANALYTICS_TRACK_UPDATE_SECONDS", 1.0),
            crowd_seconds=_env_float("ANALYTICS_CROWD_SECONDS", 2.0),
            proximity_distance=_env_float("ANALYTICS_PROXIMITY_DISTANCE", 0.08),
            proximity_distance_m=_env_float("ANALYTICS_PROXIMITY_DISTANCE_M", 3.0),
            proximity_seconds=_env_float("ANALYTICS_PROXIMITY_SECONDS", 6.0),
            proximity_correlation=_env_float("ANALYTICS_PROXIMITY_CORRELATION", 0.6),
            proximity_repeat_seconds=_env_float("ANALYTICS_PROXIMITY_REPEAT_SECONDS", 30.0),
            abandoned_seconds=_env_float("ANALYTICS_ABANDONED_SECONDS", 45.0),
            abandoned_movement=_env_float("ANALYTICS_ABANDONED_MOVEMENT", 0.015),
            abandoned_owner_distance=_env_float("ANALYTICS_ABANDONED_OWNER_DISTANCE", 0.15),
            speed_window_seconds=_env_float("ANALYTICS_SPEED_WINDOW_SECONDS", 1.0),
            speed_min_dt_seconds=_env_float("ANALYTICS_SPEED_MIN_DT_SECONDS", 0.2),
            speed_min_samples=_env_int("ANALYTICS_SPEED_MIN_SAMPLES", 3),
        )


# ---------------------------------------------------------------------------
# Event egress
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SinkConfig:
    """Where primitive events go. See ``sink.py`` for the delivery mechanics.

    ``registry_url`` is deliberately separate from ``WorkerConfig.registry_url``:
    the two are different endpoints on the same service (assignments are read,
    events are written), and a worker running against a stale or read-only
    registry mirror should be able to point them at different places. Left unset,
    ``WorkerConfig.from_env`` defaults it to the assignments URL's host with the
    events path appended, which is right for the common case of one registry doing
    both jobs.

    No default URL is ever synthesised from nothing. A worker with neither set
    prints JSONL to stdout, which is loud enough that leaving it that way past a
    dry run gets noticed.
    """

    registry_url: str = ""
    api_token: str = ""
    #: Empty and no registry_url -> stdout. Set to a path to append to a file
    #: instead, which is what a rehearsal harness reads back.
    jsonl_path: str = ""

    #: Alerts are a second, much lower-volume stream — see ``sink.AlertSink`` —
    #: so they get their own endpoint rather than being mixed into the primitive
    #: event batch a rule engine had to read first to produce them. Left unset,
    #: ``WorkerConfig.from_env`` defaults it alongside ``registry_url``.
    alerts_url: str = ""
    alerts_jsonl_path: str = ""

    batch_size: int = 32
    flush_interval_seconds: float = 2.0
    #: Bounded, drop-oldest-when-full. See ``sink.BufferedEventSink``.
    buffer_capacity: int = 5000
    post_timeout_seconds: float = 5.0
    #: Attempts made during an orderly shutdown to drain the buffer before giving
    #: up and logging what is left undelivered. Not the retry count for a single
    #: batch, which is unbounded and paced by backoff instead.
    max_post_attempts: int = 3

    @staticmethod
    def from_env() -> SinkConfig:
        return SinkConfig(
            registry_url=_env("ANALYTICS_EVENTS_URL", ""),
            api_token=_env("ANALYTICS_API_TOKEN", ""),
            jsonl_path=_env("ANALYTICS_JSONL_PATH", ""),
            alerts_url=_env("ANALYTICS_ALERTS_URL", ""),
            alerts_jsonl_path=_env("ANALYTICS_ALERTS_JSONL_PATH", ""),
            batch_size=_env_int("ANALYTICS_SINK_BATCH_SIZE", 32),
            flush_interval_seconds=_env_float("ANALYTICS_SINK_FLUSH_SECONDS", 2.0),
            buffer_capacity=_env_int("ANALYTICS_SINK_BUFFER_CAPACITY", 5000),
            post_timeout_seconds=_env_float("ANALYTICS_SINK_TIMEOUT_SECONDS", 5.0),
            max_post_attempts=_env_int("ANALYTICS_SINK_MAX_ATTEMPTS", 3),
        )


# ---------------------------------------------------------------------------
# Per-camera geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Zone:
    """A named polygon in normalised frame coordinates.

    Drawn by an operator on the console, which means it is frequently concave (a
    zone following a kerb around a corner) and occasionally self-intersecting. The
    point-in-polygon test in ``primitives`` handles the first correctly and
    degrades predictably on the second; a convex-hull shortcut would silently
    include the road.
    """

    zone_id: str
    polygon: tuple[tuple[float, float], ...]
    dwell_seconds: float | None = None  # Overrides PrimitiveConfig for this zone.
    #: Operator-set context, not a model output: "this stretch has poor lighting
    #: and low footfall" is knowledge a control room already has about its own
    #: jurisdiction. ``rules.rule_women_safety`` reads it to weight a sustained
    #: ``proximity`` primitive more heavily here than in a crowded market zone —
    #: authored as data, per this module's own design argument, not as a model.
    isolated: bool = False

    def __post_init__(self) -> None:
        if len(self.polygon) < 3:
            raise ValueError(f"zone {self.zone_id!r} needs at least 3 vertices")


@dataclass(frozen=True, slots=True)
class CrossingLine:
    """A directed line segment in normalised coordinates.

    Directed, and that is the entire point. ``a -> b`` defines a positive side by
    the sign of the 2-D cross product, so a crossing is reported with a direction
    and "wrong-way driving" is a rule that reads that sign against the direction
    the registry says this carriageway runs. Without the direction the primitive
    would only say "something crossed", which no rule can act on.
    """

    line_id: str
    a: tuple[float, float]
    b: tuple[float, float]
    #: Human-readable names for the two directions, so an event says
    #: "northbound" rather than "+1". Free text; the rule engine may ignore it.
    positive_name: str = "positive"
    negative_name: str = "negative"


@dataclass(frozen=True, slots=True)
class Homography:
    """Image-to-ground plane mapping for one camera, plus its honest error bar.

    ``matrix`` is a 3x3 row-major homography taking **normalised** image
    coordinates to ground-plane metres in an arbitrary local frame. Normalised in,
    because the camera renegotiates its resolution mid-stream and a pixel-domain
    homography silently becomes wrong when it does.

    ``rms_error_m`` is the residual from whatever fit produced the matrix. It is
    mandatory and it is carried all the way onto the ``speed`` primitive. A speed
    with no error bar invites a rule to threshold on 61 km/h in a 60 zone, which
    on a four-point manual calibration of a wide-angle road camera is not a
    distinction the measurement can support. See ``primitives.SpeedEstimator``.
    """

    matrix: tuple[tuple[float, float, float], ...]
    rms_error_m: float
    #: Free text: who calibrated this, when, and how. Provenance, for the day
    #: somebody asks why the number is what it is.
    method: str = ""

    def __post_init__(self) -> None:
        if len(self.matrix) != 3 or any(len(row) != 3 for row in self.matrix):
            raise ValueError("homography matrix must be 3x3")
        if self.rms_error_m <= 0:
            # Zero would mean a perfect calibration, which no four-point manual
            # fit on a wide-angle CCTV view has ever been. Refusing it stops an
            # unset field being read downstream as "exact".
            raise ValueError("homography rms_error_m must be positive; a fit has residuals")


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """One stream this worker is responsible for.

    ``camera_id`` is the registry's integer primary key, not a name. Every
    cross-camera correlation downstream joins on it, and a worker that invents its
    own identifier produces events that cannot be tied to a location.
    """

    camera_id: int
    url: str
    label: str = ""
    zones: tuple[Zone, ...] = ()
    lines: tuple[CrossingLine, ...] = ()
    homography: Homography | None = None
    #: Region polygons for crowd counting. Separate from ``zones`` because a
    #: crowd measurement is about a region's occupancy over time and a zone event
    #: is about one track's relationship to a polygon; conflating them produces a
    #: dwell event per person in a crowd.
    crowd_regions: tuple[Zone, ...] = ()
    #: Synthetic frames instead of RTSP. Dry runs and tests.
    stub: bool = False

    @staticmethod
    def parse(spec: str) -> CameraConfig:
        """``17=rtsp://host/stream`` or ``17=stub://``.

        The integer prefix is required. A bare URL is rejected rather than given a
        derived id, because a derived id looks like it works and produces events
        that silently fail to join to the camera table.
        """
        camera_id, sep, url = spec.partition("=")
        if not sep:
            raise ValueError(
                f"camera spec {spec!r} must be 'camera_id=url' with the registry's "
                f"integer camera id, e.g. '17=rtsp://host/stream'"
            )
        try:
            cid = int(camera_id.strip())
        except ValueError as exc:
            raise ValueError(
                f"camera id {camera_id!r} must be the registry's integer id"
            ) from exc
        url = url.strip()
        return CameraConfig(camera_id=cid, url=url, stub=url.startswith("stub://") or not url)

    @staticmethod
    def from_mapping(data: Mapping[str, Any]) -> CameraConfig:
        """Build from a registry response or a JSON blob in the environment.

        Unknown keys are ignored rather than rejected: the registry will grow
        fields this worker does not care about, and a worker that refuses to start
        because the camera table gained a column is a worker that takes the fleet
        down on a schema migration.
        """
        url = str(data.get("url") or data.get("rtsp_url") or "")
        return CameraConfig(
            camera_id=int(data["camera_id"] if "camera_id" in data else data["id"]),
            url=url,
            label=str(data.get("label") or data.get("name") or ""),
            zones=tuple(_parse_zone(z) for z in data.get("zones") or ()),
            lines=tuple(_parse_line(line) for line in data.get("lines") or ()),
            homography=_parse_homography(data.get("homography")),
            crowd_regions=tuple(_parse_zone(z) for z in data.get("crowd_regions") or ()),
            stub=url.startswith("stub://") or not url,
        )


def _parse_zone(data: Mapping[str, Any]) -> Zone:
    return Zone(
        zone_id=str(data.get("zone_id") or data.get("id") or "zone"),
        polygon=tuple((float(p[0]), float(p[1])) for p in data["polygon"]),
        dwell_seconds=(
            float(data["dwell_seconds"]) if data.get("dwell_seconds") is not None else None
        ),
        isolated=bool(data.get("isolated", False)),
    )


def _parse_line(data: Mapping[str, Any]) -> CrossingLine:
    return CrossingLine(
        line_id=str(data.get("line_id") or data.get("id") or "line"),
        a=(float(data["a"][0]), float(data["a"][1])),
        b=(float(data["b"][0]), float(data["b"][1])),
        positive_name=str(data.get("positive_name") or "positive"),
        negative_name=str(data.get("negative_name") or "negative"),
    )


def _parse_homography(data: Any) -> Homography | None:
    if not data:
        return None
    return Homography(
        matrix=tuple(tuple(float(v) for v in row) for row in data["matrix"]),
        rms_error_m=float(data["rms_error_m"]),
        method=str(data.get("method") or ""),
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Everything one worker process needs.

    One process per N cameras with a thread per camera, not one process per
    camera. Fifty Python processes each holding their own copy of a detector
    session is fifty copies of the weights and fifty interpreter heaps, which on
    hardware a district can actually afford is the difference between running and
    not running. ``max_cameras_per_process`` exists so the scaling claim in the
    submission is a measured number of processes rather than a hope.
    """

    worker_id: str = "worker-0"
    cameras: tuple[CameraConfig, ...] = ()
    max_cameras_per_process: int = 8
    stages: tuple[str, ...] = STAGE_ORDER

    motion: MotionConfig = field(default_factory=MotionConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    plate: PlateConfig = field(default_factory=PlateConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    primitives: PrimitiveConfig = field(default_factory=PrimitiveConfig)
    sink: SinkConfig = field(default_factory=SinkConfig)

    #: Where the camera list comes from when ``ANALYTICS_CAMERAS`` is unset. Read
    #: only; this worker never POSTs anything anywhere over this URL — events go
    #: out over ``sink.registry_url`` instead, which is a write and is kept a
    #: separate field for exactly that reason.
    registry_url: str = ""
    registry_token: str = ""

    #: Local watchlist fixture, e.g. for a reproducible demo scenario. Wins over
    #: the registry when set — see ``watchlist.resolve_watchlist``. Empty and no
    #: registry means the worker matches against nothing, which is a valid
    #: configuration (Model 2-only sites), not an error.
    watchlist_path: str = ""
    watchlist_refresh_seconds: float = 60.0

    log_level: str = "INFO"
    #: How often the measured cascade counters are logged. On a schedule rather
    #: than only at shutdown, because a worker SIGKILLed when the demo ends must
    #: still have reported the figures the submission is built from.
    stats_interval_seconds: float = 30.0
    #: Wall-clock limit. Zero means run until stopped; used by the rehearsal
    #: harness so a fifty-camera run terminates on its own.
    run_seconds: float = 0.0

    def stage_enabled(self, name: str) -> bool:
        if name not in self.stages:
            return False
        section = getattr(self, name, None)
        return bool(getattr(section, "enabled", True))

    def enabled_stages(self) -> tuple[str, ...]:
        return tuple(name for name in STAGE_ORDER if self.stage_enabled(name))

    def with_cameras(self, cameras: Sequence[CameraConfig]) -> WorkerConfig:
        return replace(self, cameras=tuple(cameras))

    def shards(self) -> tuple[tuple[CameraConfig, ...], ...]:
        """Split the camera list into per-process groups.

        Returned rather than acted on, so the caller — a supervisor, a systemd
        template, compose — decides how to spawn. The worker itself only ever
        handles one shard.
        """
        n = max(1, self.max_cameras_per_process)
        return tuple(
            tuple(self.cameras[i : i + n]) for i in range(0, len(self.cameras), n)
        )

    @property
    def needs_no_optional_deps(self) -> bool:
        """True when this configuration runs on tier-1 dependencies only."""
        return (
            self.detect.backend in STUB_BACKENDS
            and self.ocr.backend in STUB_BACKENDS
            and all(c.stub for c in self.cameras)
        )

    @staticmethod
    def from_env() -> WorkerConfig:
        specs = _env_list("ANALYTICS_CAMERAS")
        cameras: tuple[CameraConfig, ...]
        blob = _env_json("ANALYTICS_CAMERAS_JSON")
        if blob:
            cameras = tuple(CameraConfig.from_mapping(c) for c in blob)
        else:
            cameras = tuple(CameraConfig.parse(s) for s in specs)

        registry_url = _env("ANALYTICS_REGISTRY_URL", "")
        sink = SinkConfig.from_env()
        if not sink.registry_url and registry_url:
            # One registry doing both jobs is the common case: default the events
            # endpoint from the assignments endpoint's host rather than making an
            # operator configure the same base URL twice. An operator who does need
            # them split sets ANALYTICS_EVENTS_URL explicitly, which always wins
            # because it was read first.
            sink = replace(sink, registry_url=registry_url.rstrip("/") + "/api/analytics/events")
        if not sink.alerts_url and registry_url:
            sink = replace(sink, alerts_url=registry_url.rstrip("/") + "/api/analytics/alerts")

        return WorkerConfig(
            worker_id=_env("ANALYTICS_WORKER_ID", "worker-0"),
            cameras=cameras,
            max_cameras_per_process=_env_int("ANALYTICS_MAX_CAMERAS_PER_PROCESS", 8),
            stages=_env_list("ANALYTICS_STAGES", STAGE_ORDER),
            motion=MotionConfig.from_env(),
            detect=DetectConfig.from_env(),
            plate=PlateConfig.from_env(),
            ocr=OcrConfig.from_env(),
            tracker=TrackerConfig.from_env(),
            source=SourceConfig.from_env(),
            sampling=SamplingConfig.from_env(),
            primitives=PrimitiveConfig.from_env(),
            sink=sink,
            registry_url=registry_url,
            registry_token=_env("ANALYTICS_REGISTRY_TOKEN", ""),
            watchlist_path=_env("ANALYTICS_WATCHLIST_PATH", ""),
            watchlist_refresh_seconds=_env_float("ANALYTICS_WATCHLIST_REFRESH_SECONDS", 60.0),
            log_level=_env("LOG_LEVEL", "INFO"),
            stats_interval_seconds=_env_float("ANALYTICS_STATS_INTERVAL_SECONDS", 30.0),
            run_seconds=_env_float("ANALYTICS_RUN_SECONDS", 0.0),
        )

    def validate(self) -> None:
        """Reject configurations that would produce plausible-looking nonsense."""
        from services.common.events import OBJECT_CLASSES  # noqa: PLC0415

        unknown_stages = [s for s in self.stages if s not in STAGE_ORDER]
        if unknown_stages:
            raise ValueError(
                f"unknown stage(s) {unknown_stages}; valid stages are {list(STAGE_ORDER)}"
            )
        unknown_classes = [c for c in self.detect.classes if c not in OBJECT_CLASSES]
        if unknown_classes:
            raise ValueError(
                f"detect classes {unknown_classes} are not in OBJECT_CLASSES. Add them "
                f"to services/common/events.py if the detector legitimately emits them; "
                f"a class the shared contract does not know cannot be serialised."
            )
        if self.source.transport != "tcp":
            # Not a soft warning. UDP loss on this gateway is silent frame
            # corruption that gets diagnosed as a detector failure, and somebody
            # will spend a day of a nine-day schedule on it.
            raise ValueError(
                "RTSP transport must be 'tcp'. UDP drops packets silently on these "
                "streams and the resulting corrupt frames are misread as detection "
                "failures. Override only if you are not reading RTSP at all."
            )
        if self.sampling.min_stride < 1:
            raise ValueError("min_stride must be at least 1")
        if self.sampling.max_stride < self.sampling.min_stride:
            raise ValueError("max_stride must be >= min_stride")
        if not 0.0 < self.sampling.target_lag_seconds < self.sampling.max_lag_seconds:
            raise ValueError("require 0 < target_lag_seconds < max_lag_seconds")
        if self.source.backoff_cap_seconds < self.source.backoff_initial_seconds:
            raise ValueError("backoff cap must be >= initial delay")
        if not 0.0 <= self.source.backoff_jitter < 1.0:
            raise ValueError("backoff jitter must be in [0, 1)")
        if self.source.backoff_factor < 1.0:
            raise ValueError("backoff factor must be >= 1; a shrinking backoff is not one")
        if self.ocr.accept_confidence < self.ocr.min_confidence:
            raise ValueError("ocr accept_confidence must be >= min_confidence")
        if not 0.0 <= self.plate.band_top < self.plate.band_bottom <= 1.0:
            raise ValueError("require 0 <= band_top < band_bottom <= 1")
        if self.tracker.low_confidence > self.tracker.high_confidence:
            raise ValueError(
                "tracker low_confidence must be <= high_confidence; inverting them "
                "makes weak detections start tracks, which is what the two-tier "
                "scheme exists to prevent"
            )
        if self.motion.grid_rows < 1 or self.motion.grid_cols < 1:
            raise ValueError("motion grid must have at least one row and one column")
        if self.sink.buffer_capacity < 1:
            raise ValueError("sink buffer_capacity must be at least 1")
        if self.sink.batch_size < 1:
            raise ValueError("sink batch_size must be at least 1")
        if self.sink.flush_interval_seconds <= 0:
            raise ValueError("sink flush_interval_seconds must be positive")
        if self.sink.max_post_attempts < 1:
            raise ValueError("sink max_post_attempts must be at least 1")
        seen: set[int] = set()
        for camera in self.cameras:
            if camera.camera_id in seen:
                raise ValueError(
                    f"camera_id {camera.camera_id} appears twice. Two threads writing "
                    f"sightings under one id makes the track ids collide."
                )
            seen.add(camera.camera_id)


def load_cameras_from_registry(
    base_url: str, token: str = "", timeout: float = 10.0
) -> tuple[CameraConfig, ...]:
    """Fetch this worker's camera assignments from the registry. Read-only.

    Stdlib ``urllib`` rather than ``httpx`` or ``requests``, matching ``sink.py``'s
    choice for the same reason: one fewer wheel that can fail to install on an edge
    box the night before a demo, and this worker's own requirements file already
    promises tier 1 needs nothing beyond numpy.

    The registry is the system of record for camera geometry — zone polygons, the
    crossing lines an operator drew, the homography somebody calibrated — so
    fetching it here rather than duplicating it in worker environment variables is
    what keeps the two from drifting. Environment still wins when set, because
    during a live test you sometimes need to point one worker at one stream
    without touching the database.
    """
    if not base_url:
        return ()
    import json as _json
    import urllib.error
    import urllib.request

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = base_url.rstrip("/") + "/api/analytics/assignments"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"registry rejected the assignments request ({exc.code}): {url}"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"could not reach registry at {url}: {exc}") from exc

    rows = payload.get("cameras", payload) if isinstance(payload, dict) else payload
    cameras = tuple(CameraConfig.from_mapping(row) for row in rows)
    log.info("registry returned %d camera assignment(s)", len(cameras))
    return cameras


def resolve_config(argv_cameras: str = "") -> WorkerConfig:
    """Environment, then command line, then registry. In that order of precedence.

    Registry last because it is the one source that can be wrong in a way this
    worker cannot see: a stale assignment row points a worker at a camera that has
    been decommissioned, and during a live test the operator's override has to win.
    """
    config = WorkerConfig.from_env()
    if argv_cameras:
        config = config.with_cameras(
            tuple(CameraConfig.parse(s) for s in argv_cameras.split(",") if s.strip())
        )
    if not config.cameras and config.registry_url:
        config = config.with_cameras(
            load_cameras_from_registry(config.registry_url, config.registry_token)
        )
    return config
