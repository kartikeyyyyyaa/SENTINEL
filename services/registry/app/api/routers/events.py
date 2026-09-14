"""The console's live feed: one SSE stream, fed by ``core.broadcast.Broadcaster``.

**Why the access token is a query parameter here and nowhere else.** The
browser's ``EventSource`` API cannot set an ``Authorization`` header — it is
the one place in this API where that matters, because it is the one endpoint a
browser opens without going through ``fetch``/``XMLHttpRequest`` first. A
short-lived access token in a URL is a narrower exposure than it looks:
``web/nginx.conf`` already terminates the console over the loopback interface
in development and TLS in front of it in any real deployment, and this token
still expires in ``ACCESS_TOKEN_TTL_MINUTES`` like any other — it is not a
new, longer-lived credential shape.

**Why plain ``StreamingResponse`` and not an async generator over an asyncio
queue.** ``Broadcaster`` is a stdlib ``queue.Queue`` (see its module docstring
for why); ``q.get(timeout=...)`` blocks the worker thread FastAPI already runs
synchronous generators on, which is exactly where a synchronous ingestion
endpoint (``routers/analytics.py``, ``routers/alerts.py``) also runs. Nothing
here crosses an event loop.
"""
from __future__ import annotations

import json
import queue
import time

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..deps import AuthContext
from ...core.broadcast import broadcaster
from ...core.tokens import TokenError, verify_access_token
from ...config import get_settings
from ...db import load_security_context, transaction

router = APIRouter(prefix="/api/events", tags=["events"])

_KEEPALIVE_SECONDS = 15


def _resolve_stream_auth(token: str) -> AuthContext:
    settings = get_settings()
    try:
        claims = verify_access_token(token, secret=settings.jwt_secret)
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    with transaction(None) as cur:
        ctx = load_security_context(cur, claims.user_id)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account is inactive")
    if not ctx.has("alert.read"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing permission: alert.read")
    return AuthContext(security=ctx, client_ip=None, user_agent=None, auth_method="jwt")


@router.get("/stream")
async def event_stream(request: Request, token: str = Query(...)) -> StreamingResponse:
    _resolve_stream_auth(token)  # raises 401/403 before the stream opens

    def generate():
        q = broadcaster.subscribe()
        try:
            # A cold open should feel alive immediately, not after the first
            # real alert — an operator staring at a blank panel cannot tell
            # "nothing has happened yet" from "this is broken".
            yield "event: ready\ndata: {}\n\n"
            last_beat = time.monotonic()
            while True:
                # Starlette's Request.is_disconnected() is async; this
                # generator is deliberately sync (see module docstring), so a
                # client closing the socket is instead detected the ordinary
                # way generators detect it: the next `yield` raises, and
                # StreamingResponse turns that into cleanup via this
                # generator's `finally` below.
                try:
                    message = q.get(timeout=1.0)
                    yield f"data: {json.dumps(message, default=str)}\n\n"
                    last_beat = time.monotonic()
                except queue.Empty:
                    if time.monotonic() - last_beat >= _KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last_beat = time.monotonic()
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: do not buffer an SSE response
            "Connection": "keep-alive",
        },
    )
