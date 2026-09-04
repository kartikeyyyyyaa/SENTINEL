"""Stages 2 and 3: find the plate, then read it. The expensive end of the funnel.

Stage 2 narrows *objects* to *plate candidates*. Stage 3 narrows plate candidates to
*reads*. They are in one module because they share a vocabulary and because the
rejection gates of the two only make sense read together — stage 2 rejects on what
the geometry can tell you for free, stage 3 rejects on what one convolution over a
small crop can tell you, and the ordering is strictly cheapest-first.

**Stage 2 does not use a model.** PaddleOCR already contains a text detector, so
handing it a region that certainly contains the plate and little else gets the
localisation for free. A dedicated plate detector would be a fourth model to
quantise, ship, keep resident in memory on every edge box and validate — to find a
region that a geometric prior plus a one-dimensional gradient scan already finds.
The prior is that plates sit low and central on a vehicle and are wider than they
are tall, which holds for every vehicle type on an Indian road; the refinement is
that plate glyphs are dense *vertical* strokes while the body seams and shadow lines
dominating a vehicle's lower half are horizontal, so column-wise horizontal-gradient
energy peaks on the plate.

**The gates are ordered by what they cost to evaluate.** In order:

1. ``class_label in PLATED_CLASSES`` — a set membership test. A pedestrian has no
   plate and must not cost a crop.
2. Box size in *pixels* — two multiplications. A 12-pixel-wide plate cannot be read
   by any OCR engine at any price, so cropping it, sharpening it and paying for an
   inference is pure waste. This is the single most common way an ANPR pipeline's
   cost runs away, because a wide-angle road camera is mostly full of vehicles that
   are too far away, and it is why the rejection happens *before* anything is
   copied out of the frame.
3. Aspect ratio — one division on the candidate region.
4. Sharpness — one Laplacian over a few thousand pixels. A motion-blurred plate does
   not become readable by being handed to a better recogniser; it produces a
   *confident wrong string*, and that is the failure that ends up in front of a
   magistrate.
5. Track dedupe — a dict lookup, and the largest single saving in the worker. A
   vehicle crossing frame is forty-plus frames. Reading its plate forty times costs
   forty inferences and creates forty chances to emit a wrong string.

Gate 5 is last in the list and first in effect, which is why it is measured
separately in the cascade stats: without it the other four still leave stage 3
running once per vehicle per frame.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

try:
    import cv2 as _cv2
except ImportError:  # pragma: no cover
    _cv2 = None

from services.common.events import PLATED_CLASSES, BBox

from ..config import OcrConfig, PlateConfig
from .detect import Detection, crop
from .motion import variance_of_laplacian

log = logging.getLogger(__name__)

# Stage 2 rejection reasons.
DROP_NOT_PLATED = "not_plated"
DROP_BOX_TOO_SMALL = "box_too_small"
DROP_PLATE_TOO_NARROW = "plate_too_narrow"
DROP_ASPECT = "bad_aspect"
DROP_EMPTY_CROP = "empty_crop"
DROP_FRAME_BUDGET = "frame_crop_budget"

# Stage 3 rejection reasons.
DROP_ALREADY_READ = "already_read"
DROP_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
DROP_TOO_SMALL = "crop_too_small"
DROP_BLURRED = "crop_blurred"
DROP_LOW_CONFIDENCE = "low_confidence"
DROP_NO_TEXT = "no_text"


@dataclass(frozen=True, slots=True)
class PlateCrop:
    """A candidate plate region, cropped and ready for OCR.

    ``bbox`` is in normalised *frame* coordinates rather than coordinates within the
    vehicle box, so that the crop can be located in the source frame later for an
    evidence export without needing the vehicle box that produced it.
    """

    image: np.ndarray
    bbox: BBox
    track_id: str | None
    class_label: str
    detection_confidence: float
    #: Column-gradient energy at the chosen window, kept for diagnostics. A read
    #: that turns out wrong is much easier to explain when you can see that the
    #: localiser picked a low-energy window and was probably looking at a bumper.
    edge_energy: float = 0.0

    @property
    def width(self) -> int:
        return int(self.image.shape[1]) if self.image.ndim >= 2 else 0

    @property
    def height(self) -> int:
        return int(self.image.shape[0]) if self.image.ndim >= 2 else 0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


@dataclass(frozen=True, slots=True)
class PlateRead:
    """The result of one OCR call.

    ``text`` and ``text_raw`` are both kept, and that is a requirement rather than
    a nicety: an operator reviewing evidence needs to see what the machine actually
    read, not only what normalisation decided that meant. If the raw output was
    ``GJ-O1-AB-1234`` and the normalised form is ``GJO1AB1234``, the difference
    between an ``O`` and a ``0`` is the whole case.

    ``format_valid`` records whether the read matches the Indian plate pattern. It
    is a flag, never a correction — see ``normalise``.
    """

    text: str
    text_raw: str
    confidence: float
    format_valid: bool
    track_id: str | None = None
    bbox: BBox | None = None
    sharpness: float = 0.0


@runtime_checkable
class OcrReader(Protocol):
    """What stage 3 needs from a text recogniser."""

    name: str

    def read(self, image: np.ndarray) -> tuple[str, float]:
        """Return ``(raw_text, confidence)``. Empty string means nothing legible."""
        ...


# ---------------------------------------------------------------------------
# Stage 2: localisation
# ---------------------------------------------------------------------------


class PlateLocaliser:
    """Turns vehicle detections into plate crops, rejecting hopeless ones first.

    Stateless apart from its counters, so one instance can serve several cameras —
    though the worker gives each camera its own, because the counters are per-camera
    diagnostics.
    """

    __slots__ = ("_config", "drops", "crops_emitted")

    def __init__(self, config: PlateConfig | None = None) -> None:
        self._config = config or PlateConfig()
        self.drops: dict[str, int] = {}
        self.crops_emitted = 0

    def locate(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        track_ids: dict[int, str] | None = None,
    ) -> list[PlateCrop]:
        """Crop plate candidates from ``frame``.

        ``track_ids`` maps a detection's index to the track id the tracker assigned
        it. Passed in rather than looked up, because the tracker has already done
        the association work this frame and re-deriving it here by IoU would be a
        second, independent association that could disagree with the first — and
        two disagreeing associations means a plate read attributed to the wrong
        vehicle.
        """
        cfg = self._config
        if not cfg.enabled or frame is None or not len(detections):
            return []

        out: list[PlateCrop] = []
        # Largest boxes first. When the per-frame budget bites, the nearest vehicle
        # is the one whose plate is most likely to be legible, so spending the
        # budget on it is strictly better than spending it on whichever vehicle the
        # detector happened to list first.
        order = sorted(
            range(len(detections)),
            key=lambda i: detections[i].bbox.area,
            reverse=True,
        )
        for idx in order:
            det = detections[idx]
            if det.class_label not in PLATED_CLASSES:
                self._drop(DROP_NOT_PLATED)
                continue
            if len(out) >= max(1, cfg.max_crops_per_frame):
                # Bounds worst-case fan-out. A junction frame with fifteen vehicles
                # would otherwise put fifteen crops into stage 3 in one frame
                # interval, and the resulting lag spike makes the sampler shed the
                # next second of video — losing more than the crops were worth.
                self._drop(DROP_FRAME_BUDGET)
                continue

            box_px = det.pixel_width
            if box_px < cfg.min_vehicle_box_px:
                # Rejected before any pixels are touched.
                self._drop(DROP_BOX_TOO_SMALL)
                continue
            if box_px * cfg.width_fraction < cfg.min_plate_width_px:
                self._drop(DROP_PLATE_TOO_NARROW)
                continue

            candidate = self._plate_box(frame, det)
            if candidate is None:
                self._drop(DROP_EMPTY_CROP)
                continue
            box, energy = candidate
            aspect = (box.width * det.source_width) / max(
                1e-6, box.height * det.source_height
            )
            if not cfg.aspect_min <= aspect <= cfg.aspect_max:
                self._drop(DROP_ASPECT)
                continue

            patch = crop(frame, box)
            if patch.size == 0:
                self._drop(DROP_EMPTY_CROP)
                continue

            out.append(
                PlateCrop(
                    image=patch,
                    bbox=box,
                    track_id=(track_ids or {}).get(idx),
                    class_label=det.class_label,
                    detection_confidence=det.confidence,
                    edge_energy=energy,
                )
            )
            self.crops_emitted += 1
        return out

    def _plate_box(
        self, frame: np.ndarray, det: Detection
    ) -> tuple[BBox, float] | None:
        """Geometric prior, then optional gradient refinement in x."""
        cfg = self._config
        box = det.bbox
        bw, bh = box.width, box.height
        if bw <= 0 or bh <= 0:
            return None

        y1 = box.y1 + bh * cfg.band_top
        y2 = box.y1 + bh * cfg.band_bottom
        plate_w = bw * cfg.width_fraction
        plate_h = plate_w * (det.source_width / max(1, det.source_height)) / max(
            1e-6, cfg.target_aspect
        )
        # ``plate_h`` is computed in normalised units from a *pixel* aspect ratio, so
        # the source frame's own aspect has to be divided back out. Skipping this is
        # a silent bug that makes crops 1.78x too tall on 16:9 video, which the
        # aspect gate then rejects — and the symptom is "ANPR mysteriously reads
        # nothing", ten files away from the cause.
        plate_h = min(plate_h, y2 - y1)
        if plate_h <= 0:
            return None

        cx = box.centre[0]
        x1 = cx - plate_w / 2.0
        energy = 0.0
        if cfg.refine_by_edges:
            refined = self._refine_x(frame, det, y1, y2, plate_w)
            if refined is not None:
                x1, energy = refined

        # Anchor the plate band to the bottom of the search band. Front and rear
        # plates sit at the low end of the lower half on essentially every vehicle;
        # centring in the band picks up the grille as often as the plate.
        py2 = y2
        py1 = py2 - plate_h
        pad = plate_w * cfg.pad_fraction
        try:
            return (
                BBox(
                    max(0.0, x1 - pad),
                    max(0.0, py1 - pad * 0.5),
                    min(1.0, x1 + plate_w + pad),
                    min(1.0, py2 + pad * 0.5),
                ),
                energy,
            )
        except ValueError:
            # Clamping collapsed the box — the vehicle is at the frame edge.
            return None

    def _refine_x(
        self, frame: np.ndarray, det: Detection, y1: float, y2: float, plate_w: float
    ) -> tuple[float, float] | None:
        """Slide a ``plate_w``-wide window across the band; return the best x1.

        The scan is a cumulative sum, so the cost is one pass over the band
        regardless of how many window positions are evaluated. A naive
        window-by-window sum is O(positions x window) and would be a measurable
        fraction of stage 2 on a large vehicle box.
        """
        try:
            band = crop(frame, BBox(det.bbox.x1, y1, det.bbox.x2, y2))
        except ValueError:
            return None
        if band.size == 0 or band.shape[1] < 8:
            return None

        grey = band if band.ndim == 2 else _grey(band)
        if grey is None or grey.size == 0:
            return None
        gx = np.abs(np.diff(grey.astype(np.float32), axis=1))
        column_energy = gx.sum(axis=0)
        if column_energy.size < 4:
            return None

        band_px = float(band.shape[1])
        win = max(4, int(round(plate_w * det.source_width)))
        win = min(win, int(column_energy.size))
        cumulative = np.concatenate(([0.0], np.cumsum(column_energy)))
        windows = cumulative[win:] - cumulative[:-win]
        if windows.size == 0:
            return None
        best = int(np.argmax(windows))
        energy = float(windows[best] / win)
        # Back to normalised frame coordinates via the band's own left edge.
        x1 = det.bbox.x1 + (best / band_px) * det.bbox.width
        return x1, energy

    def _drop(self, reason: str) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Stage 3: recognition
# ---------------------------------------------------------------------------


class StubOcrReader:
    """Deterministic synthetic plate reads. No weights, no network.

    Keyed on the crop's *content*, so the same crop always reads the same string —
    which is what makes the dedupe test meaningful. A stub that returned a random
    plate each call would make "the same track was read once" pass for the wrong
    reason.

    The strings it produces are in Gujarat format (``GJ01AB1234``) because a
    downstream consumer being fed plausibly-shaped plates during a dry run is more
    useful than being fed ``TEST0001``: it exercises the format validation, the
    normalisation and the plate-index lookups on realistic input.
    """

    name = "stub"

    __slots__ = ("calls", "_confidence")

    _DISTRICTS = ("01", "05", "06", "18", "27", "38")
    _LETTERS = ("AB", "CD", "GH", "JK", "MN", "PQ", "XY")

    def __init__(self, confidence: float = 0.86) -> None:
        self.calls = 0
        self._confidence = confidence

    def read(self, image: np.ndarray) -> tuple[str, float]:
        self.calls += 1
        if image is None or image.size == 0:
            return "", 0.0
        # Hash the image bytes rather than its shape: two different vehicles often
        # produce identically-shaped crops, and a shape-keyed stub would give them
        # the same plate, which would hide a genuine cross-track mix-up bug.
        digest = hashlib.blake2b(np.ascontiguousarray(image).tobytes(), digest_size=8).digest()
        n = int.from_bytes(digest, "big")
        district = self._DISTRICTS[n % len(self._DISTRICTS)]
        letters = self._LETTERS[(n >> 8) % len(self._LETTERS)]
        number = f"{(n >> 16) % 10000:04d}"
        return f"GJ {district} {letters} {number}", self._confidence


class PaddleOcrReader:
    """PaddleOCR, imported lazily. Absent is an actionable error, not a fallback.

    Handles both the 2.x and 3.x return shapes because the two are incompatible and
    which one you get depends on the wheel that resolved at install time — which is
    not something an edge deployment can be relied on to pin. 2.x returns
    ``[[[box, (text, score)], ...]]``; 3.x returns a dict with parallel
    ``rec_texts`` / ``rec_scores`` lists. Guessing wrong yields an ``IndexError``
    deep in a hot loop rather than a clear message.
    """

    name = "paddleocr"

    __slots__ = ("_ocr", "calls")

    def __init__(self, config: OcrConfig) -> None:
        self.calls = 0
        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "ANALYTICS_OCR=paddle but 'paddleocr' is not installed. Either "
                "install the optional ML tier:\n"
                "    pip install -r services/analytics/requirements.txt\n"
                "or run without it:\n"
                "    ANALYTICS_OCR=stub\n"
                "The stub emits synthetic plate strings and reads no video."
            ) from exc

        # ``use_angle_cls`` off: the crop's rotation is already constrained by the
        # geometric prior, and the classifier is a second model load for a rotation
        # correction of at most a few degrees.
        self._ocr = PaddleOCR(lang=config.lang, use_angle_cls=False, show_log=False)

    def read(self, image: np.ndarray) -> tuple[str, float]:
        self.calls += 1
        if image is None or image.size == 0:
            return "", 0.0
        result = self._ocr.ocr(image)
        if not result:
            return "", 0.0

        first = result[0]
        if isinstance(first, dict):  # PaddleOCR 3.x
            texts = list(first.get("rec_texts") or [])
            scores = list(first.get("rec_scores") or [])
        else:  # PaddleOCR 2.x
            texts, scores = [], []
            for line in first or []:
                if not line or len(line) < 2:
                    continue
                payload = line[1]
                if isinstance(payload, (list, tuple)) and len(payload) >= 2:
                    texts.append(str(payload[0]))
                    scores.append(float(payload[1]))
        if not texts:
            return "", 0.0
        # Concatenate every line and take the *minimum* score. A plate split across
        # two rows is two lines, and the confidence of the whole read is the
        # confidence of its weakest part — averaging would let one crisp row carry a
        # guessed one over the acceptance threshold.
        return " ".join(texts), min(scores) if scores else 0.0


def build_reader(config: OcrConfig) -> OcrReader:
    backend = (config.backend or "stub").lower()
    if backend == "stub":
        log.warning(
            "OCR backend is 'stub': plate strings are synthetic. Set "
            "ANALYTICS_OCR=paddle for real recognition."
        )
        return StubOcrReader()
    if backend in {"paddle", "paddleocr"}:
        return PaddleOcrReader(config)
    raise ValueError(f"unknown OCR backend {config.backend!r}; expected 'stub' or 'paddle'")


class PlateReadStage:
    """Stage 3. Quality gate, then OCR, then once-per-track dedupe.

    Per-camera state, because the dedupe cache is keyed on track id and track ids
    are only unique within a camera.
    """

    __slots__ = ("_config", "_reader", "_best", "_attempts", "drops", "reads_emitted")

    def __init__(self, config: OcrConfig, reader: OcrReader) -> None:
        self._config = config
        self._reader = reader
        self._best: dict[str, PlateRead] = {}
        self._attempts: dict[str, int] = {}
        self.drops: dict[str, int] = {}
        self.reads_emitted = 0

    @property
    def reader(self) -> OcrReader:
        return self._reader

    def reset(self) -> None:
        """Clear the dedupe cache. Called on a scene change.

        Not optional. Track ids are not reused across a scene change, so a stale
        cache would never be hit again and would simply leak — but worse, if ids
        *were* ever reused the cache would attribute the previous scene's plate to a
        new vehicle, which is a fabricated evidence record.
        """
        self._best.clear()
        self._attempts.clear()

    def known_read(self, track_id: str | None) -> PlateRead | None:
        """The best accepted read for a track, if any. Cheap; no inference."""
        if track_id is None:
            return None
        return self._best.get(track_id)

    def read_all(self, crops: list[PlateCrop]) -> list[PlateRead]:
        cfg = self._config
        if not cfg.enabled or not crops:
            return []
        out: list[PlateRead] = []
        for candidate in crops:
            read = self._read_one(candidate)
            if read is not None:
                out.append(read)
        return out

    def _read_one(self, candidate: PlateCrop) -> PlateRead | None:
        cfg = self._config
        tid = candidate.track_id

        # --- dedupe, before anything is measured let alone inferred -----------
        if cfg.once_per_track and tid is not None:
            existing = self._best.get(tid)
            if existing is not None and existing.confidence >= cfg.accept_confidence:
                self._drop(DROP_ALREADY_READ)
                return None
            if self._attempts.get(tid, 0) >= cfg.max_attempts_per_track:
                # Give up rather than retry forever. A vehicle whose plate is
                # obscured by a bike rack will never read, and the retry loop would
                # spend an inference per frame for the whole time it is in view.
                self._drop(DROP_ATTEMPTS_EXHAUSTED)
                return None

        # --- quality gate ----------------------------------------------------
        if candidate.width < cfg.min_crop_width_px or candidate.height < cfg.min_crop_height_px:
            self._drop(DROP_TOO_SMALL)
            return None
        if not cfg.aspect_min <= candidate.aspect <= cfg.aspect_max:
            self._drop(DROP_ASPECT)
            return None
        sharpness = variance_of_laplacian(candidate.image)
        if sharpness < cfg.min_laplacian_variance:
            self._drop(DROP_BLURRED)
            return None

        # --- the expensive part ----------------------------------------------
        if tid is not None:
            self._attempts[tid] = self._attempts.get(tid, 0) + 1
        raw, confidence = self._reader.read(candidate.image)
        if not raw.strip():
            self._drop(DROP_NO_TEXT)
            return None

        text = normalise(raw)
        if not text:
            self._drop(DROP_NO_TEXT)
            return None
        if confidence < cfg.min_confidence:
            # Not emitted at all. A 0.2-confidence plate string is not evidence, and
            # putting it on the bus invites a consumer to treat it as one.
            self._drop(DROP_LOW_CONFIDENCE)
            return None

        read = PlateRead(
            text=text,
            text_raw=raw.strip(),
            confidence=float(confidence),
            format_valid=bool(re.match(cfg.plate_pattern, text)),
            track_id=tid,
            bbox=candidate.bbox,
            sharpness=sharpness,
        )
        if tid is not None:
            previous = self._best.get(tid)
            if previous is None or read.confidence > previous.confidence:
                self._best[tid] = read
            elif cfg.once_per_track:
                # A worse read of an already-read track is not news. Emitting it
                # would put two contradictory plate strings for one vehicle on the
                # bus and leave the correlation layer to guess which is right.
                return None
        self.reads_emitted += 1
        return read

    def _drop(self, reason: str) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1


_NON_PLATE = re.compile(r"[^A-Z0-9]")


def normalise(raw: str) -> str:
    """Uppercase and strip separators. Nothing else. No character substitution.

    The tempting version of this function fixes the classic OCR confusions —
    ``O``/``0``, ``I``/``1``, ``S``/``5``, ``B``/``8`` — using the Indian plate
    format to decide which positions must be letters and which must be digits. It
    would raise the apparent match rate and it is not implemented here on purpose.

    A substituted character is a *fabricated* character. If ``GJ01AB1234`` is
    actually ``GJ0IAB1234`` and this function rewrote it to match the format, the
    resulting evidence record says the machine read something it did not read, and
    the operator has no way to tell. Sentinel keeps both forms on every read and
    exposes ``format_valid`` as a flag, so a consumer that wants to search
    fuzzily can do so explicitly and on the record — which is the right layer for
    that decision, because there it is reversible and auditable.
    """
    if not raw:
        return ""
    return _NON_PLATE.sub("", raw.upper())


def _grey(image: np.ndarray) -> np.ndarray | None:
    if image.ndim == 2:
        return image
    if _cv2 is not None and image.shape[2] == 3:
        return _cv2.cvtColor(image, _cv2.COLOR_BGR2GRAY)
    if image.shape[2] >= 3:
        b, g, r = image[..., 0], image[..., 1], image[..., 2]
        return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.float32)
    return image[..., 0]


@dataclass(slots=True)
class PlateBudget:
    """Optional hard cap on OCR calls per second, per worker.

    Not enabled by default. It exists because stage 3's cost is the one part of the
    pipeline an adversary can drive: a vehicle deliberately parked so that its plate
    sits at the sharpness threshold will fail the accept check every frame, retry
    until ``max_attempts_per_track``, and — if it re-enters as a new track — start
    again. The per-track attempt cap bounds that per vehicle; this bounds it for the
    process, which is what protects the other cameras sharing the box.
    """

    max_calls_per_second: float = 0.0
    _window_start: float = field(default=0.0, repr=False)
    _calls_in_window: int = field(default=0, repr=False)

    def allow(self, now: float) -> bool:
        if self.max_calls_per_second <= 0:
            return True
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._calls_in_window = 0
        if self._calls_in_window >= self.max_calls_per_second:
            return False
        self._calls_in_window += 1
        return True
