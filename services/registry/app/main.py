"""FastAPI application.

Kept thin on purpose: wiring, middleware and lifecycle only. Routers live in
``app/api/`` and the logic they call lives in ``app/core/`` and ``app/services/``,
so none of the interesting behaviour is trapped inside a request handler where it
cannot be tested.

Right now this exposes health and readiness only. Endpoints arrive with the
registry routers; this file exists so the stack can be brought up and verified end
to end before there is anything to verify.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routers import alerts, analytics, auth, cameras, events, health, reports, safety_zones, watchlist
from .config import get_settings
from .core.broadcast import broadcaster
from .db import close_pool, init_pool, transaction

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    # Opening the pool at startup means a bad DATABASE_URL fails here, loudly, on
    # boot — rather than on the first request from a real user.
    init_pool()
    # Exposed on app.state (not just importable directly) so a test can swap in
    # a fresh Broadcaster per test case instead of sharing process-wide state.
    app.state.broadcaster = broadcaster
    log.info("sentinel-registry started in %s mode", settings.environment)
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="Sentinel Registry",
    version="0.1.0",
    description=(
        "Camera registry, GIS foundation and federation API for the Gujarat "
        "Sentinel platform."
    ),
    lifespan=lifespan,
    # No docs in production. An interactive schema browser is a gift to anyone
    # mapping the attack surface, and operators do not need it.
    docs_url=None if get_settings().is_production else "/docs",
    redoc_url=None,
    openapi_url=None if get_settings().is_production else "/openapi.json",
)


settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,  # auth is a bearer token, never a cookie — no credentials to carry
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


app.include_router(auth.router)
app.include_router(cameras.router)
app.include_router(watchlist.router)
app.include_router(alerts.router)
app.include_router(analytics.router)
app.include_router(events.router)
app.include_router(health.router)
app.include_router(reports.router)
app.include_router(safety_zones.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Defence-in-depth headers.

    nginx sets these for the console's own pages; setting them here too means the
    API is protected even when reached directly, without nginx in front of it —
    which is exactly how it will be reached during integration testing, and how a
    misconfiguration would otherwise go unnoticed.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    # API responses are JSON and never a document context, so the tightest
    # possible policy is also a correct one.
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.get("/api/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Liveness. Deliberately does not touch the database.

    A liveness probe that fails when the database is down causes the orchestrator
    to restart a perfectly healthy API process, which does nothing to fix the
    database and adds an outage on top of it. Database state belongs in readiness.
    """
    return {"status": "ok", "service": "sentinel-registry"}


@app.get("/api/ready", tags=["ops"])
async def ready() -> JSONResponse:
    """Readiness. Confirms the database answers and reports schema version.

    Unauthenticated, so it leaks only the migration count — enough to diagnose a
    half-deployed stack, not enough to be useful to anyone else.
    """
    try:
        with transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM app.schema_migration")
            applied = cur.fetchone()["n"]
            cur.execute("SELECT postgis_version() AS v")
            postgis = cur.fetchone()["v"]
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness check failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not-ready", "reason": "database unavailable"},
        )

    return JSONResponse(
        content={
            "status": "ready",
            "migrations_applied": applied,
            "postgis": postgis,
        }
    )
