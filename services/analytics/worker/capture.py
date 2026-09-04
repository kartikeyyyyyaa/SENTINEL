"""RTSP capture. Eight rules, each one a way to fail that we have already found.

The sandbox gateway's own integration guide warns about most of what follows.
Ignoring any single item here produces a worker that appears to run and quietly
reports nonsense, which is worse than one that crashes.

1. **RTSP over TCP, forced.** ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` is read by FFmpeg
   when the capture is *constructed*, so it must be set before
   ``cv2.VideoCapture``. Over UDP this gateway drops packets under load without
   reporting anything: you get frames with smeared macroblocks, the detector
   misses objects in them, and the symptom looks like a bad model.

2. **``CAP_PROP_FPS`` is not trustworthy.** It reports the container's declared
   rate, which on these streams is frequently 0, 90000, or simply wrong. Every
   rate in this module is measured.

3. **All timing comes from PTS, never arrival time.** On connect the gateway
   flushes a buffered GOP, so the first one to two seconds of frames arrive
   *faster than real time* — sometimes 5-10x. A speed or dwell computed from
   arrival timestamps is therefore wildly wrong for exactly the period an
   operator is most likely to be watching. ``PtsTimeline`` converts
   ``CAP_PROP_POS_MSEC`` into a monotonic stream timeline and detects the loop
   discontinuity, where each feed restarts with a hard scene cut and PTS jumps
   backwards. At a cut we start a new segment and reset tracker state rather than
   interpolate across it: interpolating manufactures a vehicle that crossed the
   frame in one frame interval, which a speeding rule would dutifully report.

4. **Frame intervals are not uniform.** Never assume a fixed dt. Every timing
   value in this module is a measured interval.

5. **Reconnect with jittered exponential backoff**, 2 s doubling to a 30 s cap.
   Fifty workers reconnecting in lockstep against a sandbox we do not own is a
   denial of service; the jitter is what desynchronises them.

6. **Join-time decoder warnings are expected and non-fatal.** ``Error
   constructing the frame RPS`` and ``Could not find ref with POC`` mean the
   replayed GOP references frames that were never sent to us. They stop on their
   own once a keyframe arrives. FFmpeg prints them from the C layer, so Python
   cannot catch them — what Python *can* do is not panic at the run of failed
   reads that accompanies them, which is why the failure tolerance is wider
   during the join grace window.

7. **Mixed codecs and mixed resolutions.** H.264 and H.265 in the same fleet, and
   no two cameras agreeing on frame size. There is therefore no fixed-shape
   batch: every frame is letterboxed to the model input individually, and
   ``Frame`` carries its own dimensions rather than the worker caching one shape.

8. **Consume only.** Nothing in this module writes, publishes or POSTs to the
   sandbox. The only outbound traffic the worker generates goes to our own
   registry, from ``sink.py``.
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from .config import CameraSpec, CaptureConfig, SamplingConfig

log = logging.getLogger(__name__)

# Decoder complaints that are expected at join and must not be treated as
# failures. Matched case-insensitively on substrings because FFmpeg's exact
# wording varies between builds.
NONFATAL_DECODER_MESSAGES: tuple[str, ...] = (
    "error constructing the frame rps",
    "could not find ref with poc",
    "missing reference picture",
    "no frame!",
    "illegal short term buffer state detected",
    "reference picture missing during reorder",
    "co located poc unavailable",
    "mmco: unref short failure",
)


def is_nonfatal_decoder_message(message: str) -> bool:
    """True for the join-time reference-picture complaints listed above.

    Used where a decoder message is available to Python at all (a log redirect,
    or a subprocess wrapper). The classifier lives here rather than at the call
    site so there is one list to extend when a new build invents new wording.
    """
    low = message.lower()
    return any(pattern in low for pattern in NONFATAL_DECODER_MESSAGES)


def ffmpeg_capture_options(config: CaptureConfig) -> str:
    """The ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` value, semicolon-separated pairs.

    ``stimeout`` is in microseconds and is the only timeout FFmpeg honours for an
    RTSP socket; without it a dead stream blocks ``read()`` indefinitely and the
    worker thread is gone for good — no exception, no reconnect, just a thread
    that never returns. Which is how you lose a camera for the whole demo.
    """
    micros = int(max(1.0, config.open_timeout_seconds) * 1_000_000)
    return "|".join(
        (
            f"rtsp_transport;{config.transport}",
            f"stimeout;{micros}",
            "max_delay;500000",
            # Analysis of a long buffer delays the first frame by seconds and
            # gains nothing: we do not need the decoder's opinion on the stream,
            # we need frames.
            "analyzeduration;1000000",
            "probesize;500000",
            "reorder_queue_size;0",
        )
    )


def apply_ffmpeg_options(config: CaptureConfig) -> str:
    """Install the options into the environment. Must run before VideoCapture.

    FFmpeg reads this variable at capture construction, not at read time, so
    setting it after the object exists silently does nothing and you are back on
    UDP without being told.
    """
    value = ffmpeg_capture_options(config)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = value
    return value


class BackoffSchedule:
    """Jittered exponential backoff with a hard cap.

    Two separate accessors on purpose. ``base_delay`` is deterministic, monotonic
    non-decreasing and capped — that is the property worth testing and worth
    reasoning about. ``next_delay`` adds jitter and is what the caller sleeps for.
    Jittered delays are clamped to the cap as well, so "never waits longer than
    30 s" is a guarantee rather than an approximation.
    """

    __slots__ = ("initial", "factor", "cap", "jitter", "_attempt", "_rng")

    def __init__(self, config: CaptureConfig, rng: random.Random | None = None) -> None:
        self.initial = max(0.1, config.backoff_initial_seconds)
        self.factor = max(1.0, config.backoff_factor)
        self.cap = max(self.initial, config.backoff_cap_seconds)
        self.jitter = min(max(0.0, config.backoff_jitter), 0.99)
        self._attempt = 0
        self._rng = rng or random.Random()

    def base_delay(self, attempt: int) -> float:
        """Un-jittered delay for a zero-based attempt number."""
        if attempt <= 0:
            return self.initial
        # Computed in log space and clamped before exponentiating, because
        # 2.0 ** 900 after a fortnight of failed reconnects is an OverflowError
        # rather than a large float, and a crash in the reconnect path is the one
        # crash you cannot recover from.
        if attempt * math.log(self.factor or 1.0) > 40:
            return self.cap
        return min(self.cap, self.initial * (self.factor**attempt))

    def next_delay(self) -> float:
        delay = self.base_delay(self._attempt)
        self._attempt += 1
        if self.jitter:
            delay *= 1.0 + self._rng.uniform(-self.jitter, self.jitter)
        return min(self.cap, max(0.05, delay))

    def reset(self) -> None:
        """Call after a *successful* read, not a successful connect.

        A gateway that accepts the TCP connection and then delivers nothing is a
        common failure here. Resetting on connect turns that into a tight
        reconnect loop at the initial delay forever.
        """
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt


@dataclass(frozen=True, slots=True)
class FrameTiming:
    """What the PTS timeline concluded about one frame.

    ``t`` is the number every downstream consumer uses. It is monotonic across
    loop cuts (so nothing has to cope with time going backwards) while
    ``segment_id`` changes (so nothing mistakes the two sides of a cut for
    continuous motion).
    """

    index: int
    t: float  # Monotonic stream-timeline seconds.
    pts_seconds: float  # Raw PTS as reported, for diagnostics.
    dt: float  # Interval since the previous frame. Not constant. May be 0.
    segment_id: int
    discontinuity: bool = False  # Loop cut. Reset the tracker.
    reordered: bool = False  # Small backwards step, absorbed as dt=0.
    gap: bool = False  # Large forward jump; frames are missing.
    pts_valid: bool = True  # False means t was synthesised, see PtsTimeline.


class PtsTimeline:
    """Turns ``CAP_PROP_POS_MSEC`` into a monotonic timeline plus segment ids.

    The distinction that matters: a *small* backwards PTS step is normal — B-frame
    reordering wobbles by a frame or two and the decoder is not obliged to hand
    frames over in presentation order. A *large* backwards step is the feed
    looping, with a hard scene cut. Treating the first as a cut fragments every
    track; treating the second as reordering fabricates motion across the cut.
    ``loop_jump_tolerance_seconds`` is the line between them, and it is
    configurable because different feeds reorder by different amounts.
    """

    __slots__ = (
        "loop_tolerance",
        "segment_gap",
        "forward_gap_seconds",
        "nominal_interval",
        "_last_pts",
        "_last_t",
        "_segment_pts_start",
        "_segment_id",
        "_offset",
        "_index",
        "_segment_first_t",
        "_invalid_pts_frames",
        "_arrival_last",
        "_replay_ratio",
    )

    def __init__(
        self,
        loop_tolerance_seconds: float = 1.0,
        segment_gap_seconds: float = 1.0,
        forward_gap_seconds: float = 10.0,
        nominal_interval_seconds: float = 0.04,
    ) -> None:
        self.loop_tolerance = max(0.0, loop_tolerance_seconds)
        self.segment_gap = max(0.0, segment_gap_seconds)
        self.forward_gap_seconds = forward_gap_seconds
        self.nominal_interval = nominal_interval_seconds
        self._last_pts: float | None = None
        self._last_t = 0.0
        self._segment_pts_start = 0.0
        self._segment_id = 0
        self._offset = 0.0
        self._index = 0
        self._segment_first_t = 0.0
        self._invalid_pts_frames = 0
        self._arrival_last: float | None = None
        self._replay_ratio = 1.0

    @property
    def segment_id(self) -> int:
        return self._segment_id

    @property
    def invalid_pts_frames(self) -> int:
        """Frames whose PTS was unusable and whose timeline position was
        synthesised. A nonzero value here has to appear in any measurement
        derived from this stream, because those timings are estimates."""
        return self._invalid_pts_frames

    @property
    def replay_ratio(self) -> float:
        """Stream time advanced per unit of wall time, smoothed.

        About 1.0 on a healthy live feed. Much greater than 1.0 means the gateway
        is flushing its buffer at us — the join burst. This is measured only so it
        can be reported; nothing in the worker's timing depends on it, which is
        the entire point of driving from PTS.
        """
        return self._replay_ratio

    def is_replaying(self, threshold: float = 1.5) -> bool:
        return self._replay_ratio > threshold

    @property
    def measured_fps(self) -> float:
        """Frames per second of *stream* time in the current segment.

        Measured, because ``CAP_PROP_FPS`` lies. Scoped to the segment because
        spanning a loop cut would divide by a timeline that includes the inserted
        gap.
        """
        span = self._last_t - self._segment_first_t
        if span <= 0:
            return 0.0
        return (self._index - 1) / span if self._index > 1 else 0.0

    def observe(self, pts_ms: float | None, arrival_monotonic: float | None = None) -> FrameTiming:
        """Place one frame on the timeline."""
        pts_valid = pts_ms is not None and math.isfinite(pts_ms) and pts_ms >= 0.0
        if pts_valid:
            pts = float(pts_ms) / 1000.0  # type: ignore[arg-type]
        else:
            # Some gateway builds report 0 or NaN for POS_MSEC on H.265. We still
            # need a timeline, so we advance by the last measured interval and
            # flag it. Flagging matters: a synthesised timeline is fine for
            # ordering and useless for a speed claim, and the consumer has to be
            # able to tell.
            self._invalid_pts_frames += 1
            pts = (self._last_pts or 0.0) + self.nominal_interval

        if arrival_monotonic is not None:
            self._update_replay_ratio(pts, arrival_monotonic)

        if self._last_pts is None:
            self._last_pts = pts
            self._segment_pts_start = pts
            self._last_t = 0.0
            self._segment_first_t = 0.0
            self._index = 1
            return FrameTiming(
                index=0, t=0.0, pts_seconds=pts, dt=0.0, segment_id=self._segment_id,
                pts_valid=pts_valid,
            )

        delta = pts - self._last_pts
        index = self._index
        self._index += 1

        if delta < -self.loop_tolerance:
            # Loop cut. New segment, and the timeline jumps forward by a nominal
            # gap so that the two sides can never look adjacent in time. The
            # caller must reset tracker state on seeing discontinuity=True.
            self._segment_id += 1
            self._offset = self._last_t + self.segment_gap
            self._segment_pts_start = pts
            self._last_pts = pts
            self._last_t = self._offset
            self._segment_first_t = self._offset
            return FrameTiming(
                index=index, t=self._last_t, pts_seconds=pts, dt=0.0,
                segment_id=self._segment_id, discontinuity=True, pts_valid=pts_valid,
            )

        if delta < 0.0:
            # Reordering. Hold the timeline still rather than moving it backwards;
            # a dt of 0 is honest and a negative dt divides badly in every speed
            # calculation downstream. The reference PTS is not moved back either,
            # so one late frame cannot drag the timeline with it.
            return FrameTiming(
                index=index, t=self._last_t, pts_seconds=pts, dt=0.0,
                segment_id=self._segment_id, reordered=True, pts_valid=pts_valid,
            )

        t = self._offset + (pts - self._segment_pts_start)
        dt = t - self._last_t
        self._last_pts = pts
        self._last_t = t
        # A large forward jump means the decoder or the network lost a chunk. Not
        # a new segment: it is the same scene and the tracker's own age-based
        # retirement handles the missing interval correctly. Flagged so a dwell
        # measurement spanning the gap can be marked as such.
        gap = dt > self.forward_gap_seconds
        return FrameTiming(
            index=index, t=t, pts_seconds=pts, dt=dt, segment_id=self._segment_id,
            gap=gap, pts_valid=pts_valid,
        )

    def _update_replay_ratio(self, pts: float, arrival: float) -> None:
        if self._arrival_last is not None and self._last_pts is not None:
            wall = arrival - self._arrival_last
            stream = pts - self._last_pts
            if wall > 1e-4 and stream > 0:
                inst = stream / wall
                self._replay_ratio = 0.3 * inst + 0.7 * self._replay_ratio
        self._arrival_last = arrival

    def reset(self) -> None:
        """Full reset, for a reconnect.

        The segment id is *incremented* rather than zeroed: after a reconnect we
        have no idea how much stream we missed, so the new frames are not the same
        segment and must not be correlated with the old ones.
        """
        self._segment_id += 1
        self._offset = self._last_t + self.segment_gap
        self._last_pts = None
        self._last_t = self._offset
        self._segment_first_t = self._offset
        self._index = 0
        self._arrival_last = None
        self._replay_ratio = 1.0


@dataclass(frozen=True, slots=True, eq=False)
class Frame:
    """One decoded frame plus everything known about when it happened.

    Carries its own ``width``/``height`` because the fleet mixes resolutions and
    nothing may cache "the" frame size. ``eq=False`` because the payload is a
    numpy array and dataclass equality on one raises rather than returning a bool.
    """

    image: np.ndarray
    timing: FrameTiming
    camera_id: str
    width: int
    height: int

    @property
    def t(self) -> float:
        return self.timing.t

    @property
    def segment_id(self) -> int:
        return self.timing.segment_id

    @staticmethod
    def build(image: np.ndarray, timing: FrameTiming, camera_id: str) -> Frame:
        h, w = image.shape[:2]
        return Frame(image=image, timing=timing, camera_id=camera_id, width=int(w), height=int(h))


class AdaptiveSampler:
    """Raises the frame stride when the worker cannot keep up.

    The failure this prevents: per-frame cost exceeds the frame interval, the
    decode backlog grows without bound, RSS climbs, and every event emitted is
    minutes stale. Bounding the queue instead of the stride just moves the
    problem — you still process stale frames, you merely lose the newest ones.

    ``duty`` is compute seconds per second of *stream* time, measured between
    consecutive processed frames. Widening the stride multiplies the stream
    interval per processed frame, so duty falls roughly in proportion, which makes
    the control loop a simple one: over the high watermark, widen; under the low
    watermark, narrow.

    On a stride change the smoothed duty is rescaled by the stride ratio rather
    than left alone. Without that, the EWMA still holds pre-change values, the
    controller sees a duty it has already fixed, and it overshoots to max_stride
    on the first busy second.
    """

    __slots__ = (
        "config",
        "stride",
        "_duty",
        "_samples",
        "frames_seen",
        "frames_processed",
        "frames_skipped",
        "stride_changes",
        "_last_processed_t",
    )

    def __init__(self, config: SamplingConfig) -> None:
        self.config = config
        self.stride = max(1, config.min_stride)
        self._duty: float | None = None
        self._samples = 0
        self.frames_seen = 0
        self.frames_processed = 0
        self.frames_skipped = 0
        self.stride_changes = 0
        self._last_processed_t: float | None = None

    @property
    def duty(self) -> float:
        return self._duty if self._duty is not None else 0.0

    def should_process(self, timing: FrameTiming) -> bool:
        """Decide by frame index, not by clock.

        Index-based striding is deterministic and reproducible, which matters
        because the cascade's measured reduction figures go into the submission
        and have to be reproducible from a recorded stream.

        A discontinuity is always processed: it is the frame that tells the
        tracker to reset, and skipping it leaves stale tracks alive across a
        scene cut.
        """
        self.frames_seen += 1
        if timing.discontinuity or self.stride <= 1 or timing.index % self.stride == 0:
            self.frames_processed += 1
            return True
        self.frames_skipped += 1
        return False

    def observe(self, processing_seconds: float, stream_t: float) -> None:
        """Record the cost of one processed frame.

        ``stream_t`` is the frame's timeline position, so the stream interval
        between processed frames is derived here rather than trusted from the
        caller — the caller does not know which frames were skipped.
        """
        last = self._last_processed_t
        self._last_processed_t = stream_t
        if last is None:
            return
        stream_dt = stream_t - last
        if stream_dt <= 1e-6:
            return  # Reordered or discontinuous frame; no meaningful interval.
        inst = processing_seconds / stream_dt
        alpha = self.config.ewma_alpha
        self._duty = inst if self._duty is None else alpha * inst + (1 - alpha) * self._duty
        self._samples += 1
        self._adjust()

    def _adjust(self) -> None:
        if not self.config.enabled or self._samples < self.config.min_samples:
            return
        assert self._duty is not None
        old = self.stride
        if self._duty > self.config.high_watermark and self.stride < self.config.max_stride:
            self.stride += 1
        elif self._duty < self.config.low_watermark and self.stride > self.config.min_stride:
            self.stride -= 1
        if self.stride != old:
            self.stride_changes += 1
            self._duty *= old / self.stride
            log.info(
                "adaptive sampling: stride %d -> %d (duty %.2f, target %.2f)",
                old, self.stride, self._duty, self.config.target_duty,
            )

    def reset_timeline(self) -> None:
        """Forget the last processed timestamp, keeping the learned stride.

        Called at a loop cut or reconnect. The stride is a property of this
        machine's throughput and does not change because the stream restarted;
        the interval reference does.
        """
        self._last_processed_t = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stride": self.stride,
            "duty": round(self.duty, 4),
            "frames_seen": self.frames_seen,
            "frames_processed": self.frames_processed,
            "frames_skipped": self.frames_skipped,
            "stride_changes": self.stride_changes,
        }


class FrameSource(Protocol):
    """What the cascade consumes. Implemented by RTSP capture and by a synthetic
    generator, so the whole pipeline runs end to end with no network and no
    models."""

    camera_id: str

    def frames(self) -> Iterator[Frame]: ...

    def close(self) -> None: ...


@dataclass
class CaptureStats:
    """Capture-side counters. Reported alongside the cascade's numbers; a
    reduction factor means nothing without knowing how many frames arrived."""

    frames_delivered: int = 0
    read_failures: int = 0
    reconnects: int = 0
    segments: int = 1
    discontinuities: int = 0
    reordered_frames: int = 0
    gaps: int = 0
    synthesised_timestamps: int = 0
    first_frame_wait_seconds: float = 0.0
    wall_seconds: float = 0.0
    declared_fps: float = 0.0  # What the stream claims. Recorded to show it is wrong.
    measured_stream_fps: float = 0.0  # From PTS.
    measured_delivery_fps: float = 0.0  # From wall clock. Differs at join: the burst.

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames_delivered": self.frames_delivered,
            "read_failures": self.read_failures,
            "reconnects": self.reconnects,
            "segments": self.segments,
            "discontinuities": self.discontinuities,
            "reordered_frames": self.reordered_frames,
            "gaps": self.gaps,
            "synthesised_timestamps": self.synthesised_timestamps,
            "first_frame_wait_seconds": round(self.first_frame_wait_seconds, 3),
            "declared_fps": round(self.declared_fps, 3),
            "measured_stream_fps": round(self.measured_stream_fps, 3),
            "measured_delivery_fps": round(self.measured_delivery_fps, 3),
        }


class RtspCapture:
    """A reconnecting RTSP reader that yields ``Frame`` objects forever.

    Deliberately a generator rather than a callback or a queue. A generator makes
    the consumer set the pace, so a slow cascade produces backpressure the
    adaptive sampler can see, instead of a queue that grows quietly until the
    process is killed by the OOM reaper an hour into the demo.
    """

    def __init__(
        self,
        spec: CameraSpec,
        config: CaptureConfig,
        stop: Any = None,
        rng: random.Random | None = None,
    ) -> None:
        self.spec = spec
        self.camera_id = spec.camera_id
        self.config = config
        self.stats = CaptureStats()
        self.timeline = PtsTimeline(
            loop_tolerance_seconds=config.loop_jump_tolerance_seconds,
            segment_gap_seconds=config.segment_gap_seconds,
        )
        self._backoff = BackoffSchedule(config, rng=rng)
        self._stop = stop  # threading.Event or anything with .is_set()
        self._cap: Any = None
        self._cv2: Any = None

    def _stopping(self) -> bool:
        return bool(self._stop is not None and self._stop.is_set())

    def _load_cv2(self) -> Any:
        if self._cv2 is None:
            try:
                import cv2  # noqa: PLC0415 - lazy on purpose, see module docstring
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RuntimeError(
                    "opencv-python is required for RTSP capture but is not installed. "
                    "Install it with: pip install -r services/analytics/requirements.txt "
                    "Or run the worker against synthetic frames with "
                    "ANALYTICS_CAMERAS='cam=stub://' python -m worker.main --dry-run"
                ) from exc
            self._cv2 = cv2
        return self._cv2

    def _open(self) -> Any:
        cv2 = self._load_cv2()
        # Rule 1. Set before construction; FFmpeg reads it there and nowhere else.
        options = apply_ffmpeg_options(self.config)
        log.debug("camera %s opening with FFmpeg options %s", self.camera_id, options)
        cap = cv2.VideoCapture(self.spec.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            raise ConnectionError(f"could not open stream for camera {self.camera_id}")
        # Rule 2. Recorded only so the report can show the declared value next to
        # the measured one. Nothing computes from it.
        try:
            self.stats.declared_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        except Exception:  # noqa: BLE001 - a property read must never kill capture
            self.stats.declared_fps = 0.0
        # A one-frame internal buffer keeps us near the live edge. Without it
        # OpenCV hands over whatever is oldest in its queue, so falling behind
        # means processing progressively staler frames instead of dropping them.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:  # noqa: BLE001
            pass
        return cap

    def _pts_ms(self, cap: Any) -> float | None:
        cv2 = self._cv2
        try:
            value = float(cap.get(cv2.CAP_PROP_POS_MSEC))
        except Exception:  # noqa: BLE001
            return None
        if not math.isfinite(value):
            return None
        return value

    def frames(self) -> Iterator[Frame]:
        started = time.monotonic()
        first_frame_at: float | None = None
        while not self._stopping():
            try:
                self._cap = self._open()
            except Exception as exc:  # noqa: BLE001 - every open failure is retryable
                delay = self._backoff.next_delay()
                log.warning(
                    "camera %s open failed (attempt %d): %s; retrying in %.1fs",
                    self.camera_id, self._backoff.attempt, exc, delay,
                )
                self.stats.reconnects += 1
                if self._sleep(delay):
                    return
                continue

            connected_at = time.monotonic()
            failures = 0
            got_any = False
            while not self._stopping():
                ok, image = self._cap.read()
                arrival = time.monotonic()
                if not ok or image is None:
                    failures += 1
                    self.stats.read_failures += 1
                    # Rule 6. During the join grace window a run of failed reads
                    # is the expected consequence of the replayed GOP referencing
                    # frames we never received; the decoder recovers at the next
                    # keyframe. Outside that window the same run means a dead
                    # socket.
                    in_join_grace = (arrival - connected_at) < 2.0 and not got_any
                    tolerance = self.config.max_read_failures * (4 if in_join_grace else 1)
                    if failures <= tolerance:
                        time.sleep(0.01)
                        continue
                    log.warning(
                        "camera %s: %d consecutive read failures, reconnecting",
                        self.camera_id, failures,
                    )
                    break

                failures = 0
                got_any = True
                # Reset backoff only once frames are actually flowing. A gateway
                # that accepts the connection and delivers nothing is common, and
                # resetting on connect turns it into a hot reconnect loop.
                self._backoff.reset()
                timing = self.timeline.observe(self._pts_ms(self._cap), arrival)
                self._account(timing)
                if first_frame_at is None:
                    first_frame_at = arrival
                    self.stats.first_frame_wait_seconds = arrival - started
                self.stats.frames_delivered += 1
                self.stats.wall_seconds = arrival - (first_frame_at or arrival)
                if self.stats.wall_seconds > 0:
                    self.stats.measured_delivery_fps = (
                        self.stats.frames_delivered / self.stats.wall_seconds
                    )
                self.stats.measured_stream_fps = self.timeline.measured_fps
                yield Frame.build(image, timing, self.camera_id)

            self._release()
            if self._stopping():
                return
            self.timeline.reset()
            self.stats.reconnects += 1
            self.stats.segments += 1
            delay = self._backoff.next_delay()
            log.info("camera %s reconnecting in %.1fs", self.camera_id, delay)
            if self._sleep(delay):
                return

    def _account(self, timing: FrameTiming) -> None:
        if timing.discontinuity:
            self.stats.discontinuities += 1
            self.stats.segments += 1
        if timing.reordered:
            self.stats.reordered_frames += 1
        if timing.gap:
            self.stats.gaps += 1
        if not timing.pts_valid:
            self.stats.synthesised_timestamps += 1

    def _sleep(self, seconds: float) -> bool:
        """Sleep, but wake immediately on shutdown. Returns True if stopping.

        A plain ``time.sleep(30)`` in the reconnect path means Ctrl-C takes up to
        thirty seconds per camera to take effect, which during a live rehearsal
        reads as a hung program.
        """
        if self._stop is not None and hasattr(self._stop, "wait"):
            return bool(self._stop.wait(seconds))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stopping():
                return True
            time.sleep(min(0.2, deadline - time.monotonic()))
        return self._stopping()

    def _release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None

    def close(self) -> None:
        self._release()


@dataclass
class SyntheticSource:
    """Deterministic frames with no decoder, no network and no model.

    Exists for three jobs: the unit tests, a no-GPU dry run that proves the whole
    pipeline is wired up, and reproducing the two stream behaviours that are
    otherwise only observable against the live sandbox — a non-uniform frame
    interval and a loop cut with the PTS jumping backwards.

    Resolution and interval are parameters rather than constants because the fleet
    has neither fixed, and a test that only ever sees 640x480 at exactly 25 fps
    would not have caught the letterboxing bug it was written to catch.
    """

    camera_id: str = "synthetic"
    frame_count: int = 120
    width: int = 640
    height: int = 360
    intervals_ms: tuple[float, ...] = (40.0, 33.0, 52.0, 40.0, 41.0, 38.0)
    loop_at: int | None = None  # Frame index where PTS jumps back to zero.
    moving: bool = True
    speed_px: float = 6.0
    # Block dimensions. Sized to be a plausible near vehicle at this resolution, so
    # that stage 3's minimum-vehicle-box and minimum-plate-width gates are actually
    # exercised rather than rejecting everything and making the dry run look like a
    # cascade that filters perfectly.
    block_width: int = 160
    block_height: int = 90
    seed: int = 7
    config: CaptureConfig = field(default_factory=CaptureConfig)

    def __post_init__(self) -> None:
        self.timeline = PtsTimeline(
            loop_tolerance_seconds=self.config.loop_jump_tolerance_seconds,
            segment_gap_seconds=self.config.segment_gap_seconds,
        )
        self.stats = CaptureStats()

    def frames(self) -> Iterator[Frame]:
        rng = np.random.default_rng(self.seed)
        # Fixed background with light noise. Noise matters: a perfectly static
        # synthetic background would let a motion gate pass that could never
        # survive real sensor noise at night.
        background = rng.integers(60, 90, size=(self.height, self.width, 3), dtype=np.uint8)
        pts_ms = 0.0
        for i in range(self.frame_count):
            if self.loop_at is not None and i == self.loop_at:
                pts_ms = 0.0  # The hard cut. PTS restarts.
            elif i:
                pts_ms += self.intervals_ms[i % len(self.intervals_ms)]
            image = background.copy()
            if self.moving:
                bw, bh = self.block_width, self.block_height
                x = int((i * self.speed_px) % max(1, self.width - bw - 1))
                y = max(0, self.height // 2 - bh // 2)
                image[y : y + bh, x : x + bw] = 220  # Stands in for a vehicle.
            timing = self.timeline.observe(pts_ms, arrival_monotonic=i * 0.04)
            self.stats.frames_delivered += 1
            if timing.discontinuity:
                self.stats.discontinuities += 1
                self.stats.segments += 1
            yield Frame.build(image, timing, self.camera_id)

    def close(self) -> None:
        return None


def build_source(spec: CameraSpec, config: CaptureConfig, stop: Any = None) -> FrameSource:
    """Pick a source for a camera spec.

    ``stub://`` URLs get synthetic frames. That is how the dry run exercises the
    real cascade, the real tracker and the real sink on a machine with no GPU, no
    model weights and no route to the sandbox.
    """
    if spec.stub or spec.url.startswith("stub://") or not spec.url:
        return SyntheticSource(camera_id=spec.camera_id, config=config)
    return RtspCapture(spec, config, stop=stop)
