"""Face embedding: the person side of watchlist matching. No model ships here.

**Read this before wiring in a real backend.** ``services/common/events.py``
bans appearance-based demographic inference — no gender, age, caste, religion or
ethnicity, anywhere — because it is unreliable at CCTV distance and its errors
are not evenly distributed. Matching a crop against a *specific, individually
enrolled* watchlist photo (a wanted-person notice, a missing-person report) is a
different act: it is not a guess about a category someone belongs to, it is a
one-to-few comparison against faces a court or a missing-persons unit put on the
list by name, the same legal basis every police facial-recognition watchlist
already relies on. That distinction is why this module exists and the banned
demographic path does not: the vocabulary and the decision rule are both about
*identity against a consented, auditable list*, never about a track's inferred
category.

**Stage shape mirrors ``stages/plate.py`` deliberately** — a quality gate before
any inference is paid for, one embedding per track (not per frame), and a stub
backend that is honest about being synthetic. ``StubFaceMatcher`` below produces
a deterministic pseudo-embedding from the crop's *bytes*, exactly like
``StubOcrReader`` produces a deterministic plate from the same input: it proves
the plumbing end to end (dedup, thresholding, the rule that consumes a match) on
a machine with no GPU and no model weights, and it will never falsely match two
different real people, because it is not doing face recognition at all — it is a
wiring test. Swapping in ``ANALYTICS_FACE=insightface`` (or any ONNX embedding
model) changes nothing upstream or downstream of this module.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

from services.common.events import BBox

from .detect import crop
from .motion import variance_of_laplacian

log = logging.getLogger(__name__)

#: A fixed-length embedding. Plain tuples of float, not a numpy array, because
#: this crosses the sink boundary into JSON (via ``watchlist.embedding_to_list``)
#: exactly like everything else in ``services.common.events`` does.
Embedding = tuple[float, ...]

#: Stage rejection reasons, for the funnel-style diagnostics the rest of the
#: cascade already reports.
DROP_TOO_SMALL = "crop_too_small"
DROP_BLURRED = "crop_blurred"
DROP_ALREADY_EMBEDDED = "already_embedded"
DROP_EMPTY_CROP = "empty_crop"


@dataclass(frozen=True, slots=True)
class FaceConfig:
    """Thresholds for the face stage. No model touches any of these."""

    enabled: bool = True
    #: ``stub`` or ``insightface``. Never probed — see ``stages.plate``'s config
    #: docstring for why an explicit choice beats a silent fallback.
    backend: str = "stub"
    embed_dim: int = 32
    min_crop_width_px: int = 40
    min_crop_height_px: int = 40
    min_laplacian_variance: float = 20.0
    #: Cosine similarity at or above this is a candidate match. Deliberately
    #: conservative for a stub backend whose "embeddings" carry no real facial
    #: signal — see the module docstring. Tune down only once the backend is a
    #: real embedding model with a measured false-accept rate.
    match_threshold: float = 0.90
    #: One embedding attempt per track, matching the OCR stage's dedupe: a person
    #: crossing frame for forty frames should not pay for forty inferences, and a
    #: person who does not match should not be retried into a false positive.
    once_per_track: bool = True

    @staticmethod
    def from_env() -> FaceConfig:
        from ..config import _env, _env_bool, _env_float, _env_int  # noqa: PLC0415 - avoid a cycle

        return FaceConfig(
            enabled=_env_bool("ANALYTICS_FACE_ENABLED", True),
            backend=_env("ANALYTICS_FACE", "stub").lower(),
            embed_dim=_env_int("ANALYTICS_FACE_EMBED_DIM", 32),
            min_crop_width_px=_env_int("ANALYTICS_FACE_MIN_WIDTH_PX", 40),
            min_crop_height_px=_env_int("ANALYTICS_FACE_MIN_HEIGHT_PX", 40),
            min_laplacian_variance=_env_float("ANALYTICS_FACE_MIN_SHARPNESS", 20.0),
            match_threshold=_env_float("ANALYTICS_FACE_MATCH_THRESHOLD", 0.90),
            once_per_track=_env_bool("ANALYTICS_FACE_ONCE_PER_TRACK", True),
        )


@runtime_checkable
class FaceMatcher(Protocol):
    """What the face stage needs from an embedding backend."""

    name: str

    def embed(self, image: np.ndarray) -> Embedding | None:
        """Return a fixed-length embedding, or ``None`` if no face was usable."""
        ...


class StubFaceMatcher:
    """Deterministic pseudo-embedding from crop bytes. See the module docstring.

    Two different crops (almost certainly) get different embeddings; the *same*
    crop always gets the same one. That is enough to exercise dedup, threshold
    comparison and the rule engine's match path without claiming to recognise
    anyone — which is also why ``FaceConfig.match_threshold`` defaults high:
    there is no real facial signal here to threshold on, only a demonstration
    that the plumbing is wired correctly end to end.
    """

    name = "stub"

    __slots__ = ("calls", "_dim")

    def __init__(self, embed_dim: int = 32) -> None:
        self.calls = 0
        self._dim = embed_dim

    def embed(self, image: np.ndarray) -> Embedding | None:
        self.calls += 1
        if image is None or image.size == 0:
            return None
        digest = hashlib.blake2b(
            np.ascontiguousarray(image).tobytes(), digest_size=max(8, self._dim // 4)
        ).digest()
        # Expand the digest into embed_dim floats in [-1, 1] by walking bytes
        # cyclically, then L2-normalise so cosine similarity is well-behaved.
        raw = [((digest[i % len(digest)] / 127.5) - 1.0) for i in range(self._dim)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return tuple(v / norm for v in raw)


class InsightFaceMatcher:
    """Real ONNX-based face embedding, imported lazily.

    Not installed by default — see ``services/analytics/requirements.txt``. This
    class exists so ``ANALYTICS_FACE=insightface`` is a configuration change, not
    a rewrite: everything from here down the pipeline (dedup, thresholding, the
    ``watchlist_match_person`` rule, the alert it produces) already consumes a
    plain ``Embedding`` tuple and does not know or care which backend produced it.
    """

    name = "insightface"

    __slots__ = ("_app",)

    def __init__(self) -> None:
        try:
            import insightface  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "ANALYTICS_FACE=insightface but the 'insightface' package is not "
                "installed. Either install the optional ML tier:\n"
                "    pip install insightface onnxruntime\n"
                "or run without it:\n"
                "    ANALYTICS_FACE=stub\n"
                "The stub proves the watchlist-matching pipeline is wired "
                "correctly; it does not recognise faces."
            ) from exc
        self._app = insightface.app.FaceAnalysis(name="buffalo_l")
        self._app.prepare(ctx_id=-1)  # CPU; edge boxes are not assumed to have a GPU.

    def embed(self, image: np.ndarray) -> Embedding | None:
        faces = self._app.get(image)
        if not faces:
            return None
        # Largest face in the crop, on the assumption the crop is already one
        # person's bounding box and a second face is a bystander behind them.
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return tuple(float(v) for v in best.normed_embedding)


def build_face_matcher(config: FaceConfig) -> FaceMatcher:
    backend = (config.backend or "stub").lower()
    if backend == "stub":
        log.warning(
            "face backend is 'stub': embeddings carry no real facial signal, "
            "for pipeline verification only. Set ANALYTICS_FACE=insightface for "
            "real person-watchlist matching."
        )
        return StubFaceMatcher(config.embed_dim)
    if backend == "insightface":
        return InsightFaceMatcher()
    raise ValueError(f"unknown face backend {config.backend!r}; expected 'stub' or 'insightface'")


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """1.0 is identical direction, 0.0 is orthogonal, -1.0 is opposite.

    Embeddings from ``StubFaceMatcher``/``InsightFaceMatcher`` are already
    L2-normalised, but this does not assume that of its inputs — a watchlist
    entry's stored embedding may have come from a different enrolment path.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


