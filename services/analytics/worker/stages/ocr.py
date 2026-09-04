"""Stage 4: plate OCR. The expensive one.

Target is roughly 2% of decoded frames. If that number climbs, the whole cost
argument for the platform weakens, so stage 4 counts its own calls and the cascade
reports them.

**Once per track, not once per frame.** This is the single biggest saving in the
pipeline and it also improves accuracy. A vehicle crossing the frame is forty-plus
frames; reading its plate forty times costs forty inferences and produces forty
chances to emit a wrong string. One read per track, retried only while confidence
is below ``accept_confidence`` and capped at ``max_attempts_per_track``, gets the
cost down by more than an order of magnitude and gets the false-read rate down with
it.

**Normalisation never invents characters.** Uppercasing and stripping separators is
safe. O/0 and I/1 substitution to force a string into the expected Indian format is
not: it manufactures a plate that was never read. A read that does not match the
format is emitted with ``format_valid=False`` and the rule engine decides. The
alternative — quietly correcting — is how an ANPR system produces a confident hit
on a vehicle that was never there, and that is the failure mode that ends up in
front of a magistrate.

**Confidence is always carried.** See ``primitives.PlateRead``.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Protocol, runtime_checkable

import numpy as np

from ..config import OcrConfig
from ..primitives import PlateCrop, PlateRead
from . import Stage, StageContext

log = logging.getLogger(__name__)

# Characters that are never part of a plate string once separators are stripped.
_STRIP = re.compile(r"[^A-Z0-9]")


@runtime_checkable
class PlateReader(Protocol):
    """What stage 4 needs from an OCR engine.

    Takes a crop, returns text and confidence. Returning ``None`` means "no text
    found", which is different from "text found with low confidence" — the first
    is a crop that was not a plate, the second is a plate we could not read, and
    only the second is worth retrying.
    """

    name: str

    def read(self, crop: np.ndarray) -> tuple[str, float] | None: ...


class ReaderUnavailable(RuntimeError):
    """An OCR backend could not be loaded. Message must say what to do next."""


def normalise(text: str) -> str:
    """Uppercase and strip separators. Nothing else.

    Explicitly *not* a corrector. See the module docstring.
    """
    return _STRIP.sub("", text.upper())


class StubReader:
    """Deterministic OCR with no model.

    Derives a plate string from a hash of the crop's shape and pixel statistics, so
    the same crop always reads the same and different crops read differently. That
    is enough to exercise the once-per-track logic, the confidence gate, the format
    validator and the sink's serialisation without weights.

    It is not pretending to be OCR. Its value is that the 2%-of-frames figure the
    cascade reports is measured on the real control flow, with only the model
    swapped out.
    """

    name = "stub"

    # Plausible Gujarat series. Format-valid on purpose, so the validator's happy
    # path is exercised; ``_MALFORMED`` covers the other branch.
    _PLATES = (
        "GJ01AB1234",
        "GJ05CD5678",
        "GJ18EF9012",
        "GJ27GH3456",
        "GJ03JK7890",
        "MH12LM2468",
    )
    _MALFORMED = ("GJ01", "1234ABCD", "")

    def __init__(
        self,
        scripted: list[tuple[str, float] | None] | None = None,
        malformed_every: int = 7,
        base_confidence: float = 0.55,
    ) -> None:
        self.scripted = scripted
        self.malformed_every = malformed_every
        self.base_confidence = base_confidence
        self.calls = 0

    def read(self, crop: np.ndarray) -> tuple[str, float] | None:
        call = self.calls
        self.calls += 1
        if self.scripted is not None:
            if not self.scripted:
                return None
            return self.scripted[call % len(self.scripted)]
        if crop is None or crop.size == 0:
            return None
        digest = hashlib.sha256(
            f"{crop.shape}:{int(crop.sum())}:{int(crop.mean() * 1000)}".encode()
        ).digest()
        index = digest[0]
        if self.malformed_every and call % self.malformed_every == self.malformed_every - 1:
            return self._MALFORMED[index % len(self._MALFORMED)], 0.42
        text = self._PLATES[index % len(self._PLATES)]
        # Deterministic confidence spread across the accept/reject boundary, so a
        # test run exercises both the "good enough, stop reading" and the "retry
        # this track" branches.
        confidence = self.base_confidence + (digest[1] / 255.0) * 0.44
        return text, round(min(0.99, confidence), 4)


class PaddleOcrReader:
    """PaddleOCR. The real backend.

    Imported inside ``__init__`` for the same reason as the detector: paddle is a
    heavyweight dependency that is not present on the development machines, and a
    module-level import would make this service's unit tests unrunnable there.

    Two result shapes are handled because PaddleOCR changed its return format
    between 2.x and 3.x and both are in the wild. Guessing one and crashing on the
    other during a live test is not a trade worth making for shorter code.
    """

    name = "paddleocr"

    def __init__(self, config: OcrConfig) -> None:
        self.config = config
        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415 - lazy on purpose
        except ImportError as exc:
            raise ReaderUnavailable(
                "paddleocr is not installed, so plate OCR cannot run. Either:\n"
                "    pip install -r services/analytics/requirements.txt\n"
                "or run the cascade with stage 4 switched off:\n"
                "    ANALYTICS_OCR_ENABLED=false python -m worker.main\n"
                "or use the deterministic stub reader:\n"
                "    python -m worker.main --dry-run"
            ) from exc
        try:
            # use_angle_cls: plates on a passing vehicle are rotated by a few
            # degrees far more often than they are level, and the angle classifier
            # costs very little next to the recogniser.
            self._engine = PaddleOCR(
                lang=config.lang,
                use_angle_cls=True,
                show_log=False,
                use_gpu=config.device != "cpu",
            )
        except TypeError:
            # 3.x dropped show_log/use_gpu. Retry with the minimal signature rather
            # than pin a version we cannot guarantee on the target machine.
            self._engine = PaddleOCR(lang=config.lang)
        except Exception as exc:  # noqa: BLE001
            raise ReaderUnavailable(
                f"PaddleOCR failed to initialise: {exc}. On a first run it downloads "
                "model files; if this machine has no internet, pre-seed "
                "~/.paddleocr from a machine that does, or run with "
                "ANALYTICS_OCR_ENABLED=false."
            ) from exc

    def read(self, crop: np.ndarray) -> tuple[str, float] | None:
        try:
            result = self._engine.ocr(crop, cls=True)
        except TypeError:
            result = self._engine.ocr(crop)
        except Exception as exc:  # noqa: BLE001 - one crop must not kill the camera
            log.debug("ocr failed on crop: %s", exc)
            return None
        best_text, best_conf = "", 0.0
        for line in _iter_paddle_lines(result):
            text, conf = line
            # A plate is one line. Where the engine returns several, the most
            # confident one is the plate and the rest are the dealer sticker, the
            # state name strip, or a shop sign behind the vehicle.
            if conf > best_conf:
                best_text, best_conf = text, conf
        if not best_text:
            return None
        return best_text, best_conf


def _iter_paddle_lines(result: Any) -> list[tuple[str, float]]:
    """Flatten either PaddleOCR return shape into ``(text, confidence)`` pairs."""
    out: list[tuple[str, float]] = []
    if not result:
        return out
    # 2.x: [[ [box, (text, conf)], ... ]]. 3.x: [{"rec_texts": [...], "rec_scores": [...]}].
    for page in result:
        if isinstance(page, dict):
            texts = page.get("rec_texts") or []
            scores = page.get("rec_scores") or []
            out.extend((str(t), float(s)) for t, s in zip(texts, scores))
            continue
        if not page:
            continue
        for entry in page:
            try:
                payload = entry[1]
                out.append((str(payload[0]), float(payload[1])))
            except (TypeError, IndexError, ValueError):
                continue
    return out


class OcrStage(Stage):
    """Reads plates from crops, at most once per track until confident.

    Per-track memory lives on the stage and is cleared by ``reset()`` at a loop
    cut. Without that, after the feed loops the same vehicle reappears, the tracker
    has correctly issued it a new id, and a stage-4 cache keyed on the old ids
    would either refuse to read it or attribute the new read to the old track.
    """

    name = "ocr"

    def __init__(self, config: OcrConfig, reader: PlateReader) -> None:
        super().__init__(enabled=config.enabled)
        self.config = config
        self.reader = reader
        self._pattern = re.compile(config.plate_pattern) if config.plate_pattern else None
        self._best_by_track: dict[int, float] = {}
        self._attempts_by_track: dict[int, int] = {}
        self.reader_calls = 0
        self.skipped_already_read = 0
        self.skipped_attempts_exhausted = 0
        self.rejected_low_confidence = 0
        self.no_text = 0
        self.errors = 0

    def reset(self) -> None:
        self._best_by_track.clear()
        self._attempts_by_track.clear()

    def run(self, ctx: StageContext, items: list[Any]) -> list[Any]:
        crops: list[PlateCrop] = [c for c in items if isinstance(c, PlateCrop)]
        arrays: list[np.ndarray] = ctx.extras.get("plate_crop_arrays", [])
        reads: list[PlateRead] = []
        calls = 0
        for index, crop in enumerate(crops):
            if not self._should_read(crop):
                continue
            array = arrays[index] if index < len(arrays) else None
            if array is None:
                continue
            try:
                calls += 1
                self.reader_calls += 1
                result = self.reader.read(array)
            except Exception as exc:  # noqa: BLE001 - one crop, not the camera
                self.errors += 1
                log.debug("camera %s ocr error: %s", ctx.camera_id, exc)
                continue
            if crop.track_id is not None:
                self._attempts_by_track[crop.track_id] = (
                    self._attempts_by_track.get(crop.track_id, 0) + 1
                )
            if result is None:
                self.no_text += 1
                continue
            raw, confidence = result
            text = normalise(raw)
            if not text or confidence < self.config.min_confidence:
                # Not emitted at all. A 0.2-confidence plate string is not
                # evidence, and putting it on the bus invites a downstream
                # consumer to treat it as one.
                self.rejected_low_confidence += 1
                continue
            if crop.track_id is not None:
                previous = self._best_by_track.get(crop.track_id, 0.0)
                if confidence <= previous:
                    # A worse read of a plate we have already read better. Keeping
                    # the best read per track rather than the latest is what makes
                    # the once-per-track rule an accuracy improvement and not just
                    # a cost saving.
                    continue
                self._best_by_track[crop.track_id] = confidence
            reads.append(
                PlateRead(
                    text=text,
                    text_raw=raw,
                    confidence=float(confidence),
                    t=crop.t,
                    box=crop.box,
                    format_valid=bool(self._pattern.match(text)) if self._pattern else False,
                    track_id=crop.track_id,
                    engine=getattr(self.reader, "name", type(self.reader).__name__),
                )
            )
        ctx.extras["plate_reads"] = reads
        # The number that matters for sizing: actual inferences, not crops offered.
        # See Stage.last_work_units.
        self.last_work_units = calls
        return list(reads)

    def _should_read(self, crop: PlateCrop) -> bool:
        track_id = crop.track_id
        if track_id is None:
            # No track means no way to deduplicate, so this crop is read on its own
            # merits. Untracked crops happen on the frames before a track is
            # confirmed, and they are the minority.
            return True
        if not self.config.once_per_track:
            return True
        if self._best_by_track.get(track_id, 0.0) >= self.config.accept_confidence:
            self.skipped_already_read += 1
            return False
        if self._attempts_by_track.get(track_id, 0) >= self.config.max_attempts_per_track:
            # Some plates are unreadable — glare, mud, a bent plate, a vehicle at a
            # bad angle. Retrying forever spends the most expensive stage in the
            # pipeline on the frames least likely to yield anything.
            self.skipped_attempts_exhausted += 1
            return False
        return True

    def stats(self) -> dict[str, Any]:
        return {
            "backend": getattr(self.reader, "name", type(self.reader).__name__),
            "reader_calls": self.reader_calls,
            "tracks_read": len(self._best_by_track),
            "skipped_already_read": self.skipped_already_read,
            "skipped_attempts_exhausted": self.skipped_attempts_exhausted,
            "rejected_low_confidence": self.rejected_low_confidence,
            "no_text": self.no_text,
            "errors": self.errors,
        }


def build_reader(config: OcrConfig, prefer_stub: bool = False) -> PlateReader:
    """Pick a backend. No silent fallback — see ``detect.build_detector``."""
    if prefer_stub or config.engine == "stub":
        return StubReader()
    return PaddleOcrReader(config)
