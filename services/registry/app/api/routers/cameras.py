"""Camera registry read endpoints — Model 1's map and inventory.

Deliberately read-only here. Camera onboarding (create/update/bulk import) is a
larger surface — CSV/GeoJSON import, credential rotation, health probes — than
this push has time to build a full UI for; the console needs somewhere to
plot cameras and pick one to attach a watchlist/alert to *now*, so that is what
ships first. Every row returned is already scoped by ``camera_select``'s RLS
policy (004_rls.sql) before this handler ever sees it — there is no additional
filtering to get right or get wrong here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..deps import AuthContext, get_auth_context
from ..schemas import CameraOut
from ...db import transaction

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraOut])
async def list_cameras(
    auth: AuthContext = Depends(get_auth_context),
    status_filter: str | None = Query(default=None, alias="status"),
    camera_type: str | None = Query(default=None),
    bbox: str | None = Query(
        default=None,
        description="minLon,minLat,maxLon,maxLat — restrict to a map viewport",
    ),
) -> list[CameraOut]:
    clauses = ["true"]
    params: list[object] = []

    if status_filter:
        clauses.append("status = %s")
        params.append(status_filter)
    if camera_type:
        clauses.append("camera_type = %s")
        params.append(camera_type)
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except ValueError:
            min_lon = min_lat = max_lon = max_lat = None  # ignored below
        if min_lon is not None:
            clauses.append(
                "ST_Intersects(location::geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
            )
            params.extend([min_lon, min_lat, max_lon, max_lat])

    sql = f"""
        SELECT id, code, name, department_id, jurisdiction_id, camera_type, status,
               ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon,
               address, landmark
        FROM   app.camera
        WHERE  {' AND '.join(clauses)}
        ORDER BY code
        LIMIT 5000
    """
    with transaction(auth.security) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [CameraOut(**row) for row in rows]


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(camera_id: int, auth: AuthContext = Depends(get_auth_context)) -> CameraOut:
    from fastapi import HTTPException, status as http_status

    with transaction(auth.security) as cur:
        cur.execute(
            """
            SELECT id, code, name, department_id, jurisdiction_id, camera_type, status,
                   ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon,
                   address, landmark
            FROM   app.camera
            WHERE  id = %s
            """,
            (camera_id,),
        )
        row = cur.fetchone()
    if row is None:
        # RLS makes "exists but out of scope" and "does not exist" look
        # identical from here, which is the correct behaviour — a 404 leaks
        # nothing a 403 would about cameras outside the caller's jurisdiction.
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="camera not found")
    return CameraOut(**row)