class FaceStage:
    """Per-camera: quality gate, then embed, once per track.

    Mirrors ``stages.plate.PlateReadStage`` closely enough that anyone who has
    read one can read the other. Separate class (rather than folding into
    ``PlateReadStage``) because the two run over disjoint track populations
    (plated vehicle classes vs. ``person``) and gate on different geometry
    (aspect-ratio plate crop vs. a roughly square face crop), and conflating them
    would make either docstring lie about what the class does.
    """

    __slots__ = ("_config", "_matcher", "_done", "drops", "embeds_emitted")

    def __init__(self, config: FaceConfig, matcher: FaceMatcher) -> None:
        self._config = config
        self._matcher = matcher
        self._done: set[str] = set()
        self.drops: dict[str, int] = {}
        self.embeds_emitted = 0

    def reset(self) -> None:
        """Clear the dedupe cache. Called on a scene change, same reasoning as
        ``PlateReadStage.reset``: track ids are not reused across a scene cut."""
        self._done.clear()

    def embed_person(
        self, frame: np.ndarray, track_id: str, bbox: BBox
    ) -> Embedding | None:
        cfg = self._config
        if not cfg.enabled:
            return None
        if cfg.once_per_track and track_id in self._done:
            self._drop(DROP_ALREADY_EMBEDDED)
            return None

        patch = crop(frame, bbox)
        if patch.size == 0:
            self._drop(DROP_EMPTY_CROP)
            return None
        h, w = patch.shape[:2]
        if w < cfg.min_crop_width_px or h < cfg.min_crop_height_px:
            self._drop(DROP_TOO_SMALL)
            return None
        if variance_of_laplacian(patch) < cfg.min_laplacian_variance:
            # A motion-blurred crop does not become a usable embedding by being
            # handed to a better model — see the identical argument in
            # stages.plate for why this gate exists before the expensive call.
            self._drop(DROP_BLURRED)
            return None

        if cfg.once_per_track:
            self._done.add(track_id)
        embedding = self._matcher.embed(patch)
        if embedding is not None:
            self.embeds_emitted += 1
        return embedding

    def _drop(self, reason: str) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1
