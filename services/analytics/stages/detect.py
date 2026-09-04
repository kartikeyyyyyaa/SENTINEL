"""Stage 1: object detection. The first stage that costs real money.

Runs only on frames stage 0 passed, which is the whole point of stage 0 existing.

Three things here are structural rather than incidental.

**The backend is behind a protocol with a working stub.** ``Detector`` is a
``Protocol``, ``UltralyticsDetector`` imports ``ultralytics`` lazily inside its
constructor, and ``StubDetector`` produces deterministic synthetic boxes from the
frame index. This is not a testing convenience bolted on afterwards: the machines
this is being written on have no GPU, no weights and no route to the model
registry, and a module-level ``import torch`` would make the tracker, the
primitives and the whole cascade untestable here. The stub is deterministic so that
a tracker test asserting "these two objects cross and keep their ids" is a
repeatable assertion rather than a flake.

**Every frame is letterboxed individually.** The grid mixes H.264 with H.265 and
mixes resolutions, and a camera renegotiates its profile mid-stream. There is no
fixed input shape to batch against, so there is no batching, and pretending
otherwise produces either a crash on the first resolution change or a silently
stretched frame whose boxes are wrong by the aspect ratio. Letterboxing preserves
aspect ratio; ``unletterbox`` maps the boxes back to normalised source coordinates.

**Boxes leave this stage normalised.** ``services.common.events.BBox`` is in [0, 1]
and this is the only place that knows the source resolution, so this is the only
place that can do the conversion honestly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

try:
    import cv2 as _cv2
except ImportError:  # pragma: no cover
    _cv2 = None

from services.common.events import OBJECT_CLASSES, PLATED_CLASSES, BBox

from ..config import DetectConfig

log = logging.getLogger(__name__)

#: COCO class name to Sentinel class name.
#:
#: The gaps are deliberate and they are documented rather than papered over. A
#: COCO-trained model has no ``auto_rickshaw`` class and no ``tractor`` class, so
#: an auto-rickshaw arrives here as ``car`` or ``truck`` and a tractor as ``truck``.
#: Both are in ``OBJECT_CLASSES`` because the vocabulary has to admit them for a
#: later refinement step to have somewhere to put its answer — but this mapping
#: will not invent them, because a guess based on a bounding box's aspect ratio
#: would be wrong often enough to poison every downstream count of vehicle types.
#: The honest state of affairs: Sentinel currently under-reports auto-rickshaws as
#: cars. Fixing it needs a fine-tune on Indian road imagery, which is out of scope
#: for a nine-day build and is listed as future work rather than claimed as done.
COCO_TO_SENTINEL: dict[str, str] = {
    "person": "person",
    "bicycle": "bicycle",
    "car": "car",
    "motorcycle": "motorcycle",
    "bus": "bus",
    "truck": "truck",
    "train": "unknown",
    "boat": "unknown",
    "airplane": "unknown",
    "bird": "animal",
    "cat": "animal",
    "dog": "animal",
    "horse": "animal",
    "sheep": "animal",
    "cow": "animal",
    "elephant": "animal",
    "bear": "animal",
    "zebra": "animal",
    "giraffe": "animal",
}


@dataclass(frozen=True, slots=True)
class Detection:
    """One box from the detector, in normalised source-frame coordinates.

    ``bbox`` is a ``services.common.events.BBox``, which validates its own range and
    positive area on construction — so a detector returning ``x2 < x1`` fails here
    rather than silently poisoning every IoU the tracker computes.

    ``source_width`` and ``source_height`` are carried because stage 2's rejection
    gates are in *pixels*. Legibility is a function of how many photosites the
    glyphs landed on, which normalised coordinates deliberately discard, so the
    pixel dimensions have to travel with the box.
    """

    bbox: BBox
    class_label: str
    confidence: float
    source_width: int = 0
    source_height: int = 0

    @property
    def pixel_width(self) -> float:
        return self.bbox.width * self.source_width

    @property
    def pixel_height(self) -> float:
        return self.bbox.height * self.source_height

    @property
    def is_plated(self) -> bool:
        return self.class_label in PLATED_CLASSES


@runtime_checkable
class Detector(Protocol):
    """What the cascade needs from an object detector. Nothing more.

    ``frame_index`` is in the signature for the stub's benefit and is ignored by
    real detectors. It is cheaper than a second protocol, and a deterministic stub
    is worth a redundant parameter: without it, the stub would need its own frame
    counter and would then produce different results depending on how many times a
    test had constructed it.
    """

    name: str

    def detect(self, frame: np.ndarray, frame_index: int = 0) -> list[Detection]: ...


class StubDetector:
    """Deterministic synthetic detections. No weights, no network, no GPU.

    The scene is fixed: a handful of objects on straight-line trajectories, derived
    entirely from ``frame_index``. That determinism is what makes the tracker's
    hard cases testable — two objects that cross at a known frame, an object that
    passes behind an occluder for a known interval — as assertions rather than as
    hopeful observations of a random stream.

    One object is deliberately far away and tiny. It exists so that stage 2's
    pre-spend size rejection has something real to reject in every test run: a
    cascade whose tiny-box gate is never exercised is a cascade whose tiny-box gate
    might not work.
    """

    name = "stub"

    #: (label, y-centre, height, speed per frame, x offset, confidence)
    #: Chosen so the ``car`` and the ``person`` cross around frame 25 at similar
    #: scale, which is the ID-switch case the tracker tests target.
    _ACTORS: tuple[tuple[str, float, float, float, float, float], ...] = (
        ("car", 0.62, 0.22, 0.012, 0.05, 0.88),
        ("person", 0.60, 0.20, -0.012, 0.75, 0.74),
        ("motorcycle", 0.80, 0.16, 0.008, 0.20, 0.66),
        # Distant vehicle: 3% of frame width. At 1280 px source that is a ~38 px
        # box, so its plate is single-digit pixels wide and unreadable by anything.
        ("truck", 0.34, 0.035, 0.004, 0.10, 0.52),
    )

    __slots__ = ("_config", "calls")

    def __init__(self, config: DetectConfig | None = None) -> None:
        self._config = config or DetectConfig()
        self.calls = 0

    def detect(self, frame: np.ndarray, frame_index: int = 0) -> list[Detection]:
        self.calls += 1
        h, w = _frame_shape(frame)
        allowed = set(self._config.classes)
        out: list[Detection] = []
        for label, cy, height, speed, x0, conf in self._ACTORS:
            if label not in allowed:
                continue
            width = height * (2.0 if label in {"car", "truck", "bus"} else 0.9)
            # Bounce rather than wrap. A wrap teleports the object across the frame,
            # which would create an ID switch the tracker is *right* to make and
            # would therefore make the tracker tests assert the wrong thing.
            span = max(1e-3, 1.0 - width)
            raw = x0 + speed * frame_index
            phase = raw % (2.0 * span)
            x1 = phase if phase <= span else 2.0 * span - phase
            x1 = min(max(x1, 0.0), span)
            y1 = max(0.0, cy - height / 2.0)
            y2 = min(1.0, y1 + height)
            if y2 <= y1:
                continue
            out.append(
                Detection(
                    bbox=BBox(x1, y1, min(1.0, x1 + width), y2),
                    class_label=label,
                    confidence=conf,
                    source_width=w,
                    source_height=h,
                )
            )
        return out


class UltralyticsDetector:
    """YOLOv8n via ultralytics. Imported lazily; absent is an actionable error.

    Deliberately *not* falling back to the stub when the import fails. A worker that
    quietly analyses synthetic data while reporting healthy is worse than one that
    refuses to start: the first produces an empty incident log that looks like a
    quiet night, and nobody finds out until an incident is missed.

    ``int8`` where the runtime supports it. On a CPU-only edge box the quantised
    model is roughly three times faster than fp32 at a small mAP cost, and the
    cascade's stage 0 has already removed the frames where that mAP cost would
    matter — an empty road misdetected is nothing misdetected.
    """

    name = "ultralytics"

    __slots__ = ("_config", "_model", "_names", "calls")

    def __init__(self, config: DetectConfig) -> None:
        self._config = config
        self.calls = 0
        try:
            from ultralytics import YOLO  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "ANALYTICS_DETECTOR=ultralytics but the 'ultralytics' package is not "
                "installed. Either install the optional ML tier:\n"
                "    pip install -r services/analytics/requirements.txt\n"
                "or run without weights:\n"
                "    ANALYTICS_DETECTOR=stub\n"
                "The stub emits synthetic detections and is for pipeline testing "
                "only — it does not analyse the video."
            ) from exc

        self._model = YOLO(config.model_path)
        # ``model.names`` is a dict[int, str] of COCO names. Resolved once here
        # rather than per frame; it is a dict lookup on the hot path otherwise.
        self._names = dict(getattr(self._model, "names", {}) or {})

    def detect(self, frame: np.ndarray, frame_index: int = 0) -> list[Detection]:
        self.calls += 1
        cfg = self._config
        h, w = _frame_shape(frame)
        padded, scale, pad_x, pad_y = letterbox(frame, cfg.input_size)

        results = self._model.predict(
            padded,
            conf=cfg.low_conf_threshold,  # Low tier: the tracker needs the weak ones.
            iou=cfg.nms_iou,
            imgsz=cfg.input_size,
            device=cfg.device,
            verbose=False,
        )
        allowed = set(cfg.classes)
        out: list[Detection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            xyxy = boxes.xyxy.tolist() if hasattr(boxes.xyxy, "tolist") else list(boxes.xyxy)
            confs = boxes.conf.tolist() if hasattr(boxes.conf, "tolist") else list(boxes.conf)
            classes = boxes.cls.tolist() if hasattr(boxes.cls, "tolist") else list(boxes.cls)
            for (x1, y1, x2, y2), conf, cls in zip(xyxy, confs, classes):
                coco = self._names.get(int(cls), "")
                label = COCO_TO_SENTINEL.get(coco, "unknown")
                if label not in allowed:
                    # Dropped here rather than downstream so it never costs the
                    # tracker an association attempt. A motorway camera sees a lot
                    # of ``traffic light``.
                    continue
                box = unletterbox(
                    (float(x1), float(y1), float(x2), float(y2)),
                    scale=scale,
                    pad_x=pad_x,
                    pad_y=pad_y,
                    source_width=w,
                    source_height=h,
                )
                if box is None:
                    continue
                out.append(
                    Detection(
                        bbox=box,
                        class_label=label,
                        confidence=float(conf),
                        source_width=w,
                        source_height=h,
                    )
                )
        return out


def build_detector(config: DetectConfig) -> Detector:
    """Construct the configured backend. Selected by name, never probed.

    Probing — "try ultralytics, fall back to stub" — is the tempting version and it
    is wrong, for the reason in ``UltralyticsDetector``'s docstring. Choosing the
    stub has to be a decision an operator made and the log records.
    """
    backend = (config.backend or "stub").lower()
    if backend == "stub":
        log.warning(
            "detector backend is 'stub': synthetic detections only, no video is "
            "being analysed. Set ANALYTICS_DETECTOR=ultralytics for real inference."
        )
        return StubDetector(config)
    if backend in {"ultralytics", "yolo", "yolov8"}:
        return UltralyticsDetector(config)
    raise ValueError(
        f"unknown detector backend {config.backend!r}; expected 'stub' or 'ultralytics'"
    )


# ---------------------------------------------------------------------------
# Letterboxing
# ---------------------------------------------------------------------------


def letterbox(
    frame: np.ndarray, size: int, fill: int = 114
) -> tuple[np.ndarray, float, int, int]:
    """Resize to fit a ``size`` x ``size`` square, preserving aspect ratio.

    Returns the padded image and the parameters needed to invert the transform.
    ``fill`` is 114 to match the value YOLO trains with; a black border is a strong
    edge the model has not seen during training and it produces spurious detections
    along it.

    Padding is applied on the right and bottom only rather than centred. Centring is
    what ultralytics does and both are correct, but one-sided padding makes the
    inverse transform a subtraction of a single known offset, which is one fewer
    place for an off-by-half-a-pixel error to hide in a coordinate conversion that
    every downstream box depends on.
    """
    h, w = _frame_shape(frame)
    if h <= 0 or w <= 0:
        raise ValueError("cannot letterbox an empty frame")
    scale = min(size / float(w), size / float(h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))

    if _cv2 is not None:
        # INTER_LINEAR when upscaling, INTER_AREA when down. Using AREA to upscale
        # produces blocky output; using LINEAR to downscale aliases.
        interp = _cv2.INTER_AREA if scale < 1.0 else _cv2.INTER_LINEAR
        resized = _cv2.resize(frame, (new_w, new_h), interpolation=interp)
    else:  # pragma: no cover - nearest-neighbour fallback, cv2 is tier 1
        ys = (np.arange(new_h) * (h / new_h)).astype(np.int32)
        xs = (np.arange(new_w) * (w / new_w)).astype(np.int32)
        resized = frame[np.ix_(ys, xs)] if frame.ndim == 2 else frame[np.ix_(ys, xs, np.arange(frame.shape[2]))]

    shape = (size, size) if resized.ndim == 2 else (size, size, resized.shape[2])
    padded = np.full(shape, fill, dtype=resized.dtype)
    padded[:new_h, :new_w] = resized
    return padded, scale, 0, 0


def unletterbox(
    box: tuple[float, float, float, float],
    *,
    scale: float,
    pad_x: int,
    pad_y: int,
    source_width: int,
    source_height: int,
) -> BBox | None:
    """Map a letterboxed pixel box back to a normalised source-frame ``BBox``.

    Returns ``None`` for a box that does not survive the round trip — zero area
    after clamping, or entirely outside the frame. Returning ``None`` rather than
    raising because a detector occasionally emits a degenerate box on a frame edge
    and one bad box must not take down the frame.
    """
    if scale <= 0 or source_width <= 0 or source_height <= 0:
        return None
    x1 = (box[0] - pad_x) / scale
    y1 = (box[1] - pad_y) / scale
    x2 = (box[2] - pad_x) / scale
    y2 = (box[3] - pad_y) / scale

    nx1 = min(max(x1 / source_width, 0.0), 1.0)
    ny1 = min(max(y1 / source_height, 0.0), 1.0)
    nx2 = min(max(x2 / source_width, 0.0), 1.0)
    ny2 = min(max(y2 / source_height, 0.0), 1.0)
    if nx2 <= nx1 or ny2 <= ny1:
        return None
    try:
        return BBox(nx1, ny1, nx2, ny2)
    except ValueError:
        return None


def crop(frame: np.ndarray, box: BBox) -> np.ndarray:
    """Extract a normalised box from a frame as pixels.

    Clamped to the frame, and returns an empty array rather than raising on a box
    that clamps to nothing. ``BBox`` permits coordinates slightly outside [0, 1] —
    deliberately, so that objects entering frame are not discarded — which means
    every consumer of a ``BBox`` has to clamp, and doing it in one place is how that
    stays true.
    """
    h, w = _frame_shape(frame)
    x1 = min(max(int(round(box.x1 * w)), 0), w)
    x2 = min(max(int(round(box.x2 * w)), 0), w)
    y1 = min(max(int(round(box.y1 * h)), 0), h)
    y2 = min(max(int(round(box.y2 * h)), 0), h)
    if x2 <= x1 or y2 <= y1:
        return np.empty((0, 0), dtype=frame.dtype)
    return frame[y1:y2, x1:x2]


def _frame_shape(frame: np.ndarray) -> tuple[int, int]:
    if frame is None or not hasattr(frame, "shape") or len(frame.shape) < 2:
        return (0, 0)
    return int(frame.shape[0]), int(frame.shape[1])


def validate_class_map() -> None:
    """Assert every mapped label is in the shared vocabulary.

    Called from the self-test. A label that is not in ``OBJECT_CLASSES`` raises
    inside ``Sighting.__post_init__``, which is a hundred lines and one queue away
    from the mapping table that caused it — so it is worth catching at startup, in
    the file where the fix belongs.
    """
    unknown = sorted(set(COCO_TO_SENTINEL.values()) - set(OBJECT_CLASSES))
    if unknown:
        raise AssertionError(
            f"COCO_TO_SENTINEL maps to label(s) not in OBJECT_CLASSES: {unknown}. "
            f"Either fix the mapping or add them to services/common/events.py."
        )
