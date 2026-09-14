"""In-process pub/sub for Server-Sent Events, thread-safe by construction.

**Why stdlib ``queue.Queue`` instead of an asyncio queue.** Every ingestion
endpoint that publishes here (``POST /api/analytics/events``,
``POST /api/analytics/alerts``) is a plain synchronous FastAPI handler using the
synchronous ``psycopg`` connection in ``db.py`` — mixing that with an
asyncio-native broadcaster would mean bouncing every publish across an event
loop from a worker thread. A stdlib queue needs none of that: ``put``/``get``
are already thread-safe, so a sync ingestion handler and a streaming SSE
response (also served as a plain synchronous generator — see
``api/routers/events.py``) can share one without touching asyncio at all.

**Single process only.** This is exactly the same scale admission
``AlertHttpTransport``'s docstring and ``federation/base.py`` make elsewhere in
this codebase: correct for one registry instance, and the first thing to
replace with a real pub/sub (Redis, NATS) the day the API runs behind more than
one process. Nothing downstream of ``Broadcaster`` needs to change when that
happens — only what sits behind ``publish``/``subscribe``.

**Bounded per subscriber, drop-oldest.** A console tab left open overnight must
not turn into unbounded memory growth on the server; and when it is behind,
what an operator needs on reconnect is *now*, not an hour-old backlog — the
same "oldest event dropped, not queued forever" argument
``services/analytics/sink.py`` makes for the edge-side buffer.
"""
from __future__ import annotations

import queue
import threading
from typing import Any


class Broadcaster:
    def __init__(self, maxsize: int = 200) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except queue.Full:
                # Drop the oldest queued item to make room, then retry once. A
                # subscriber that is still full after that is being served
                # slower than it can drain regardless; losing one more message
                # to it is the same policy applied consistently.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except queue.Empty:
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


#: One broadcaster for the whole process, created at import time rather than
#: per-request so every ingestion handler and every open SSE connection shares
#: the same set of subscribers. See ``main.py``'s lifespan for where this is
#: also exposed on ``app.state`` for testability.
broadcaster = Broadcaster()
