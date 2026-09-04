"""Event egress. Metadata leaves the edge; video does not.

**The rule this module enforces.** Only primitive metadata, small thumbnails and
short alert clips ever cross the edge boundary. Continuous video never does, under
any configuration — there is no flag for it, because the bandwidth argument for the
whole architecture rests on it. Eighty thousand cameras at even 2 Mbit/s is 160
Gbit/s of backhaul; the same fleet emitting a few hundred bytes of metadata per
event is a rounding error on a single link. The size ceiling below is a structural
guard rather than a policy note: an event whose payload exceeds it is dropped at
this boundary, because the only way a payload gets that large is somebody
attaching pixels they should not.

**Bounded, always.** The buffer has a fixed capacity and drops the *oldest* event
when full. Two deliberate choices there. Bounded, because an edge box that runs out
of memory because the registry was unreachable for an hour has turned a network
problem into an outage. Oldest-first, because when the link comes back the operator
needs to know what is happening now — a queue that preserved the oldest events would
spend the recovery window delivering an hour-old backlog while the current incident
went unreported.

**Drops are logged, with a count.** Silent loss is indistinguishable from nothing
happening, and "the analytics stopped reporting" has to be diagnosable after the
fact. The log is rate-limited, because a sustained outage would otherwise fill the
disk with the notice that we are dropping events.

Transports are stdlib only. ``httpx`` and ``requests`` are both perfectly good and
neither is worth being a hard dependency of a process that has to start on an edge
box with whatever wheels happened to install. See ``config.load_cameras_from_registry``
for the same choice on the read side.

This module knows ``services.common.events.PrimitiveEvent`` and nothing else — no
``Sighting``, because sightings are the higher-volume stream and a district-scale
deployment does not put per-frame per-vehicle records over the same bounded buffer
as its incident-relevant primitives. A future sightings sink is a second instance of
this module's machinery, not a change to it.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from typing import Any, Protocol

from services.common.alerts import Alert, alert_to_dict
from services.common.events import PrimitiveEvent

from .config import SinkConfig

log = logging.getLogger(__name__)

# A primitive event is a few hundred bytes; a thumbnail pushes it to a few
# kilobytes. Anything an order of magnitude past that is pixels that should have
# stayed on the edge, and it is refused here rather than shipped. See the module
# docstring.
MAX_EVENT_BYTES = 256 * 1024

# HTTP statuses worth retrying. Everything else in 4xx is a permanent rejection —
# a malformed body or a revoked token will fail identically on every retry, and
# retrying it forever means the buffer never drains and every later event is
# dropped behind it.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def event_to_dict(event: PrimitiveEvent) -> dict[str, Any]:
    """``PrimitiveEvent`` as a JSON-safe mapping.

    A free function rather than a method on ``PrimitiveEvent`` because that class
    is the shared contract with the state core (see ``services/common/events.py``)
    and is deliberately kept free of every transport's opinion about wire format.
    Sightings will need the same treatment from whatever sink eventually ships
    them, and it belongs there too, not on the dataclass.
    """
    payload = dict(event.payload)
    return {
        "kind": event.kind,
        "camera_id": event.camera_id,
        "ts": event.ts.isoformat(),
        "track_id": event.track_id,
        "payload": payload,
    }


def event_to_json(event: PrimitiveEvent) -> str:
    return json.dumps(event_to_dict(event), separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


class Transport(Protocol):
    """Delivers a batch. True means delivered; False means retry later.

    Raising is also acceptable and is treated as a retryable failure. The
    distinction that matters is a *permanent* rejection, which a transport signals
    by returning True after logging — the batch is gone and retrying it would block
    the queue behind a body the server will never accept.
    """

    name: str

    def send(self, batch: list[PrimitiveEvent]) -> bool: ...

    def close(self) -> None: ...


@dataclass
class SinkStats:
    accepted: int = 0
    delivered: int = 0
    dropped_backpressure: int = 0
    dropped_oversize: int = 0
    dropped_permanent: int = 0
    send_failures: int = 0
    batches: int = 0
    buffered: int = 0
    high_water: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "delivered": self.delivered,
            "dropped_backpressure": self.dropped_backpressure,
            "dropped_oversize": self.dropped_oversize,
            "dropped_permanent": self.dropped_permanent,
            "send_failures": self.send_failures,
            "batches": self.batches,
            "buffered": self.buffered,
            "high_water": self.high_water,
        }


class JsonlTransport:
    """One JSON object per line, to stdout or a file.

    The default when no registry URL is configured, and what the dry run and a
    rehearsal harness consume. Line-delimited JSON because it is the format that
    survives being piped, truncated and tailed — a run killed halfway through
    leaves a file that is still parseable up to the last complete line, which a
    single JSON array would not.
    """

    name = "jsonl"

    def __init__(self, path: str = "") -> None:
        self.path = path
        self._handle = open(path, "a", encoding="utf-8") if path else sys.stdout
        self._owned = bool(path)

    def send(self, batch: list[PrimitiveEvent]) -> bool:
        for event in batch:
            self._handle.write(event_to_json(event) + "\n")
        self._handle.flush()
        return True

    def close(self) -> None:
        if self._owned:
            self._handle.close()


class HttpTransport:
    """POSTs a JSON batch to the registry's event endpoint.

    urllib rather than a client library: see the module docstring. The endpoint is
    expected to be idempotent per event — ``camera_id`` + ``track_id`` + ``kind`` +
    ``ts`` identifies one — because a timeout that actually succeeded server-side is
    indistinguishable here from one that did not, and re-sending is the only safe
    response.
    """

    name = "http"

    def __init__(self, config: SinkConfig) -> None:
        self.config = config
        self.url = config.registry_url.rstrip("/")

    def send(self, batch: list[PrimitiveEvent]) -> bool:
        body = json.dumps(
            {"events": [event_to_dict(e) for e in batch]}, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.config.api_token:
            request.add_header("Authorization", f"Bearer {self.config.api_token}")
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.post_timeout_seconds
            ) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_STATUSES:
                log.warning("event POST failed with %s; will retry", exc.code)
                return False
            # Permanent. Reporting success discards the batch on purpose: a body the
            # registry will never accept must not block everything queued behind it.
            log.error(
                "event POST rejected permanently with %s; discarding %d events",
                exc.code, len(batch),
            )
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("event POST transport error: %s; will retry", exc)
            return False

    def close(self) -> None:
        return None


class MemoryTransport:
    """Collects batches in a list. Tests and the self-test harness, nothing else.

    ``fail`` makes it refuse deliveries, which is how the backpressure and
    drop-oldest behaviour is exercised without a network.
    """

    name = "memory"

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[PrimitiveEvent]] = []
        self.fail = fail

    @property
    def events(self) -> list[PrimitiveEvent]:
        return [e for batch in self.batches for e in batch]

    def send(self, batch: list[PrimitiveEvent]) -> bool:
        if self.fail:
            return False
        self.batches.append(list(batch))
        return True

    def close(self) -> None:
        return None


class BufferedEventSink:
    """Bounded buffer, batched delivery, retry with backoff, drop oldest when full.

    Thread-safe because one worker process runs several camera threads and they all
    emit into one sink. A lock around a deque is the right amount of machinery here:
    the critical sections are appends and slices, contention is negligible next to
    the cost of a frame, and a queue plus a dedicated drain thread would add a
    failure mode (the drain thread dying quietly) for no measurable gain.
    """

    def __init__(self, transport: Transport, config: SinkConfig) -> None:
        self.transport = transport
        self.config = config
        self.stats = SinkStats()
        self._buffer: deque[PrimitiveEvent] = deque()
        self._lock = threading.Lock()
        self._retry_not_before = 0.0
        self._retry_delay = 0.0
        self._last_flush = 0.0
        self._last_drop_log = 0.0
        self._drops_since_log = 0

    @property
    def buffered(self) -> int:
        with self._lock:
            return len(self._buffer)

    def emit(self, event: PrimitiveEvent) -> bool:
        """Queue one event. Returns False if it was refused or displaced something."""
        payload = event_to_json(event)
        if len(payload) > MAX_EVENT_BYTES:
            # Structural guard, not a policy note. See the module docstring.
            self.stats.dropped_oversize += 1
            log.error(
                "refusing a %d byte event from camera %s: only metadata, thumbnails "
                "and alert clips leave the edge",
                len(payload), event.camera_id,
            )
            return False
        with self._lock:
            self.stats.accepted += 1
            capacity = max(1, self.config.buffer_capacity)
            displaced = False
            while len(self._buffer) >= capacity:
                self._buffer.popleft()
                self.stats.dropped_backpressure += 1
                self._drops_since_log += 1
                displaced = True
            self._buffer.append(event)
            self.stats.buffered = len(self._buffer)
            self.stats.high_water = max(self.stats.high_water, len(self._buffer))
        if displaced:
            self._log_drops()
        return not displaced

    def emit_many(self, events: list[PrimitiveEvent]) -> None:
        for event in events:
            self.emit(event)

    def _log_drops(self) -> None:
        """Rate-limited drop notice.

        Logged at most once every five seconds. During a sustained outage the
        alternative is a log line per event, which fills the disk with the notice
        that we are running out of room.
        """
        now = time.monotonic()
        if now - self._last_drop_log < 5.0:
            return
        self._last_drop_log = now
        log.warning(
            "sink at capacity (%d): dropped %d oldest events since last notice, "
            "%d total. Newest events are kept on purpose.",
            self.config.buffer_capacity, self._drops_since_log,
            self.stats.dropped_backpressure,
        )
        self._drops_since_log = 0

    def flush(self, force: bool = False) -> int:
        """Deliver whatever is queued. Returns the number of events delivered.

        Honours a retry backoff, so a dead registry is retried on a widening
        interval rather than once per frame. ``force`` ignores the batch-size
        threshold but not the backoff — shutdown should not turn into a tight
        retry loop against an endpoint that is already known to be down.
        """
        now = time.monotonic()
        if now < self._retry_not_before:
            return 0
        delivered = 0
        while True:
            with self._lock:
                if not self._buffer:
                    break
                if not force and len(self._buffer) < self.config.batch_size:
                    if now - self._last_flush < self.config.flush_interval_seconds:
                        break
                batch = [
                    self._buffer.popleft()
                    for _ in range(min(self.config.batch_size, len(self._buffer)))
                ]
            self._last_flush = now
            ok = False
            try:
                ok = bool(self.transport.send(batch))
            except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
                log.warning("sink transport %s raised: %s", self.transport.name, exc)
            self.stats.batches += 1
            if ok:
                delivered += len(batch)
                self.stats.delivered += len(batch)
                self._retry_delay = 0.0
                self._retry_not_before = 0.0
                continue
            self.stats.send_failures += 1
            with self._lock:
                # Back onto the front, in order, so delivery stays FIFO. If the
                # buffer filled while we were away, the returned events are the
                # oldest and are the ones that get displaced by the next emit —
                # which is the intended policy, applied consistently.
                self._buffer.extendleft(reversed(batch))
                overflow = len(self._buffer) - max(1, self.config.buffer_capacity)
                for _ in range(max(0, overflow)):
                    self._buffer.popleft()
                    self.stats.dropped_backpressure += 1
                    self._drops_since_log += 1
                self.stats.buffered = len(self._buffer)
            if overflow > 0:
                self._log_drops()
            self._retry_delay = min(
                self.config.flush_interval_seconds * 8,
                max(self.config.flush_interval_seconds, self._retry_delay * 2 or 1.0),
            )
            self._retry_not_before = time.monotonic() + self._retry_delay
            break
        with self._lock:
            self.stats.buffered = len(self._buffer)
        return delivered

    def close(self) -> None:
        """One last flush, then release the transport.

        Bounded to a few attempts. A shutdown that blocks indefinitely trying to
        deliver a backlog to a dead endpoint is a worker that cannot be restarted,
        and during a live rehearsal that is worse than losing the backlog.
        """
        for _ in range(self.config.max_post_attempts):
            self._retry_not_before = 0.0
            if self.flush(force=True) == 0:
                break
        try:
            self.transport.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("transport close failed: %s", exc)
        if self.buffered:
            log.warning("sink closing with %d undelivered events", self.buffered)


def build_transport(config: SinkConfig) -> Transport:
    """HTTP when an events URL is set, JSONL otherwise.

    There is no default URL on purpose: a worker that posts somewhere because a
    default was left in a config file is worse than one that prints to a terminal
    and makes you notice.
    """
    if config.registry_url:
        return HttpTransport(config)
    return JsonlTransport(config.jsonl_path)


def build_sink(config: SinkConfig) -> BufferedEventSink:
    return BufferedEventSink(build_transport(config), config)


def make_thumbnail(image: Any, max_width: int = 320, quality: int = 70) -> str | None:
    """Small base64 JPEG for an event, or None if encoding is unavailable.

    Thumbnails are attached to alert-worthy events only, never to every primitive.
    A 6 KB thumbnail on every track update at fifty cameras is several megabits per
    second of egress, which would quietly reintroduce the video-backhaul cost this
    architecture exists to avoid.

    cv2 is imported lazily; without it the worker still emits every primitive, just
    without pictures.
    """
    if image is None:
        return None
    try:
        import cv2  # noqa: PLC0415 - optional, see docstring

        h, w = image.shape[:2]
        if w > max_width:
            scale = max_width / float(w)
            image = cv2.resize(
                image, (max_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA
            )
        ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return base64.b64encode(buffer.tobytes()).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - a missing thumbnail is not a failure
        log.debug("thumbnail encode failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Alert egress — a second, much lower-volume instance of the same machinery
# ---------------------------------------------------------------------------
# See the module docstring's closing line: "A future sightings sink is a second
# instance of this module's machinery, not a change to it." Alerts are that
# second instance now. They are not folded into ``BufferedEventSink`` because
# their delivery semantics are genuinely different — a few per minute at most,
# each one worth paging on, versus hundreds of primitives per second where
# losing the oldest under backpressure is the right trade. A tiny, separate,
# bounded queue with the same retry-with-backoff posture is clearer than one
# class trying to be right for both volumes at once.


def alert_to_json(alert: Alert) -> str:
    return json.dumps(alert_to_dict(alert), separators=(",", ":"), default=_json_default)


class AlertJsonlTransport:
    """Same reasoning as ``JsonlTransport``, for alerts. The default sink when no
    registry alerts endpoint is configured."""

    name = "alerts-jsonl"

    def __init__(self, path: str = "") -> None:
        self.path = path
        self._handle = open(path, "a", encoding="utf-8") if path else sys.stdout
        self._owned = bool(path)

    def send(self, batch: list[Alert]) -> bool:
        for alert in batch:
            self._handle.write(alert_to_json(alert) + "\n")
        self._handle.flush()
        return True

    def close(self) -> None:
        if self._owned:
            self._handle.close()


class AlertHttpTransport:
    """POSTs a JSON batch of alerts to the registry's alert-ingestion endpoint.

    Idempotent per ``Alert.alert_key`` on the server side, the same contract
    ``HttpTransport`` documents for primitive events — a timed-out POST that
    actually landed must be safely re-sendable.
    """

    name = "alerts-http"

    def __init__(self, config: SinkConfig) -> None:
        self.config = config
        self.url = config.alerts_url.rstrip("/")

    def send(self, batch: list[Alert]) -> bool:
        body = json.dumps(
            {"alerts": [alert_to_dict(a) for a in batch]}, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.config.api_token:
            request.add_header("Authorization", f"Bearer {self.config.api_token}")
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.post_timeout_seconds
            ) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_STATUSES:
                log.warning("alert POST failed with %s; will retry", exc.code)
                return False
            log.error(
                "alert POST rejected permanently with %s; discarding %d alert(s)",
                exc.code, len(batch),
            )
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("alert POST transport error: %s; will retry", exc)
            return False

    def close(self) -> None:
        return None


class MemoryAlertTransport:
    """Collects alert batches in a list. Tests and the self-test harness."""

    name = "alerts-memory"

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[Alert]] = []
        self.fail = fail

    @property
    def alerts(self) -> list[Alert]:
        return [a for batch in self.batches for a in batch]

    def send(self, batch: list[Alert]) -> bool:
        if self.fail:
            return False
        self.batches.append(list(batch))
        return True

    def close(self) -> None:
        return None


class AlertSink:
    """Bounded, retried delivery for ``Alert`` objects.

    Deliberately not batched by size the way primitive events are: an alert
    worth generating is worth sending promptly, and at the volumes an alert
    stream actually runs at (see the section docstring above) batching buys
    nothing but latency. ``flush`` is still driven from the same worker loop as
    the primitive sink, so no new thread is needed.
    """

    def __init__(self, transport: Any, config: SinkConfig, capacity: int = 500) -> None:
        self.transport = transport
        self.config = config
        self.capacity = capacity
        self._buffer: deque[Alert] = deque()
        self._lock = threading.Lock()
        self._retry_not_before = 0.0
        self._retry_delay = 0.0
        self.delivered = 0
        self.dropped = 0

    def emit(self, alert: Alert) -> None:
        with self._lock:
            while len(self._buffer) >= self.capacity:
                self._buffer.popleft()
                self.dropped += 1
            self._buffer.append(alert)

    def emit_many(self, alerts: list[Alert]) -> None:
        for alert in alerts:
            self.emit(alert)

    def flush(self, force: bool = False) -> int:
        now = time.monotonic()
        if now < self._retry_not_before:
            return 0
        with self._lock:
            if not self._buffer:
                return 0
            batch = list(self._buffer)
            self._buffer.clear()
        try:
            ok = bool(self.transport.send(batch))
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            log.warning("alert sink transport %s raised: %s", self.transport.name, exc)
            ok = False
        if ok:
            self.delivered += len(batch)
            self._retry_delay = 0.0
            self._retry_not_before = 0.0
            return len(batch)
        with self._lock:
            self._buffer.extendleft(reversed(batch))
            overflow = len(self._buffer) - self.capacity
            for _ in range(max(0, overflow)):
                self._buffer.popleft()
                self.dropped += 1
        self._retry_delay = min(30.0, max(2.0, self._retry_delay * 2 or 2.0))
        self._retry_not_before = time.monotonic() + self._retry_delay
        return 0

    def close(self) -> None:
        for _ in range(self.config.max_post_attempts):
            self._retry_not_before = 0.0
            if self.flush(force=True) == 0 and not self._buffer:
                break
        try:
            self.transport.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("alert transport close failed: %s", exc)
        if self._buffer:
            log.warning("alert sink closing with %d undelivered alert(s)", len(self._buffer))


def build_alert_transport(config: SinkConfig) -> Any:
    if config.alerts_url:
        return AlertHttpTransport(config)
    return AlertJsonlTransport(config.alerts_jsonl_path)


def build_alert_sink(config: SinkConfig) -> AlertSink:
    return AlertSink(build_alert_transport(config), config)
