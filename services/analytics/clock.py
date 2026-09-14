"""Frame pacing: what time is this frame, and can we afford to process it?

Two problems, both about time, and they are separated here because they fail
differently.

**What time is this frame?** ``services.common.events.StreamClock`` answers that
and this module does not duplicate it. ``FramePacer`` wraps it and adds the thing
the worker needs on top: a measurement of how far behind the live edge we are, and
the flag that says the stream just cut. Anchoring, re-anchoring and the reorder
tolerance all live in the shared contract, because a worker that computed
timestamps differently from the correlation layer's expectations would produce
events that look fine and join wrongly.

**Can we afford this frame?** ``AdaptiveSampler`` answers that. The alternative —
a bounded frame queue — is worse in a specific way: a full queue drops the
*newest* frame, so under sustained overload the worker analyses an ever-staler
window of the past while the incident happens off-camera. Dropping frames at the
head of the pipeline instead keeps the ones we do process current. On a live
incident, three fresh frames a second beats twenty-five frames from ninety
seconds ago.

Both classes are deliberately free of ``time.sleep``. Nothing here paces by
blocking, because a blocking pacer on a thread shared with a decoder converts a
timing problem into a deadlock. The sampler returns a verdict; the caller acts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from services.common.events import StreamClock

from .config import SamplingConfig


@dataclass(slots=True)
class FrameTiming:
    """The temporal facts about one decoded frame.

    ``ts`` is PTS-derived capture time and is what goes on every event.
    ``arrival_wall`` is when we got it. They differ by ``lag_seconds``, and the
    difference is not noise: at connect the gateway replays a buffered GOP, so a
    second of stream arrives in a fraction of a second and lag is *negative* —
    frames appear to be from the future. That is why ``lag_seconds`` is signed and
    why the sampler ignores its first few samples.
    """

    pts: float
    ts: datetime
    arrival_wall: float
    lag_seconds: float
    discontinuity: bool
    frame_index: int

    @property
    def is_replay_burst(self) -> bool:
        """True while stream time is running ahead of wall time.

        Not an error. It is the signature of the join-time GOP replay, and a
        controller that reads it as "we have spare capacity" will widen the stride
        for the wrong reason.
        """
        return self.lag_seconds < 0.0


class FramePacer:
    """Assigns capture time to frames and measures how stale they are.

    One instance per stream. Reset on reconnect: the anchor from the previous
    socket is meaningless once the gateway has replayed a new GOP from a different
    point in the loop.
    """

    __slots__ = (
        "_clock",
        "_monotonic",
        "_frame_index",
        "_first_pts",
        "_first_wall",
        "_last_timing",
        "discontinuities",
        "frames_seen",
    )

    def __init__(
        self,
        *,
        reorder_tolerance_seconds: float | None = None,
        forward_gap_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monotonic = monotonic
        self._clock = _make_clock(reorder_tolerance_seconds, forward_gap_seconds)
        self._frame_index = 0
        self._first_pts: float | None = None
        self._first_wall: float | None = None
        self._last_timing: FrameTiming | None = None
        self.discontinuities = 0
        self.frames_seen = 0

    @property
    def clock(self) -> StreamClock:
        return self._clock

    @property
    def last(self) -> FrameTiming | None:
        return self._last_timing

    def observe(self, pts: float) -> FrameTiming:
        """Record a decoded frame and return its timing.

        ``pts`` comes from the decoder (``CAP_PROP_POS_MSEC`` / container PTS),
        never from ``time.monotonic``. Arrival time is compressed at join and
        distended when the network stalls, so timestamping by arrival would place
        the same real event at two different instants on two cameras — and
        cross-camera travel time, the whole point of the correlation layer, is a
        subtraction of two such instants.
        """
        now = self._monotonic()
        ts, discontinuity = self._clock.advance(pts)
        self.frames_seen += 1

        if discontinuity or self._first_pts is None:
            # Re-anchor the lag baseline too. Keeping the old baseline across a
            # loop point produces a lag reading of however long the feed's loop is,
            # which would make the sampler shed almost every frame for no reason.
            self._first_pts = pts
            self._first_wall = now
            if discontinuity:
                self.discontinuities += 1

        assert self._first_pts is not None and self._first_wall is not None
        stream_elapsed = pts - self._first_pts
        wall_elapsed = now - self._first_wall
        lag = wall_elapsed - stream_elapsed

        timing = FrameTiming(
            pts=float(pts),
            ts=ts,
            arrival_wall=now,
            lag_seconds=lag,
            discontinuity=discontinuity,
            frame_index=self._frame_index,
        )
        self._frame_index += 1
        self._last_timing = timing
        return timing

    def reset(self, *, keep_frame_index: bool = True) -> None:
        """Start a new anchor. Called on reconnect.

        ``keep_frame_index`` defaults to true because the index is used to seed
        deterministic stubs and to label frames in logs; restarting it at zero on
        every reconnect makes two different frames share a label within one run.
        """
        self._clock = _make_clock(
            getattr(self._clock, "REORDER_TOLERANCE_S", None),
            getattr(self._clock, "FORWARD_GAP_S", None),
        )
        self._first_pts = None
        self._first_wall = None
        self._last_timing = None
        if not keep_frame_index:
            self._frame_index = 0


def _make_clock(
    reorder_tolerance_seconds: float | None, forward_gap_seconds: float | None
) -> StreamClock:
    """Build a ``StreamClock``, per-instance overrides applied as instance attrs.

    ``StreamClock`` declares its tolerances as class attributes and uses
    ``__slots__``, so a subclass is the only way to override them without mutating
    the shared contract's class — and mutating it would change the behaviour of
    every other stream in the process. The subclass is created per call, which is
    cheap and keeps the override strictly local.
    """
    if reorder_tolerance_seconds is None and forward_gap_seconds is None:
        return StreamClock()

    class _TunedClock(StreamClock):
        __slots__ = ()
        REORDER_TOLERANCE_S = (
            StreamClock.REORDER_TOLERANCE_S
            if reorder_tolerance_seconds is None
            else float(reorder_tolerance_seconds)
        )
        FORWARD_GAP_S = (
            StreamClock.FORWARD_GAP_S
            if forward_gap_seconds is None
            else float(forward_gap_seconds)
        )

    return _TunedClock()


@dataclass(slots=True)
class SamplerStats:
    """What the sampler did, for the report and for the operator.

    ``frames_shed`` is not a failure count. It is the number of frames the worker
    consciously declined in order to stay current, and it belongs in the sizing
    analysis: a claim that one box handles N cameras is only honest alongside the
    stride it ran at.
    """

    frames_offered: int = 0
    frames_accepted: int = 0
    frames_shed: int = 0
    stride_changes: int = 0
    max_stride_seen: int = 1
    max_lag_seconds: float = 0.0
    #: Frames declined during the replay burst, tracked separately because they are
    #: not evidence of overload and lumping them in overstates it.
    burst_shed: int = 0

    @property
    def acceptance_ratio(self) -> float:
        if self.frames_offered <= 0:
            return 0.0
        return self.frames_accepted / self.frames_offered

    def as_dict(self) -> dict[str, float | int]:
        return {
            "frames_offered": self.frames_offered,
            "frames_accepted": self.frames_accepted,
            "frames_shed": self.frames_shed,
            "burst_shed": self.burst_shed,
            "stride_changes": self.stride_changes,
            "max_stride_seen": self.max_stride_seen,
            "max_lag_seconds": round(self.max_lag_seconds, 3),
            "acceptance_ratio": round(self.acceptance_ratio, 4),
        }


class AdaptiveSampler:
    """Decides which decoded frames to process, from measured lag.

    The control law is deliberately dull: an EWMA of measured lag, two watermarks,
    and integer stride steps.

    * lag above ``max_lag_seconds`` — widen the stride by one
    * lag below ``target_lag_seconds`` — narrow it by one
    * between them — leave it alone

    The dead band between the watermarks is the point. A single-threshold
    controller oscillates: widening the stride reduces lag below the threshold,
    which immediately narrows it again, and the stride flaps every few frames while
    the tracker sees a jittering frame interval and starts breaking tracks. A PID
    would be a better controller and a worse decision — it needs tuning per
    deployment, and nobody is going to tune fifty edge boxes.

    Stride changes are integer steps rather than jumps to a computed value because
    the relationship between stride and lag is not linear: the cascade's cost per
    frame depends on what is *in* the frame, so a computed jump overshoots on a
    busy scene and undershoots on an empty one.
    """

    __slots__ = ("_config", "_stride", "_since_accept", "_lag_ewma", "_samples", "stats")

    def __init__(self, config: SamplingConfig | None = None) -> None:
        self._config = config or SamplingConfig()
        self._stride = max(1, self._config.min_stride)
        self._since_accept = 0
        self._lag_ewma = 0.0
        self._samples = 0
        self.stats = SamplerStats(max_stride_seen=self._stride)

    @property
    def stride(self) -> int:
        return self._stride

    @property
    def lag_seconds(self) -> float:
        """Smoothed measured lag. The number an operator should be shown."""
        return self._lag_ewma

    def should_process(self, timing: FrameTiming) -> bool:
        """Accept or shed one frame. Call for every decoded frame, in order.

        Updating the lag estimate happens here rather than after processing so that
        shed frames still inform the controller. A controller fed only by accepted
        frames is blind exactly when it is shedding most heavily.
        """
        self.stats.frames_offered += 1
        self._observe_lag(timing)

        if timing.discontinuity:
            # A loop point resets the stride. The old stride was chosen for the old
            # scene; the new one may be an empty car park where stride 1 is free.
            self._reset_stride()

        if not self._config.enabled:
            self.stats.frames_accepted += 1
            return True

        self._since_accept += 1
        if self._since_accept >= self._stride:
            self._since_accept = 0
            self.stats.frames_accepted += 1
            return True

        self.stats.frames_shed += 1
        if timing.is_replay_burst:
            self.stats.burst_shed += 1
        return False

    def _observe_lag(self, timing: FrameTiming) -> None:
        lag = timing.lag_seconds
        self.stats.max_lag_seconds = max(self.stats.max_lag_seconds, lag)

        if timing.is_replay_burst:
            # Negative lag is the GOP replay, not spare capacity. Feeding it to the
            # EWMA pulls the estimate below the low watermark and narrows the stride
            # just as the real-time backlog is about to arrive.
            return

        self._samples += 1
        alpha = self._config.ewma_alpha
        if self._samples == 1:
            self._lag_ewma = lag
        else:
            self._lag_ewma = alpha * lag + (1.0 - alpha) * self._lag_ewma

        if self._samples < self._config.min_samples:
            # Do not act on the first few samples. They come from the join window,
            # where stream time and wall time are unrelated.
            return

        if self._lag_ewma > self._config.max_lag_seconds:
            self._set_stride(self._stride + 1)
        elif self._lag_ewma < self._config.target_lag_seconds:
            self._set_stride(self._stride - 1)

    def _set_stride(self, value: int) -> None:
        clamped = max(self._config.min_stride, min(self._config.max_stride, int(value)))
        if clamped == self._stride:
            return
        self._stride = clamped
        self.stats.stride_changes += 1
        self.stats.max_stride_seen = max(self.stats.max_stride_seen, clamped)
        # The counter is not rescaled on a stride change: it counts frames since the
        # last acceptance, which stays meaningful under any stride. Resetting it
        # would make a widening stride skip a whole extra interval.

    def _reset_stride(self) -> None:
        self._stride = max(1, self._config.min_stride)
        self._since_accept = 0
        self._lag_ewma = 0.0
        self._samples = 0

    def note_processing_cost(self, seconds: float) -> None:
        """Optional hint: how long the cascade took on the last accepted frame.

        Advisory only. Lag remains the control signal, because per-frame cost says
        nothing about whether we are keeping up — a 40 ms cascade is comfortable at
        10 fps and hopeless at 30. Recorded so that a stride change can be
        explained after the fact.
        """
        if seconds > 0 and seconds > self._config.max_lag_seconds:
            # One frame costing more than the whole lag budget will cause shedding
            # on the next frame regardless; pre-empt it so the backlog never forms.
            self._set_stride(self._stride + 1)


@dataclass(slots=True)
class StallDetector:
    """Distinguishes "no frames" from "no motion". They need opposite responses.

    A stream that stops delivering frames must produce a ``stream_gap`` primitive,
    because a rule reasoning about a vehicle that never exited has to know the
    camera was blind for that interval. A stream that delivers frames of an empty
    road must produce nothing at all. Both look identical in an event log that only
    records detections, and conflating them is how an analytics platform reports
    "all clear" on a dead camera.
    """

    timeout_seconds: float = 10.0
    _last_frame_wall: float = field(default=0.0, repr=False)
    _stalled: bool = field(default=False, repr=False)

    def note_frame(self, wall: float) -> None:
        self._last_frame_wall = wall
        self._stalled = False

    def check(self, wall: float) -> float | None:
        """Return the stall duration the first time the timeout is exceeded.

        Fires once per stall, not once per poll, so a ten-minute outage is one
        event rather than six hundred. The duration on the eventual recovery event
        is the fact a rule actually needs.
        """
        if self._last_frame_wall <= 0.0 or self._stalled:
            return None
        elapsed = wall - self._last_frame_wall
        if elapsed >= self.timeout_seconds:
            self._stalled = True
            return elapsed
        return None

    @property
    def stalled(self) -> bool:
        return self._stalled
