"""Stage 0: the motion gate. Every frame pays for this and almost nothing else.

This is the cheapest stage and the one the entire cost argument rests on. If it
passes 8% of frames, the three stages behind it cost 8% of what they otherwise
would. If it fails open, the cascade silently becomes a straight pipeline and the
sizing figures in the submission become fiction.

**The failure this module exists to prevent.** Naive frame differencing computes
``|current - reference|`` and passes the frame when enough pixels changed. A cloud
crossing the sun, a vehicle's headlights sweeping a wall, a camera's auto-exposure
stepping its gain, or the IR-cut filter switching at dusk each change *every pixel
at once*. The naive gate reports 100% of the frame in motion and opens. It then
keeps opening, because the next frame's illumination is different again. The gate
that was passing 8% of frames now passes 100%, and it does so from dusk onwards —
which is precisely the period when the crimes-against-women use case needs the
pipeline most, and precisely when nobody is watching the throughput graph.

Two independent defences, because the first one is not sufficient on its own:

1. **Affine illumination compensation.** Fit ``current ≈ gain * reference + bias``
   over the whole frame by least squares, then difference against the fitted
   reference. ``bias`` absorbs an additive change (headlights, ambient light);
   ``gain`` absorbs a multiplicative one (exposure, IR filter). A genuine moving
   object is a small minority of pixels, so it barely influences the fit and
   survives it almost intact — which is the asymmetry the whole trick relies on.
   One reweighting pass then drops the outlier pixels from the fit, so a large
   object cannot drag the coefficients towards itself.

2. **A per-region concentration test.** Real motion is *local*: an object occupies
   a contiguous patch. A global illumination change is diffuse by definition. The
   frame is divided into a grid and a pass requires the change to be concentrated
   in a few cells. So even if a non-linear tone curve defeats the affine fit — and
   a real camera's response is not linear — the residual it leaves is spread evenly
   across every cell, and the concentration test rejects it.

**What this does not fix.** A camera that is physically moved, a wiper crossing the
lens, or heavy rain lit by a streetlight all produce genuine, concentrated,
non-illumination change. They will open the gate, and they should: the frame really
did change and the detector is the right thing to adjudicate it. The gate's job is
to be cheap and to not lie about global lighting, not to be a classifier.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

try:  # cv2 is a tier-1 dependency, but the gate is the one stage that must never
    # be unavailable, and a numpy fallback costs twenty lines. A box where the
    # opencv wheel failed to build can still run and still be tested.
    import cv2 as _cv2
except ImportError:  # pragma: no cover - exercised only where cv2 is absent
    _cv2 = None

from ..config import MotionConfig

#: Drop reasons. Strings rather than an enum because they are counter keys that end
#: up in a JSON stats blob, and a name that survives serialisation unchanged is
#: easier to read in a report than ``MotionReason.ILLUMINATION``.
REASON_PASS = "pass"
REASON_WARMUP = "warmup"
REASON_HEARTBEAT = "heartbeat"
REASON_STATIC = "no_motion"
REASON_ILLUMINATION = "illumination"
REASON_GLOBAL = "global_change"
REASON_RESHAPE = "resolution_change"
REASON_BAD_FRAME = "bad_frame"

#: Gain outside this range is not a lighting change, it is a fit that has been
#: captured by something pathological (a near-black frame, a frozen decoder output).
#: Clamping rather than rejecting keeps the gate conservative: a clamped fit
#: compensates less, so it errs towards passing the frame.
_GAIN_MIN = 0.4
_GAIN_MAX = 2.5

#: Scale factor turning a median absolute deviation into a Gaussian sigma estimate.
_MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True, slots=True)
class MotionDecision:
    """The gate's verdict plus the evidence for it.

    Every field here exists to be counted or logged. ``raw_changed_fraction``
    alongside ``changed_fraction`` is the direct measurement of what the
    illumination compensation is buying: when the raw fraction is large and the
    compensated fraction is near zero, the guard has just prevented the dusk
    failure, and the difference between them is the number to put in the report.
    """

    passed: bool
    reason: str
    changed_fraction: float = 0.0
    raw_changed_fraction: float = 0.0
    active_cells: int = 0
    total_cells: int = 0
    gain: float = 1.0
    bias: float = 0.0
    threshold: float = 0.0

    @property
    def suppressed_by_illumination(self) -> bool:
        """True when the raw difference would have opened the gate and the
        compensated difference did not. This is the counter that proves the guard
        works, and the assertion the adversarial test makes."""
        return not self.passed and self.reason in (REASON_ILLUMINATION, REASON_GLOBAL)

    @property
    def active_cell_fraction(self) -> float:
        if self.total_cells <= 0:
            return 0.0
        return self.active_cells / self.total_cells


class MotionGate:
    """Per-stream frame differencing gate. Not thread-safe; one per camera.

    Reference frame is the previous *examined* frame rather than a long-term
    background model. Two reasons: a background model needs a learning rate that
    has to be tuned per scene and gets it wrong on a junction that is never empty,
    and a background model burns a vehicle stopped at a light into the background
    over a minute or two — after which it stops being detected at all. Consecutive
    differencing plus the ``heartbeat_seconds`` escape hatch gives the same coverage
    without the tuning: change is detected immediately, and presence is re-checked
    on a timer.
    """

    __slots__ = (
        "_config",
        "_reference",
        "_frames_seen",
        "_last_pass_t",
        "illumination_suppressed",
        "global_changes",
    )

    def __init__(self, config: MotionConfig | None = None) -> None:
        self._config = config or MotionConfig()
        self._reference: np.ndarray | None = None
        self._frames_seen = 0
        self._last_pass_t = 0.0
        #: Frames where the raw difference would have opened the gate and the
        #: compensated difference did not. ``global_changes`` is the subset of those
        #: that were diffuse across the whole grid, i.e. the dusk case proper.
        self.illumination_suppressed = 0
        self.global_changes = 0

    def reset(self) -> None:
        """Forget the reference. Called on reconnect and on a scene change.

        Differencing across a scene cut compares two unrelated images and produces
        a full-frame residual with no coherent structure, which the concentration
        test would reject — so the gate would go *blind* for the first frames of
        every new scene rather than opening on it. Resetting makes the first frame
        after a cut a warmup frame, which passes unconditionally.
        """
        self._reference = None
        self._frames_seen = 0

    def evaluate(self, frame: np.ndarray, t: float) -> MotionDecision:
        """Decide whether ``frame`` is worth handing to the detector.

        ``t`` is stream time in seconds (PTS-derived), used only for the heartbeat.
        Wall time would make the heartbeat fire at the wrong stream instant during
        the join-time replay burst, when a second of stream arrives in 200 ms.
        """
        if not self._config.enabled:
            return MotionDecision(True, REASON_PASS)

        work = self._prepare(frame)
        if work is None:
            # A malformed frame is not evidence of no motion. Fail open: one wasted
            # detector call is cheaper than a missed incident, and a decoder that
            # emits garbage is already going to be noticed.
            return MotionDecision(True, REASON_BAD_FRAME)

        self._frames_seen += 1
        reference = self._reference

        if reference is None or reference.shape != work.shape:
            # A shape change mid-stream is the camera renegotiating its profile,
            # which the grid does. Comparing across it is meaningless.
            reason = REASON_WARMUP if reference is None else REASON_RESHAPE
            self._reference = work
            self._last_pass_t = t
            return MotionDecision(True, reason)

        if self._frames_seen <= self._config.warmup_frames:
            self._reference = work
            self._last_pass_t = t
            return MotionDecision(True, REASON_WARMUP)

        decision = self._compare(reference, work)
        self._reference = work

        if decision.passed:
            self._last_pass_t = t
            return decision

        if decision.suppressed_by_illumination:
            self.illumination_suppressed += 1
        if decision.reason == REASON_GLOBAL:
            self.global_changes += 1
            # Re-baseline. A global change means the scene's exposure has genuinely
            # moved, and holding the pre-change reference would keep producing a
            # large residual on every subsequent frame for as long as the fit
            # cannot fully explain it.
            self._reference = work

        heartbeat = self._config.heartbeat_seconds
        if heartbeat > 0 and (t - self._last_pass_t) >= heartbeat:
            # Differencing sees change, not presence. Without this, a vehicle
            # stopped at a light or a bag left on a platform stops generating
            # sightings entirely — and those are exactly what the ``abandoned`` and
            # stopped-vehicle rules are built on.
            self._last_pass_t = t
            return MotionDecision(
                True,
                REASON_HEARTBEAT,
                changed_fraction=decision.changed_fraction,
                raw_changed_fraction=decision.raw_changed_fraction,
                active_cells=decision.active_cells,
                total_cells=decision.total_cells,
                gain=decision.gain,
                bias=decision.bias,
                threshold=decision.threshold,
            )

        return decision

    # -- internals ---------------------------------------------------------

    def _prepare(self, frame: np.ndarray) -> np.ndarray | None:
        """Greyscale, downscale, blur. In that order, cheapest operation first.

        Downscaling before blurring is not an optimisation detail: blurring at full
        resolution costs more than the entire rest of the gate, and the downscale is
        itself a low-pass filter, so most of the blur's job is already done.
        """
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        if frame.ndim == 3:
            grey = _to_grey(frame)
        elif frame.ndim == 2:
            grey = frame
        else:
            return None
        if grey is None or grey.size == 0:
            return None

        width = max(16, int(self._config.work_width))
        h, w = grey.shape[:2]
        if w > width:
            scale = width / float(w)
            grey = _resize(grey, width, max(9, int(round(h * scale))))

        k = int(self._config.blur_kernel)
        if k >= 3:
            grey = _blur(grey, k | 1)  # Kernel must be odd; |1 is cheaper than a branch.
        return grey.astype(np.float32, copy=False)

    def _compare(self, reference: np.ndarray, current: np.ndarray) -> MotionDecision:
        cfg = self._config
        raw = np.abs(current - reference)

        if cfg.compensate_illumination:
            gain, bias = _fit_affine(reference, current)
            compensated = np.abs(current - (gain * reference + bias))
        else:
            gain, bias = 1.0, 0.0
            compensated = raw

        # Threshold above the frame's own noise, not a fixed constant. A robust
        # sigma (median absolute deviation) is used because the mean and standard
        # deviation of the residual are inflated by the very object we are trying to
        # find, which would raise the threshold above it.
        sigma = float(np.median(compensated)) * _MAD_TO_SIGMA
        threshold = max(float(cfg.diff_threshold), cfg.noise_sigmas * sigma)

        mask = compensated > threshold
        changed_fraction = float(mask.mean())
        raw_changed_fraction = float((raw > threshold).mean())

        active, total = _grid_activity(mask, cfg.grid_rows, cfg.grid_cols, cfg.cell_min_fraction)
        active_fraction = active / total if total else 0.0

        common = {
            "changed_fraction": changed_fraction,
            "raw_changed_fraction": raw_changed_fraction,
            "active_cells": active,
            "total_cells": total,
            "gain": gain,
            "bias": bias,
            "threshold": threshold,
        }

        if active_fraction > cfg.max_active_cell_fraction:
            # Change everywhere at once. Either the affine fit could not explain a
            # non-linear exposure shift, or the camera moved. Neither is an object,
            # and passing it hands the detector a frame in which everything is
            # "new" — the exact input that makes a detector emit a screenful of
            # low-confidence boxes and the tracker invent a dozen phantom tracks.
            return MotionDecision(False, REASON_GLOBAL, **common)

        concentrated = active >= max(1, cfg.min_active_cells)
        large_enough = changed_fraction >= cfg.min_area_fraction
        if concentrated and large_enough:
            return MotionDecision(True, REASON_PASS, **common)

        # Blocked. Which reason it is matters, because the two are diagnostically
        # different: ``no_motion`` means the scene is quiet, ``illumination`` means
        # the scene is *not* quiet and the compensation is the only thing keeping
        # the gate shut. A rising ``illumination`` count at dusk is the guard
        # working; a rising one at noon is a camera with a failing iris.
        raw_would_have_passed = (
            raw_changed_fraction >= cfg.min_area_fraction
            and raw_changed_fraction > changed_fraction * 4.0
        )
        reason = REASON_ILLUMINATION if raw_would_have_passed else REASON_STATIC
        return MotionDecision(False, reason, **common)


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _fit_affine(reference: np.ndarray, current: np.ndarray) -> tuple[float, float]:
    """Least-squares fit of ``current ≈ gain * reference + bias``, then reweight.

    The closed form for a single-variable linear fit is used rather than
    ``np.linalg.lstsq`` because it needs no allocation of a design matrix the size
    of the frame, and this runs on every frame of every camera.

    The second pass is what makes it robust. A first fit that includes a large
    moving object is pulled towards explaining that object as a lighting change,
    which is exactly the wrong outcome — it would compensate the object away. So
    pixels whose first-pass residual is a clear outlier are excluded and the fit is
    repeated on the remainder, which is the static background. This is one step of
    iteratively reweighted least squares; further iterations measurably change
    nothing on real frames and cost another pass over the image.
    """
    ref = reference.ravel()
    cur = current.ravel()
    gain, bias = _affine_once(ref, cur)

    residual = np.abs(cur - (gain * ref + bias))
    sigma = float(np.median(residual)) * _MAD_TO_SIGMA
    if sigma <= 1e-6:
        # The fit already explains the frame to within numerical noise; there is no
        # outlier population to exclude.
        return gain, bias

    keep = residual <= 3.0 * sigma
    if int(keep.sum()) < max(64, ref.size // 20):
        # Too few inliers to refit against. Trust the first pass rather than fitting
        # to a handful of pixels, which is how you get a wild gain estimate.
        return gain, bias
    return _affine_once(ref[keep], cur[keep])


def _affine_once(ref: np.ndarray, cur: np.ndarray) -> tuple[float, float]:
    n = ref.size
    if n == 0:
        return 1.0, 0.0
    ref_mean = float(ref.mean())
    cur_mean = float(cur.mean())
    ref_centred = ref - ref_mean
    var = float(np.dot(ref_centred, ref_centred))
    if var <= 1e-6 * n:
        # A featureless reference — a wall, a night frame, a frozen decoder buffer.
        # Gain is unidentifiable from a constant signal, so fit the bias only. This
        # case is common enough to matter: it is every synthetic test frame and
        # every camera pointed at a shutter.
        return 1.0, cur_mean - ref_mean
    cov = float(np.dot(ref_centred, cur - cur_mean))
    gain = cov / var
    gain = min(_GAIN_MAX, max(_GAIN_MIN, gain))
    bias = cur_mean - gain * ref_mean
    return gain, bias


def _grid_activity(
    mask: np.ndarray, rows: int, cols: int, cell_min_fraction: float
) -> tuple[int, int]:
    """Count grid cells in which the changed fraction clears ``cell_min_fraction``.

    Implemented by integer-slicing rather than reshaping, because the work
    resolution is not guaranteed to divide evenly by the grid dimensions and a
    reshape would raise on, say, a 320x181 frame. Uneven cells at the right and
    bottom edges are acceptable: the test is a fraction, so a cell being 8% smaller
    changes the answer only for a change sitting exactly on the threshold.
    """
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    h, w = mask.shape[:2]
    rows = min(rows, h)
    cols = min(cols, w)

    row_edges = [(i * h) // rows for i in range(rows + 1)]
    col_edges = [(j * w) // cols for j in range(cols + 1)]

    active = 0
    for i in range(rows):
        r0, r1 = row_edges[i], row_edges[i + 1]
        if r1 <= r0:
            continue
        for j in range(cols):
            c0, c1 = col_edges[j], col_edges[j + 1]
            if c1 <= c0:
                continue
            cell = mask[r0:r1, c0:c1]
            if cell.size and float(cell.mean()) >= cell_min_fraction:
                active += 1
    return active, rows * cols


def _to_grey(frame: np.ndarray) -> np.ndarray | None:
    channels = frame.shape[2] if frame.ndim == 3 else 1
    if _cv2 is not None:
        if channels == 3:
            return _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)
        if channels == 4:
            return _cv2.cvtColor(frame, _cv2.COLOR_BGRA2GRAY)
    if channels >= 3:
        # ITU-R BT.601 luma weights, BGR order to match OpenCV's channel layout.
        b, g, r = frame[..., 0], frame[..., 1], frame[..., 2]
        return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.float32)
    if channels == 1:
        return frame[..., 0]
    return None


def _resize(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if _cv2 is not None:
        # INTER_AREA, not INTER_LINEAR. Downscaling with a linear filter aliases,
        # and aliased high-frequency detail moves between frames even in a static
        # scene, which shows up as change and opens the gate on nothing.
        return _cv2.resize(image, (width, height), interpolation=_cv2.INTER_AREA)
    src_h, src_w = image.shape[:2]
    ys = (np.arange(height) * (src_h / height)).astype(np.int32)
    xs = (np.arange(width) * (src_w / width)).astype(np.int32)
    return image[np.ix_(ys, xs)]


def _blur(image: np.ndarray, kernel: int) -> np.ndarray:
    if _cv2 is not None:
        return _cv2.GaussianBlur(image, (kernel, kernel), 0)
    # Separable box blur, twice, which is a serviceable approximation of a Gaussian
    # and is the only thing here that needs no convolution routine.
    out = image.astype(np.float32)
    pad = kernel // 2
    for axis in (0, 1):
        padded = np.pad(out, [(pad, pad) if a == axis else (0, 0) for a in (0, 1)], mode="edge")
        acc = np.zeros_like(out, dtype=np.float32)
        for offset in range(kernel):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(offset, offset + out.shape[axis])
            acc += padded[tuple(sl)]
        out = acc / kernel
    return out


def variance_of_laplacian(image: np.ndarray) -> float:
    """Focus measure: variance of the 4-neighbour Laplacian.

    Lives here rather than in ``plate`` because it is the same class of cheap
    whole-image statistic as the gate itself, and stage 3's quality gate needs it.
    A blurred image has little high-frequency content, so its Laplacian is close to
    zero everywhere and its variance is small. It is scale-sensitive — a 40x14 crop
    and a 200x70 crop of the same plate do not score the same — so the threshold in
    ``OcrConfig`` is calibrated against crops at the sizes stage 2 actually emits,
    and changing the crop geometry means recalibrating it.
    """
    if image is None or image.size == 0:
        return 0.0
    grey = image if image.ndim == 2 else _to_grey(image)
    if grey is None or grey.size < 9:
        return 0.0
    if _cv2 is not None:
        return float(_cv2.Laplacian(grey.astype(np.float32), _cv2.CV_32F).var())
    f = grey.astype(np.float32)
    lap = (
        -4.0 * f[1:-1, 1:-1]
        + f[:-2, 1:-1]
        + f[2:, 1:-1]
        + f[1:-1, :-2]
        + f[1:-1, 2:]
    )
    return float(lap.var()) if lap.size else 0.0


def is_uniform_illumination_shift(
    reference: np.ndarray, current: np.ndarray, tolerance: float = 2.0
) -> bool:
    """True when the two frames differ only by a global gain and bias.

    Exposed for tests and for the diagnostic CLI. If this returns true for a frame
    the gate passed, the gate has a bug — and that is a much easier assertion to
    read in a test than a comparison of changed-pixel fractions.
    """
    ref = reference.astype(np.float32).ravel()
    cur = current.astype(np.float32).ravel()
    if ref.shape != cur.shape:
        return False
    gain, bias = _fit_affine(reference.astype(np.float32), current.astype(np.float32))
    residual = np.abs(cur - (gain * ref + bias))
    return bool(np.percentile(residual, 99.0) <= tolerance) and not math.isnan(gain)
