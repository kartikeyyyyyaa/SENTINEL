"""Stage 2: vehicle and person detection.

Only frames the motion gate passed reach here, which is the whole reason a
YOLOv8n int8 model on modest hardware can serve tens of cameras.

**Nothing in this project is trained.** The detector is pretrained COCO weights
used exactly as shipped. The reasoning is in the service README; the short version
is that AI accuracy is one of ten evaluation areas, no labelled Gujarat dataset
exists to fine-tune against, and at fifty concurrent streams the thing that
breaks is plumbing and false-positive rate, not mAP. A day spent on the false-
positive rules downstream buys more than a week spent chasing two points of mAP.

**Letterboxing is per frame, not per fleet.** The streams mix H.264 with H.265 and
mix resolutions, so there is no fixed-shape batch available. An int8 session is
compiled for one input shape; feeding it a different one either fails or silently
triggers a re-plan on every resolution change, which costs more than the
inference. So every frame is letterboxed individually to the model's square input
and the resulting boxes are mapped back to source coordinates before they leave
this module. Nothing downstream should ever have to know what input size was used.

Two implementations behind one protocol. ``UltralyticsDetector`` is the real one
and imports ultralytics lazily, so a machine without it — or without weights —
still starts the worker. ``StubDetector`` is deterministic and dependency-free,
and is what the tests and the no-GPU dry run use.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import numpy as np

from ..config import DetectConfig
from ..primitives import Box, Detection
from . import Stage, StageContext

log = logging.getLogger(__name__)


@runtime_checkable
class Detector(Protocol):
    """What stage 2 needs from a detection model.

    A protocol rather than a base class so a future ONNX Runtime or OpenVINO
    backend does not have to inherit from anything, and so the stub is not a
    subclass of something that drags in torch.

    Implementations receive a **letterboxed, model-sized** image and return boxes
    in that image's coordinates. Mapping back to the source frame is the stage's
    job, done once, in one place.
    """

    name: str

    def infer(self, image: np.ndarray) -> list[tuple[tuple[float, float, float, float], str, float]]:
        """Return ``((x1, y1, x2, y2), label, confidence)`` in input coordinates."""
        ...


class ModelUnavailable(RuntimeError):
    """A model backend could not be loaded. Message must say what to do next."""


def letterbox(
    image: np.ndarray, size: int, pad_value: int = 114
) -> tuple[np.ndarray, float, float, float]:
    """Resize preserving aspect ratio and pad to ``size`` x ``size``.

    Returns the padded image plus the ``(scale, pad_x, pad_y)`` needed to undo the
    transform. Aspect ratio is preserved because squashing a 16:9 frame into a
    square distorts every box's aspect, and the plate stage's whole method is an
    aspect-ratio prior — a stretched frame makes plates look like the wrong shape
    and stage 3 stops finding them.

    ``pad_value`` is grey rather than black: black padding creates a hard edge that
    a convolutional detector will occasionally fire on, producing detections at the
    letterbox seam.
    """
    h, w = image.shape[:2]
    if h == 0 or w == 0:
        raise ValueError("cannot letterbox an empty frame")
    scale = min(size / w, size / h)
    new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = _resize(image, new_w, new_h)
    channels = image.shape[2] if image.ndim == 3 else 1
    shape = (size, size, channels) if image.ndim == 3 else (size, size)
    canvas = np.full(shape, pad_value, dtype=image.dtype)
    pad_x = (size - new_w) / 2.0
    pad_y = (size - new_h) / 2.0
    top, left = int(pad_y), int(pad_x)
    canvas[top : top + new_h, left : left + new_w] = resized
    return canvas, scale, float(left), float(top)


def unletterbox_box(
    x1: float, y1: float, x2: float, y2: float, scale: float, pad_x: float, pad_y: float
) -> Box:
    """Map a box from letterboxed input coordinates back to the source frame."""
    inv = 1.0 / scale if scale else 1.0
    return Box(
        x1=(x1 - pad_x) * inv,
        y1=(y1 - pad_y) * inv,
        x2=(x2 - pad_x) * inv,
        y2=(y2 - pad_y) * inv,
    )


def _resize(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """cv2 if present, nearest-neighbour numpy if not.

    The numpy path exists so the stage is testable in a bare environment. It is
    nearest-neighbour and therefore aliases; that is acceptable for a stub
    detector and would not be acceptable in front of a real model, which is why
    the real path requires cv2 (it ships with opencv-python alongside the model
    dependencies anyway).
    """
    try:
        import cv2  # noqa: PLC0415 - optional, see docstring

        return cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
    except ImportError:  # pragma: no cover - environment-dependent
        h, w = image.shape[:2]
        ys = (np.arange(height) * (h / height)).astype(np.int64).clip(0, h - 1)
        xs = (np.arange(width) * (w / width)).astype(np.int64).clip(0, w - 1)
        return image[ys][:, xs]


class StubDetector:
    """Deterministic detector with no model and no dependencies.

    Finds the bounding box of the brightest region in the frame. That is enough to
    make ``SyntheticSource``'s moving block produce a real track, a real speed
    estimate and a real plate crop, so the dry run exercises every line of the
    pipeline that the live path would — association, ageing, reset at the loop cut,
    event serialisation, sink backpressure.

    It is a single-blob finder over an axis projection. It would be useless on
    real video and is not pretending otherwise: its job is to make the *plumbing*
    testable without weights, on the principle that the plumbing is what will
    actually fail on demo day.

    ``scripted`` overrides everything and returns pre-set boxes per call, which is
    how the tracker and cascade tests drive exact scenarios.
    """

    name = "stub"

    def __init__(
        self,
        label: str = "car",
        brightness: int = 200,
        min_area_fraction: float = 0.0005,
        confidence: float = 0.92,
        scripted: list[list[tuple[tuple[float, float, float, float], str, float]]] | None = None,
    ) -> None:
        self.label = label
        self.brightness = brightness
        self.min_area_fraction = min_area_fraction
        self.confidence = confidence
        self.scripted = scripted
        self.calls = 0

    def infer(
        self, image: np.ndarray
    ) -> list[tuple[tuple[float, float, float, float], str, float]]:
        call = self.calls
        self.calls += 1
        if self.scripted is not None:
            if not self.scripted:
                return []
            return list(self.scripted[call % len(self.scripted)])
        gray = image.mean(axis=2) if image.ndim == 3 else image
        mask = gray >= self.brightness
        if not mask.any():
            return []
        rows = np.flatnonzero(mask.any(axis=1))
        cols = np.flatnonzero(mask.any(axis=0))
        y1, y2 = float(rows[0]), float(rows[-1] + 1)
        x1, x2 = float(cols[0]), float(cols[-1] + 1)
        if (x2 - x1) * (y2 - y1) < self.min_area_fraction * gray.size:
            return []
        return [((x1, y1, x2, y2), self.label, self.confidence)]


class UltralyticsDetector:
    """YOLOv8n via ultralytics. The real backend.

    The import is inside ``__init__`` rather than at module scope so that the
    worker starts, self-tests and runs its dry run on a machine with neither
    ultralytics nor torch installed. That is not hypothetical convenience: the
    development machines for this project do not have a GPU, and a module-level
    import of torch would make every unit test in this service unrunnable there.
    """

    name = "ultralytics"

    def __init__(self, config: DetectConfig) -> None:
        self.config = config
        try:
            from ultralytics import YOLO  # noqa: PLC0415 - lazy on purpose
        except ImportError as exc:
            raise ModelUnavailable(
                "ultralytics is not installed, so the real detector cannot load. "
                "Either install it:\n"
                "    pip install -r services/analytics/requirements.txt\n"
                "or run without models using the deterministic stub:\n"
                "    python -m worker.main --dry-run\n"
                "The dry run exercises the full cascade, tracker and sink on "
                "synthetic frames and needs no GPU and no weights."
            ) from exc
        try:
            self._model = YOLO(config.model_path, task="detect")
        except Exception as exc:  # noqa: BLE001 - file missing, wrong format, bad export
            raise ModelUnavailable(
                f"could not load detector weights from {config.model_path!r}: {exc}. "
                "Export an int8 model first, e.g.\n"
                "    yolo export model=yolov8n.pt format=onnx int8=True imgsz="
                f"{config.input_size}\n"
                "and point ANALYTICS_DETECT_MODEL at the result. Set "
                "ANALYTICS_DETECT_MODEL='' and use --dry-run to run without weights."
            ) from exc
        self._names: dict[int, str] = dict(getattr(self._model, "names", {}) or {})
        self._wanted = set(config.classes)

    def infer(
        self, image: np.ndarray
    ) -> list[tuple[tuple[float, float, float, float], str, float]]:
        # imgsz is passed explicitly and equals the letterboxed size, so
        # ultralytics does not re-letterbox and no shape renegotiation happens
        # when the next camera's frames are a different resolution.
        results = self._model.predict(
            source=image,
            imgsz=self.config.input_size,
            conf=self.config.low_conf_threshold,
            iou=self.config.nms_iou,
            device=self.config.device,
            verbose=False,
        )
        out: list[tuple[tuple[float, float, float, float], str, float]] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            names = dict(getattr(result, "names", {}) or {}) or self._names
            for xyxy, cls, conf in zip(
                boxes.xyxy.tolist(), boxes.cls.tolist(), boxes.conf.tolist()
            ):
                label = names.get(int(cls), str(int(cls)))
                if self._wanted and label not in self._wanted:
                    continue  # Discarded here so it never costs the tracker anything.
                out.append(((xyxy[0], xyxy[1], xyxy[2], xyxy[3]), label, float(conf)))
        return out


class DetectStage(Stage):
    """Runs the detector on gated frames and returns source-coordinate detections.

    Emits everything above ``low_conf_threshold``, not above ``conf_threshold``.
    The weak band between the two is what the tracker's second association pass
    needs; filtering it out here would make the ByteTrack step a no-op.
    """

    name = "detect"

    def __init__(self, config: DetectConfig, detector: Detector) -> None:
        super().__init__(enabled=config.enabled)
        self.config = config
        self.detector = detector
        self._wanted = set(config.classes)
        self.errors = 0
        self.boxes_discarded = 0

    def run(self, ctx: StageContext, items: list[Any]) -> list[Any]:
        detections: list[Detection] = []
        for frame in items:
            try:
                padded, scale, pad_x, pad_y = letterbox(frame.image, self.config.input_size)
                raw = self.detector.infer(padded)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the camera
                self.errors += 1
                log.debug("camera %s detect failed on frame: %s", ctx.camera_id, exc)
                continue
            for (x1, y1, x2, y2), label, conf in raw:
                if self._wanted and label not in self._wanted:
                    self.boxes_discarded += 1
                    continue
                if conf < self.config.low_conf_threshold:
                    self.boxes_discarded += 1
                    continue
                box = unletterbox_box(x1, y1, x2, y2, scale, pad_x, pad_y).clip(
                    frame.width, frame.height
                )
                if box.area <= 0:
                    self.boxes_discarded += 1
                    continue
                detections.append(
                    Detection(box=box, label=label, confidence=float(conf), t=ctx.t)
                )
        ctx.extras["detections"] = detections
        return list(detections)

    def stats(self) -> dict[str, Any]:
        return {
            "backend": getattr(self.detector, "name", type(self.detector).__name__),
            "errors": self.errors,
            "boxes_discarded": self.boxes_discarded,
        }


def build_detector(config: DetectConfig, prefer_stub: bool = False) -> Detector:
    """Pick a backend.

    Falling back to the stub on a load failure would be the wrong default: a
    worker that silently analyses nothing while reporting healthy is worse than one
    that refuses to start. So the fallback is explicit — ``prefer_stub``, set by
    ``--dry-run``.
    """
    if prefer_stub or not config.model_path:
        return StubDetector()
    return UltralyticsDetector(config)
