"""RTSP capture, written against the documented failure modes of the Sentinel grid.

Every default and every workaround in this file answers a specific, observed failure
of the hackathon camera gateway. Each has a comment naming the failure, because in
six months somebody will read one of these lines, conclude it is superstition, remove
it, and spend a day rediscovering why it was there.

**This module consumes and never publishes.** No ``cv2.VideoWriter``, no RTSP
ANNOUNCE, no POST to the gateway, no ONVIF control call — not to a camera, not to a
VMS, not to the sandbox. The grid is somebody else's infrastructure and Sentinel's
posture towards it is strictly read-only. ``tests/test_source.py`` asserts this by
scanning the package source for the API calls that would break it, because a comment
is not an enforcement mechanism.

The failures, and what answers each:

1. *UDP loses packets silently.* The stream keeps flowing and delivers frames with
   smeared macroblocks and torn reference pictures. Every symptom points at the
   detector — "our model is missing obvious vehicles" — and none points at the
   transport. Answered by forcing ``rtsp_transport;tcp``.
2. *``CAP_PROP_FPS`` is wrong.* The gateway reports whatever is in the container
   header, which for these streams is frequently 0, 25 on a 12 fps feed, or 90000.
   Answered by never reading it; intervals are measured.
3. *The gateway replays a buffered GOP on connect.* The first one to two seconds
   arrive faster than real time. Answered by deriving all timing from PTS via
   ``StreamClock``, and by a join grace window in which failed reads are expected.
4. *Frame intervals are not uniform.* Answered by ageing everything in seconds.
5. *Join-time decoder warnings.* ``Error constructing the frame RPS``, ``Could not
   find ref with POC`` and friends are printed on stderr by ffmpeg for the first
   frames of a connection, because we joined mid-GOP and genuinely do not have the
   reference pictures. They are not fatal and must not trigger a reconnect — a
   worker that reconnects on them never gets past the first second of any stream.
6. *Each feed loops with a hard scene cut.* PTS jumps; ``StreamClock.advance``
   reports it; the caller resets tracker, motion reference and OCR cache.
7. *Mixed H.264/H.265 and mixed resolutions.* No fixed-shape batching anywhere, and
   a resolution change mid-stream is handled rather than crashed on.
8. *Reconnect storms.* Fifty workers retrying in lockstep against infrastructure we
   do not own is indistinguishable from an attack. Answered by jittered exponential
   backoff, capped.
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
from dataclasses import dataclass
from typing import Iterator, Protocol

import numpy as np

from .clock import AdaptiveSampler, FramePacer, FrameTiming, StallDetector
from .config import CameraConfig, SamplingConfig, SourceConfig

log = logging.getLogger(__name__)

#: ffmpeg emits these while decoding the first frames after joining a stream
#: mid-GOP. They mean "I do not have the reference pictures for this frame", which is
#: true and expected — we joined in the middle. Treating them as errors makes the
#: worker reconnect forever and never see a second of video.
NONFATAL_DECODER_MESSAGES = (
    "Error constructing the frame RPS",
    "Could not find ref with POC",
    "missing picture in access unit",
    "no frame!",
    "non-existing PPS",
    "decode_slice_header error",
    "Reference picture missing during reorder",
    "illegal short term buffer state detected",
    "corrupted macroblock",
    "SPS unavailable in decode_picture_timing",
)

#: URL schemes this worker will open. Anything else is refused rather than passed to
#: ffmpeg, because ffmpeg will happily open a scheme that writes.
_READ_ONLY_SCHEMES = ("rtsp://", "rtsps://", "file://", "http://", "https://", "stub://")


@dataclass(slots=True)
class Frame:
    """One decoded frame plus everything the cascade needs to place it in time.

    ``gap_seconds`` is set on the first frame after a reconnect and is ``None``
    otherwise. It exists because "the camera saw nothing" and "the worker was not
    watching" are different facts: a rule concluding that a vehicle never exited has
    to know whether we were blind for that interval, and a platform that cannot tell
    the difference reports "all clear" on a dead camera.
    """

    image: np.ndarray
    pts: float
    timing: FrameTiming
    connection: int
    gap_seconds: float | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.image.shape[0]), int(self.image.shape[1]))


class FrameSource(Protocol):
    """A source of frames. Read-only by construction: there is no write method."""

    camera_id: int

    def frames(self) -> Iterator[Frame]: ...

    def close(self) -> None: ...


class BackoffSchedule:
    """Jittered exponential backoff, capped. Bounded and monotonic to the cap.

    Jitter is not optional here. Fifty workers on one box, all pointed at one gateway,
    all retrying on the same doubling schedule after a shared network blip, produce
    synchronised bursts that look exactly like a deliberate flood — and the gateway is
    not ours to overload. Jitter spreads them.

    The exponent is clamped in log space rather than by computing the power and then
    limiting it. ``2.0 ** 2000`` raises ``OverflowError``, and a worker that has been
    failing to reach a decommissioned camera for a week will reach attempt 2000. That
    is not a hypothetical: it is what happens to any long-running fleet.
    """

    __slots__ = ("initial", "factor", "cap", "jitter", "_attempt", "_rng")

    def __init__(self, config: SourceConfig, rng: random.Random | None = None) -> None:
        self.initial = max(0.01, config.backoff_initial_seconds)
        self.factor = max(1.0, config.backoff_factor)
        self.cap = max(self.initial, config.backoff_cap_seconds)
        self.jitter = min(max(config.backoff_jitter, 0.0), 0.99)
        self._attempt = 0
        # Injectable RNG so the backoff test asserts a schedule rather than observing
        # one. A test that has to allow for random jitter can only assert loose bounds.
        self._rng = rng or random.Random()

    @property
    def attempt(self) -> int:
        return self._attempt

    def reset(self) -> None:
        """Called after a successful *read*, not after a successful connect.

        The distinction matters on this gateway: a TCP connection to it succeeds
        immediately and then delivers nothing, so resetting on connect turns the
        backoff into a tight reconnect loop against a stream that is never going to
        produce a frame.
        """
        self._attempt = 0

    def base_delay(self, attempt: int) -> float:
        """The un-jittered delay for ``attempt``. Monotonic, and clamped to the cap."""
        if attempt <= 0:
            return self.initial
        log_factor = math.log(self.factor) if self.factor > 1.0 else 0.0
        if log_factor > 0 and attempt * log_factor > 40.0:
            # exp(40) is ~2.4e17; anything past here is the cap regardless, and
            # computing the power first would overflow.
            return self.cap
        return min(self.cap, self.initial * (self.factor ** attempt))

    def next_delay(self) -> float:
        delay = self.base_delay(self._attempt)
        self._attempt += 1
        if self.jitter > 0:
            delay *= 1.0 + self._rng.uniform(-self.jitter, self.jitter)
        # Clamped after jitter as well, so the delay never exceeds the documented cap
        # — an operator told "retries top out at 30 s" should not see 37.
        return min(self.cap, max(0.0, delay))

    def schedule(self, attempts: int) -> list[float]:
        """The un-jittered schedule, for documentation and for the test."""
        return [self.base_delay(i) for i in range(attempts)]


def ffmpeg_capture_options(config: SourceConfig) -> str:
    """The ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` value, ``key;value`` pairs, ``|``-joined.

    Each option, and the failure it answers:

    ``rtsp_transport;tcp``
        UDP loses packets on this gateway and the loss is silent — the stream keeps
        delivering frames, they are just corrupt. The resulting missed detections look
        exactly like a model problem, so this is the single most valuable line in the
        file.
    ``stimeout``
        Socket timeout in *microseconds*. Without it a half-open connection to a
        camera that has gone away blocks the read forever, and the thread for that
        camera never returns — including never noticing the shutdown flag.
    ``max_delay`` / ``reorder_queue_size``
        Bound the reordering buffer. A large reorder queue trades latency for
        smoothness, which is the wrong trade for live analytics: a frame that arrives
        smoothly two seconds late is two seconds of an incident nobody saw.
    ``analyzeduration`` / ``probesize``
        Kept small. ffmpeg's defaults spend up to five seconds probing the stream
        before returning the first frame; on fifty cameras that is four minutes of
        startup during a live test with an audience watching.
    """
    micros = int(max(1.0, config.open_timeout_seconds) * 1_000_000)
    return "|".join(
        (
            f"rtsp_transport;{config.transport}",
            f"stimeout;{micros}",
            "max_delay;500000",
            "reorder_queue_size;0",
            "analyzeduration;1000000",
            "probesize;500000",
        )
    )


def apply_ffmpeg_options(config: SourceConfig) -> str:
    """Set ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` in the environment. Returns the value.

    **Must be called before any ``cv2.VideoCapture`` is constructed.** OpenCV reads
    this variable when the capture object is created, not when it is opened, so
    setting it afterwards silently has no effect — the capture falls back to UDP and
    every symptom in this module's docstring returns. This is why it is a separate,
    named function rather than a line inside the capture setup: it needs to be
    obviously ordered.
    """
    value = ffmpeg_capture_options(config)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = value
    return value


def is_nonfatal_decoder_message(text: str) -> bool:
    """True for a decoder warning that must not trigger a reconnect.

    ffmpeg prints these on the process's stderr from C, so they cannot be intercepted
    by Python's logging module — this is not a log filter and cannot be one. It is
    used by the health-check path, which shells out to a probe and reads its captured
    stderr, and by the operator-facing diagnostics: a human staring at a wall of
    ``Could not find ref with POC`` needs to be told, in the tool, that this is
    expected when joining a stream mid-GOP.
    """
    return any(marker in text for marker in NONFATAL_DECODER_MESSAGES)


def assert_read_only(url: str) -> None:
    """Refuse a URL that is not a read.

    Belt and braces over the module's consume-only posture. ffmpeg will open plenty of
    schemes that write, and a mistyped or maliciously-supplied camera URL in the
    registry must not be able to turn a read-only worker into a publisher against the
    grid we were given access to on trust.
    """
    lowered = url.strip().lower()
    if not lowered:
        return
    if not lowered.startswith(_READ_ONLY_SCHEMES):
        raise ValueError(
            f"refusing to open {url!r}: only {', '.join(_READ_ONLY_SCHEMES)} are "
            f"permitted. This worker consumes video and never publishes."
        )


# ---------------------------------------------------------------------------
# Synthetic source
# ---------------------------------------------------------------------------


class SyntheticSource:
    """Frames generated in-process. No network, no codec, no camera.

    Carries the dry run, the self-test and every test in this package. It is also the
    only way to exercise the discontinuity path deterministically: ``loop_frames``
    makes the PTS jump backwards on a schedule, which is what the sandbox feed does
    at its loop point, and asserting on a real feed's loop would mean waiting for it.
    """

    def __init__(
        self,
        camera_id: int,
        *,
        width: int = 640,
        height: int = 360,
        fps: float = 12.0,
        total_frames: int = 240,
        loop_frames: int = 0,
        seed: int = 7,
        source_config: SourceConfig | None = None,
        sampling: SamplingConfig | None = None,
    ) -> None:
        self.camera_id = int(camera_id)
        self._width = width
        self._height = height
        self._interval = 1.0 / max(1e-3, fps)
        self._total = total_frames
        self._loop = loop_frames
        self._rng = np.random.default_rng(seed)
        cfg = source_config or SourceConfig()
        self._pacer = FramePacer(
            reorder_tolerance_seconds=cfg.reorder_tolerance_seconds,
            forward_gap_seconds=cfg.forward_gap_seconds,
        )
        self.sampler = AdaptiveSampler(sampling or SamplingConfig(enabled=False))
        self._background = self._rng.integers(
            40, 90, size=(height, width), dtype=np.uint8
        ).astype(np.uint8)
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def frames(self) -> Iterator[Frame]:
        pts = 0.0
        for index in range(self._total):
            if self._closed:
                return
            if self._loop and index and index % self._loop == 0:
                # Hard cut: PTS restarts. Exactly what the sandbox feeds do, and the
                # only cheap way to test that the tracker, motion reference and OCR
                # cache are all reset at the same moment.
                pts = 0.0
                self._background = self._rng.integers(
                    40, 90, size=(self._height, self._width), dtype=np.uint8
                ).astype(np.uint8)
            image = self._render(index)
            timing = self._pacer.observe(pts)
            yield Frame(image=image, pts=pts, timing=timing, connection=0)
            pts += self._interval

    def _render(self, index: int) -> np.ndarray:
        frame = np.repeat(self._background[:, :, None], 3, axis=2).copy()
        # Two moving blocks, so the motion gate has something concentrated to find and
        # the detector stub has something plausible to sit on top of.
        for offset, colour in ((0, (200, 200, 200)), (140, (120, 160, 220))):
            x = int((index * 6 + offset) % max(1, self._width - 60))
            y = int(self._height * 0.6)
            frame[y : y + 30, x : x + 55] = colour
        return frame


# ---------------------------------------------------------------------------
# RTSP source
# ---------------------------------------------------------------------------


class RtspSource:
    """Reconnecting RTSP reader. One instance and one thread per camera.

    ``frames()`` is an infinite generator: it connects, yields frames until the stream
    fails, backs off, and reconnects. Failure is the normal state of a fleet of a
    hundred field cameras, so it is handled in the control flow rather than raised.
    """

    def __init__(
        self,
        camera: CameraConfig,
        config: SourceConfig | None = None,
        sampling: SamplingConfig | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        assert_read_only(camera.url)
        self.camera_id = camera.camera_id
        self.url = camera.url
        self._config = config or SourceConfig()
        self._backoff = BackoffSchedule(self._config, rng)
        self._pacer = FramePacer(
            reorder_tolerance_seconds=self._config.reorder_tolerance_seconds,
            forward_gap_seconds=self._config.forward_gap_seconds,
        )
        self.sampler = AdaptiveSampler(sampling or SamplingConfig())
        self._stall = StallDetector(timeout_seconds=self._config.read_timeout_seconds)
        self._capture: object | None = None
        self._connection = 0
        self._stop = False
        self.frames_read = 0
        self.reconnects = 0
        self.read_failures = 0

    @property
    def stall(self) -> StallDetector:
        """Exposed so the worker can poll it and emit ``stream_gap``.

        Polled by the worker rather than checked in ``frames()``, because a stall is by
        definition the absence of a frame: the generator is blocked inside
        ``capture.read()`` and cannot notice its own silence. Only a second observer
        can.
        """
        return self._stall

    def close(self) -> None:
        self._stop = True
        self._release()

    def _release(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.release()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - release must never raise upward
                log.debug("camera %s: capture release failed", self.camera_id, exc_info=True)

    def frames(self) -> Iterator[Frame]:
        import cv2  # noqa: PLC0415 - kept local so the package imports without it

        # Before the first VideoCapture, never after. See apply_ffmpeg_options.
        apply_ffmpeg_options(self._config)

        last_frame_wall = 0.0
        while not self._stop:
            capture = self._open(cv2)
            if capture is None:
                delay = self._backoff.next_delay()
                log.warning(
                    "camera %s: open failed (attempt %d), retrying in %.1fs",
                    self.camera_id, self._backoff.attempt, delay,
                )
                time.sleep(delay)
                continue

            self._connection += 1
            self._pacer.reset()
            connect_wall = time.monotonic()
            gap = (connect_wall - last_frame_wall) if last_frame_wall else None
            failures = 0
            first_frame = True

            while not self._stop:
                ok, image = capture.read()
                now = time.monotonic()
                in_grace = (now - connect_wall) <= self._config.join_grace_seconds
                tolerance = self._config.max_read_failures * (
                    self._config.join_grace_multiplier if in_grace else 1
                )
                if not ok or image is None or getattr(image, "size", 0) == 0:
                    failures += 1
                    self.read_failures += 1
                    if failures >= tolerance:
                        # A run of failed reads during the join window is expected: we
                        # joined mid-GOP and the decoder is discarding frames whose
                        # reference pictures we never received. Outside the window it
                        # means the stream is gone.
                        break
                    continue

                failures = 0
                self._backoff.reset()  # A read succeeded, not merely a connect.
                pts = self._read_pts(cv2, capture)
                timing = self._pacer.observe(pts)
                self._stall.note_frame(now)
                last_frame_wall = now
                self.frames_read += 1

                frame = Frame(
                    image=image,
                    pts=pts,
                    timing=timing,
                    connection=self._connection,
                    gap_seconds=gap if first_frame else None,
                )
                first_frame = False
                gap = None

                if not self.sampler.should_process(timing):
                    # Shed here, before the frame is handed on. Dropping at the head of
                    # the pipeline keeps the frames we do process current; a bounded
                    # queue would drop the newest instead and leave us analysing an
                    # ever-staler window of the past.
                    continue
                yield frame

            self._release()
            self.reconnects += 1
            if self._stop:
                return
            delay = self._backoff.next_delay()
            log.warning(
                "camera %s: stream ended after %d frame(s); reconnecting in %.1fs",
                self.camera_id, self.frames_read, delay,
            )
            time.sleep(delay)

    def _open(self, cv2) -> object | None:
        try:
            capture = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        except Exception:
            log.warning("camera %s: VideoCapture construction failed", self.camera_id,
                        exc_info=True)
            return None
        if not capture.isOpened():
            try:
                capture.release()
            except Exception:  # pragma: no cover
                pass
            return None

        # Timeouts in milliseconds, belt and braces over ``stimeout``: which of the two
        # is honoured depends on the ffmpeg build, and a hung read is the failure that
        # wedges a camera thread permanently.
        for prop, seconds in (
            ("CAP_PROP_OPEN_TIMEOUT_MSEC", self._config.open_timeout_seconds),
            ("CAP_PROP_READ_TIMEOUT_MSEC", self._config.read_timeout_seconds),
        ):
            attr = getattr(cv2, prop, None)
            if attr is not None:
                try:
                    capture.set(attr, seconds * 1000.0)
                except Exception:  # pragma: no cover - older builds reject the prop
                    log.debug("camera %s: %s unsupported", self.camera_id, prop)

        # Ask for the smallest possible internal buffer. On a live feed a buffered
        # frame is a stale frame, and staleness is the thing this whole design is
        # fighting. Not honoured by every backend, hence no assertion on the result.
        buffer_prop = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
        if buffer_prop is not None:
            try:
                capture.set(buffer_prop, 1)
            except Exception:  # pragma: no cover
                pass

        # Deliberately NOT read: cv2.CAP_PROP_FPS. The gateway reports the container
        # header's value, which on these streams is variously 0, 25 on a 12 fps feed,
        # or 90000. Anything derived from it is wrong, so nothing is derived from it.
        self._capture = capture
        return capture

    def _read_pts(self, cv2, capture) -> float:
        """Presentation timestamp in seconds, from the container.

        Falls back to a frame counter scaled by the *measured* mean interval when
        ``CAP_PROP_POS_MSEC`` is unusable — some gateway builds return 0 for every
        frame. The fallback is explicitly a fallback: it cannot detect a loop point,
        so a camera that lands on it loses scene-change detection and gets a log line
        saying so rather than silently degrading.
        """
        try:
            millis = float(capture.get(cv2.CAP_PROP_POS_MSEC))
        except Exception:  # pragma: no cover
            millis = 0.0
        if millis > 0.0:
            return millis / 1000.0
        last = self._pacer.last
        if last is None:
            return 0.0
        if last.frame_index == 8:
            log.warning(
                "camera %s: CAP_PROP_POS_MSEC is not reporting; synthesising PTS from "
                "a frame counter. Scene-change detection is degraded on this camera.",
                self.camera_id,
            )
        # Measured elapsed wall time is the best available proxy once the container has
        # stopped cooperating. It is wrong during the join burst, which is why the
        # sampler ignores its first samples.
        return last.pts + max(1e-3, time.monotonic() - last.arrival_wall)


def build_source(
    camera: CameraConfig,
    config: SourceConfig | None = None,
    sampling: SamplingConfig | None = None,
) -> FrameSource:
    """Pick a source for a camera. ``stub://`` or an empty URL means synthetic."""
    if camera.stub or not camera.url or camera.url.startswith("stub://"):
        return SyntheticSource(camera.camera_id, source_config=config, sampling=sampling)
    return RtspSource(camera, config, sampling)
