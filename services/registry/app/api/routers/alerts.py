"""Alerts: the operator queue, and the one unauthenticated write in this API.

``POST /api/sos`` takes no credential on purpose. A panic signal that requires
a police login first has already failed at the one moment it matters — a
citizen at a kiosk, or the mobile app, or an operator relaying a phone call, is
not going to authenticate their way through a crisis. ``app.women_safety_sos``'s
``sos_insert`` RLS policy (``WITH CHECK (true)``) says the same thing at the
database layer: this is the one write in the whole schema that is intentionally
open. Everything downstream of it — reading it back, acknowledging it, closing
it — goes through the normal permissioned path like any other alert.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..deps import AuthContext, require_permission
from ..schemas import AlertClose, AlertOut, SosCreate, SosOut
from ...core.audit import AuditAction, record, record_out_of_band
from ...core.broadcast import broadcaster
from ...db import transaction

router = APIRouter(tags=["alerts"])

_ALERT_COLUMNS = """
    id, kind, severity, status, camera_id, jurisdiction_path, alert_key,
    track_ids, source_event_kinds, summary, detail, case_reference,
    opened_at, acknowledged_at, closed_at, close_reason
"""


def _row_to_out(row: dict) -> AlertOut:
    return AlertOut(**row)


@router.get("/api/alerts", response_model=list[AlertOut])
async def list_alerts(
    auth: AuthContext = Depends(require_permission("alert.read")),
    status_filter: str | None = Query(default=None, alias="status", description="default: open + acknowledged"),
    severity: str | None = None,
    kind: str | None = None,
    camera_id: int | None = None,
    limit: int = Query(default=200, le=1000),
) -> list[AlertOut]:
    clauses = ["true"]
    params: list[object] = []

    if status_filter:
        clauses.append("status = %s")
        params.append(status_filter)
    else:
        clauses.append("status IN ('open', 'acknowledged')")

    if severity:
        clauses.append("severity = %s")
        params.append(severity)
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if camera_id:
        clauses.append("camera_id = %s")
        params.append(camera_id)

    params.append(limit)
    with transaction(auth.security) as cur:
        cur.execute(
            f"""
            SELECT {_ALERT_COLUMNS} FROM app.alert
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE severity WHEN 'critical' THEN 0 WHEN 'urgent' THEN 1
                              WHEN 'advisory' THEN 2 ELSE 3 END,
                opened_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    return [_row_to_out(r) for r in rows]


