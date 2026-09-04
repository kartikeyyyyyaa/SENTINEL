"""Stage 1: the motion gate.

The cheapest stage, run on every decoded frame, and the reason the rest of the
cascade is affordable. On a typical road camera it forwards somewhere around
20-40% of frames; on a camera pointed at a quiet lane at 3 a.m. it forwards almost
none. Everything downstream — the detector, the plate crop, the OCR — sees only
what this stage passes, so a percentage point here is worth far more than a
percentage point of optimisation anywhere else.

**Why plain frame differencing and not a background model.** MOG2 or KNN would
distinguish "object present" from "object moving", which sounds strictly better.
It is not, for two reasons. First, the gate has to cost less than the work it
saves, and a per-pixel Gaussian mixture at full resolution does not: it is a
sizeable fraction of a YOLOv8n int8 inference on the same machine, and the gate
runs on every frame while the detector does not. Second, these feeds loop with a
hard scene cut, so a background model spends the seconds after every cut
relearning a scene it already knew, during which it reports the entire frame as
foreground — which is the opposite of a gate.

**Why the downscale comes first.** Differencing 1080p costs more than the gate
saves. At 320 px wide the memory traffic is roughly 20x lower and the signal we
care about, an object the size of a vehicle or a person, survives intact. What
does not survive is a pixel of sensor noise, which is the point.

**Why there is a maximum gap.** Differencing detects change, not presence. A
vehicle stopped at a red light stops generating change, and a gate with no
timeout would go silent on it — so the platform would lose exactly the stationary
vehicle that a "stopped on the shoulder" rule needs. Forcing one frame through
every ``max_gap_seconds`` keeps the downstream stages seeing the scene at a slow
heartbeat when nothing is moving.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..config import MotionConfig
from . import Stage, StageContext

try:  # cv2 is faster and correctly anti-aliases the downscale, but is optional.
    import cv2 as _cv2
except ImportError:  # pragma: no cover - environment-dependent
    _cv2 = None


class MotionGate(Stage):
    """Passes a frame on if enough of it changed since the last frame it saw.

    "The last frame it saw" and not "the previous decoded frame": when the
    adaptive sampler widens the stride the gate is only shown every Nth frame, and
    comparing against the immediately preceding decoded frame would require
    decoding and downscaling the frames we just decided to skip. Differencing
    against the previous *sampled* frame means a wider stride makes the gate more
    sensitive rather than more expensive, which is the correct direction: a worker
    under load should err towards passing frames it is unsure about.
    """

    name = "motion"
    # Frame in, same frame out. The only stage that can be switched off without
    # truncating the cascade — disabled, it forwards everything.
    passthrough_when_disabled = True

    def __init__(self, config: MotionConfig) -> None:
        super().__init__(enabled=config.enabled)
        self.config = config
        self._reference: np.ndarray | None = None
        self._frames_seen = 0
        self._last_pass_t: float | None = None
        self.forced_passes = 0  # Passes that came from the heartbeat, not motion.
        self.last_changed_fraction = 0.0

    def reset(self) -> None:
        """Drop the reference frame at a loop cut.

        Without this, the first frame of the new scene is differenced against the
        last frame of the old one. Every pixel differs, the gate reports total
        motion, and the detector is handed a frame for no reason at the exact
        moment the tracker is also being reset — the worst possible time to add
        spurious detections.
        """
        self._reference = None
        self._frames_seen = 0
        self._last_pass_t = None

    def run(self, ctx: StageContext, items: list[Any]) -> list[Any]:
        out: list[Any] = []
        for frame in items:
            if self._passes(ctx, frame.image):
                out.append(frame)
        return out

    def _passes(self, ctx: StageContext, image: np.ndarray) -> bool:
        try:
            current = self._prepare(image)
        except Exception:  # noqa: BLE001 - a malformed frame costs one frame, not the camera
            return True  # Fail open: an unreadable frame is not evidence of stillness.

        reference = self._reference
        self._reference = current
        self._frames_seen += 1

        # Warmup. The frames immediately after a connect are a replayed GOP whose
        # reference pictures we never received, so their content is unreliable.
        # Passing them costs a handful of detector calls once per connection and
        # avoids calibrating the gate against garbage.
        if reference is None or self._frames_seen <= self.config.warmup_frames:
            self._last_pass_t = ctx.t
            return True

        if reference.shape != current.shape:
            # Resolution changed mid-stream. It happens: the gateway re-negotiates
            # and hands over a different frame size without dropping the
            # connection. Nothing to compare against, so pass and re-reference.
            self._last_pass_t = ctx.t
            return True

        diff = np.abs(current.astype(np.int16) - reference.astype(np.int16))
        changed = int(np.count_nonzero(diff > self.config.diff_threshold))
        fraction = changed / float(current.size) if current.size else 0.0
        self.last_changed_fraction = fraction
        ctx.extras["motion_fraction"] = fraction

        if fraction >= self.config.min_area_fraction:
            self._last_pass_t = ctx.t
            return True

        # Heartbeat. See the module docstring: a stationary object stops producing
        # change and must not therefore stop producing detections.
        last = self._last_pass_t
        if last is None or (ctx.t - last) >= self.config.max_gap_seconds:
            self._last_pass_t = ctx.t
            self.forced_passes += 1
            ctx.extras["motion_forced"] = True
            return True
        return False

    def _prepare(self, image: np.ndarray) -> np.ndarray:
        """Greyscale, downscaled, low-passed. The only per-frame cost of stage 1."""
        target_w = max(32, self.config.work_width)
        h, w = image.shape[:2]
        if _cv2 is not None:
            gray = (
                _cv2.cvtColor(image, _cv2.COLOR_BGR2GRAY)
                if image.ndim == 3
                else image
            )
            if w > target_w:
                scale = target_w / float(w)
                # INTER_AREA, not INTER_LINEAR: area averaging is the anti-aliasing
                # decimation filter. Linear downscaling by 6x aliases high-frequency
                # detail into exactly the low-frequency band the gate measures, so
                # foliage and rain would read as motion.
                gray = _cv2.resize(
                    gray, (target_w, max(1, int(round(h * scale)))), interpolation=_cv2.INTER_AREA
                )
            k = self.config.blur_kernel | 1  # GaussianBlur requires an odd kernel.
            if k >= 3:
                gray = _cv2.GaussianBlur(gray, (k, k), 0)
            return gray
        return _block_mean_gray(image, target_w)


def _block_mean_gray(image: np.ndarray, target_w: int) -> np.ndarray:
    """numpy-only greyscale downscale by block averaging.

    The fallback for a machine without cv2, and the reason the unit tests can
    exercise the gate at all in a bare environment. Block averaging is both the
    decimation and the low-pass filter, so it does the job of resize+blur in one
    pass — slower than cv2 but correct, which is the right trade for a fallback.
    """
    gray = image.mean(axis=2) if image.ndim == 3 else image.astype(np.float32)
    h, w = gray.shape[:2]
    if w <= target_w:
        return gray.astype(np.uint8)
    factor = max(1, w // target_w)
    # Trim to a whole number of blocks; the discarded strip is at most one block
    # wide and carries no information the gate would act on.
    hh, ww = (h // factor) * factor, (w // factor) * factor
    if hh == 0 or ww == 0:
        return gray.astype(np.uint8)
    trimmed = gray[:hh, :ww]
    pooled = trimmed.reshape(hh // factor, factor, ww // factor, factor).mean(axis=(1, 3))
    return pooled.astype(np.uint8)
