"""Camera health — the dashboard the schema already supported but nothing served.

``app.camera_health_check`` / ``app.camera_health_current`` (002_camera.sql) and
their RLS policies (004_rls.sql) have existed since the camera registry was
built; no probe writer and no API route ever used them. This is the read side.

Deliberately honest about what it does not know: a camera with no probe row
ever recorded returns ``last_checked_at: null``, not a fabricated uptime
percentage. See docs/DATA_HANDLING_AND_RETENTION.md's stance on not inventing
numbers this system cannot back up. Uptime-over-time is a real next step once
something is actually writing probes (``camera.health_write``, held today by
``field_technician``, ``integrator`` and ``edge_worker``) — this endpoint
reports what has been recorded, nothing more.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import AuthContext, get_auth_context
from ..schemas import CameraHealthOut
from ...db import transaction

router = APIRouter(prefix="/api/camera-health", tags=["health"])


@router.get("", response_model=list[CameraHealthOut])
async def list_camera_health(auth: AuthContext = Depends(get_auth_context)) -> list[CameraHealthOut]:
    # No require_permission: exactly like GET /api/cameras (cameras.py), the
    # gate is RLS, not an application-layer check — camera_select decides which
    # rows exist here at all, and health_select composes off that same
    # visibility (004_rls.sql), so there is nothing extra to enforce twice.
    with transaction(auth.security) as cur:
        cur.execute(
            """
            SELECT c.id AS camera_id, c.code, c.name, c.department_id, c.jurisdiction_id,
                   c.jurisdiction_path, c.camera_type, c.status,
                   h.checked_at AS last_checked_at, h.probe AS last_probe,
                   h.is_live AS last_is_live, h.latency_ms AS last_latency_ms,
                   h.error_code AS last_error_code
            FROM   app.camera c
            LEFT   JOIN app.camera_health_current h ON h.camera_id = c.id
            ORDER BY c.code
            LIMIT 5000
            """
        )
        rows = cur.fetchall()
    return [CameraHealthOut(**row) for row in rows]
