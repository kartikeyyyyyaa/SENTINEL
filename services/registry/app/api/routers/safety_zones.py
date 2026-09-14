"""Curated women's-safety risk zones — a police judgement call, not crime data.

See db/migrations/011_safety_zone.sql's module comment for why ``basis``
defaults to ``'curated'`` rather than presenting this as official statistics,
and docs/AUTOMATED_PROCESSING_DISCLOSURE.md for the same principle applied to
automated matching. Reading a zone requires ``alert.read`` — the same
permission that gates seeing alerts, because a risk zone is exactly that
sensitive; creating or removing one requires ``safety_zone.write``, held only
by state/district administrators (005_reference_data.sql's pattern for
structural, accountable decisions, not every operator's).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import AuthContext, require_permission
from ..schemas import SafetyZoneCreate, SafetyZoneOut
from ...core.audit import AuditAction, record
from ...db import transaction

router = APIRouter(prefix="/api/safety-zones", tags=["safety-zones"])

_ZONE_COLUMNS = """
    id, name, jurisdiction_id, jurisdiction_path,
    ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lon,
    radius_m, risk_level, basis, note, is_active, created_at, updated_at
"""


@router.get("", response_model=list[SafetyZoneOut])
async def list_safety_zones(
    auth: AuthContext = Depends(require_permission("alert.read")),
) -> list[SafetyZoneOut]:
    with transaction(auth.security) as cur:
        cur.execute(
            f"""
            SELECT {_ZONE_COLUMNS}
            FROM   app.safety_zone
            WHERE  is_active
            ORDER BY created_at DESC
            LIMIT 2000
            """
        )
        rows = cur.fetchall()
    return [SafetyZoneOut(**row) for row in rows]


@router.post("", response_model=SafetyZoneOut, status_code=status.HTTP_201_CREATED)
async def create_safety_zone(
    body: SafetyZoneCreate,
    auth: AuthContext = Depends(require_permission("safety_zone.write")),
) -> SafetyZoneOut:
    with transaction(auth.security) as cur:
        cur.execute(
            f"""
            INSERT INTO app.safety_zone
                (name, jurisdiction_id, center, radius_m, risk_level, note, created_by, updated_by)
            VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s, %s, %s, %s)
            RETURNING {_ZONE_COLUMNS}
            """,
            (
                body.name,
                body.jurisdiction_id,
                body.lon,
                body.lat,
                body.radius_m,
                body.risk_level,
                body.note,
                auth.security.user_id,
                auth.security.user_id,
            ),
        )
        row = cur.fetchone()
        record(
            cur,
            AuditAction.SAFETY_ZONE_CREATE,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
            resource_type="safety_zone",
            resource_id=row["id"],
            detail={"name": body.name, "risk_level": body.risk_level, "radius_m": body.radius_m},
        )
    return SafetyZoneOut(**row)


@router.delete("/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_safety_zone(
    zone_id: int, auth: AuthContext = Depends(require_permission("safety_zone.write"))
) -> None:
    with transaction(auth.security) as cur:
        cur.execute("DELETE FROM app.safety_zone WHERE id = %s RETURNING id, name", (zone_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="safety zone not found or not visible",
            )
        record(
            cur,
            AuditAction.SAFETY_ZONE_DELETE,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
            resource_type="safety_zone",
            resource_id=zone_id,
            detail={"name": row["name"]},
        )