@router.post("/api/alerts/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: int, auth: AuthContext = Depends(require_permission("alert.acknowledge"))
) -> AlertOut:
    with transaction(auth.security) as cur:
        cur.execute(
            f"""
            UPDATE app.alert
            SET    status = 'acknowledged', acknowledged_at = now(), acknowledged_by = %s
            WHERE  id = %s AND status = 'open'
            RETURNING {_ALERT_COLUMNS}
            """,
            (auth.security.user_id, alert_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="alert not found, not visible, or not in 'open' status",
            )
        record(
            cur,
            AuditAction.ALERT_ACKNOWLEDGE,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
            resource_type="alert",
            resource_id=alert_id,
            case_reference=row["case_reference"],
        )
    broadcaster.publish({"type": "alert.updated", "alert": row})
    return _row_to_out(row)


@router.post("/api/alerts/{alert_id}/close", response_model=AlertOut)
async def close_alert(
    alert_id: int, body: AlertClose, auth: AuthContext = Depends(require_permission("alert.close"))
) -> AlertOut:
    new_status = "false_positive" if body.false_positive else "closed"
    with transaction(auth.security) as cur:
        cur.execute(
            f"""
            UPDATE app.alert
            SET    status = %s, closed_at = now(), closed_by = %s, close_reason = %s
            WHERE  id = %s AND status IN ('open', 'acknowledged')
            RETURNING {_ALERT_COLUMNS}
            """,
            (new_status, auth.security.user_id, body.reason, alert_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="alert not found, not visible, or already closed",
            )
        record(
            cur,
            AuditAction.ALERT_CLOSE,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
            resource_type="alert",
            resource_id=alert_id,
            case_reference=row["case_reference"],
            detail={"reason": body.reason, "false_positive": body.false_positive},
        )
    broadcaster.publish({"type": "alert.updated", "alert": row})
    return _row_to_out(row)


@router.post("/api/sos", response_model=SosOut, status_code=status.HTTP_201_CREATED)
async def report_sos(body: SosCreate, request: Request) -> SosOut:
    """Unauthenticated by design — see module docstring.

    **Nothing here uses RETURNING, and the insert order is deliberate.** An
    anonymous session has no ``app.user_id`` at all, so it holds *no*
    permission — not ``alert.read``, not ``alert.acknowledge``. RLS applies a
    row's SELECT policy to whatever an INSERT's RETURNING clause asks for, on
    top of the INSERT policy's own WITH CHECK, so any RETURNING here would
    fail even on a row the INSERT itself was allowed to create. Every value
    this handler needs back is therefore either generated in Python before the
    insert (the alert key, the timestamp) or read via ``lastval()`` — a
    session-local read of the sequence this same session just advanced, which
    needs no row visibility at all.

    The alert is inserted *before* the SOS row, carrying a freshly generated
    key rather than one derived from the SOS row's id, specifically so the SOS
    insert can set ``alert_id`` in its own INSERT statement instead of a
    follow-up UPDATE — ``women_safety_sos``'s ``sos_update`` policy gates on
    ``alert.acknowledge``, which an anonymous reporter correctly can never
    hold, the same reasoning ``009_sos_alert_insert.sql`` documents for
    ``alert_insert`` itself.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from ...db import get_pool

    ip = request.client.host if request.client else None
    point = None
    if body.lat is not None and body.lon is not None:
        point = f"SRID=4326;POINT({body.lon} {body.lat})"

    reported_at = datetime.now(timezone.utc)
    alert_key = f"sos:{uuid4().hex}"
    summary = "Women's safety SOS" + (f" via {body.channel}" if body.channel else "")

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.alert (
                    kind, severity, camera_id, jurisdiction_id, alert_key, summary, detail, opened_at
                ) VALUES ('sos', 'critical', %s, %s, %s, %s, %s, %s)
                """,
                (
                    body.camera_id,
                    body.jurisdiction_id,
                    alert_key,
                    summary,
                    json.dumps({"channel": body.channel, "notes": body.notes}),
                    reported_at,
                ),
            )
            cur.execute("SELECT lastval()")
            alert_id = cur.fetchone()["lastval"]

            cur.execute(
                """
                INSERT INTO app.women_safety_sos (
                    channel, camera_id, jurisdiction_id, location, notes, alert_id, reported_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (body.channel, body.camera_id, body.jurisdiction_id, point, body.notes, alert_id, reported_at),
            )
            cur.execute("SELECT lastval()")
            sos_id = cur.fetchone()["lastval"]
        conn.commit()

    record_out_of_band(
        AuditAction.SOS_CREATE,
        actor_ip=ip,
        resource_type="women_safety_sos",
        resource_id=sos_id,
        detail={"channel": body.channel},
    )
    broadcaster.publish(
        {
            "type": "alert.created",
            "alert": {
                "id": alert_id,
                "kind": "sos",
                "severity": "critical",
                "summary": summary,
                "camera_id": body.camera_id,
            },
        }
    )
    return SosOut(
        id=sos_id,
        channel=body.channel,
        camera_id=body.camera_id,
        reported_at=reported_at,
        alert_id=alert_id,
        resolved_at=None,
    )


@router.get("/api/sos/{sos_id}", response_model=SosOut)
async def get_sos(sos_id: int, auth: AuthContext = Depends(require_permission("alert.read"))) -> SosOut:
    with transaction(auth.security) as cur:
        cur.execute(
            "SELECT id, channel, camera_id, reported_at, alert_id, resolved_at "
            "FROM app.women_safety_sos WHERE id = %s",
            (sos_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SOS report not found")
    return SosOut(**row)
