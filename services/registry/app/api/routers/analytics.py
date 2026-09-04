"""Machine-only endpoints: what an edge analytics worker talks to.

Every route here authenticates as an API key resolved to a machine
``app_user`` (see ``api/deps.py`` and ``008_api_key_user.sql``) — never as a
person. Three jobs:

* ``GET /api/analytics/watchlist`` — the periodic pull
  ``services/analytics/watchlist.py``'s ``WatchlistRefresher`` makes, so a
  worker started five minutes ago has the same active watchlist an operator
  edited five minutes ago.
* ``POST /api/analytics/alerts`` — what ``services/analytics/sink.py``'s
  ``AlertHttpTransport`` posts. Persisted (upsert by ``alert_key``, so the
  transport's own retry-with-backoff cannot duplicate a page an operator has
  already seen) and broadcast to any open SSE connection in the same request.
* ``POST /api/analytics/events`` — what its plain ``HttpTransport`` posts for
  primitive events. Broadcast-only, never persisted: a primitive is a high-
  volume observed fact (``services/common/events.py``), not the operational
  record this database exists to keep — only the *judgements* built from
  primitives (alerts) earn a row. The console's live feed still wants to show
  them as they happen, which is exactly what a broadcast (and nothing else)
  is for.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg.errors import UniqueViolation

from ..deps import AuthContext, get_auth_context
from ..schemas import AlertIngest, AnalyticsEventIngest, WatchlistPullEntry, WatchlistPullResponse
from ...core.audit import AuditAction, record
from ...core.broadcast import broadcaster
from ...db import transaction

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_ALERT_COLUMNS = """
    id, kind, severity, status, camera_id, jurisdiction_path, alert_key,
    track_ids, source_event_kinds, summary, detail, case_reference,
    opened_at, acknowledged_at, closed_at, close_reason
"""


def _require_ingest(auth: AuthContext) -> None:
    if not (auth.security.has("watchlist.read") or auth.security.has("camera.health_write")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller may neither read the watchlist nor write health/match records",
        )


@router.get("/watchlist", response_model=WatchlistPullResponse)
async def pull_watchlist(auth: AuthContext = Depends(get_auth_context)) -> WatchlistPullResponse:
    if not auth.security.has("watchlist.read"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing permission: watchlist.read")

    with transaction(auth.security) as cur:
        cur.execute(
            """
            SELECT id, entry_type, plate_number, person_embedding, embedding_model,
                   risk_level, label, case_reference
            FROM   app.watchlist_entry
            WHERE  status = 'active' AND (expires_at IS NULL OR expires_at > now())
            """
        )
        rows = cur.fetchall()

    entries = [
        WatchlistPullEntry(
            id=r["id"],
            entry_type=r["entry_type"],
            plate_number=r["plate_number"],
            person_embedding=(json.loads(r["person_embedding"]) if r["person_embedding"] else None),
            embedding_model=r["embedding_model"],
            risk_level=r["risk_level"],
            label=r["label"],
            case_reference=r["case_reference"],
        )
        for r in rows
    ]
    from datetime import datetime, timezone

    return WatchlistPullResponse(entries=entries, generated_at=datetime.now(timezone.utc))


@router.post("/alerts", status_code=status.HTTP_202_ACCEPTED)
async def ingest_alert(body: AlertIngest, auth: AuthContext = Depends(get_auth_context)) -> dict:
    _require_ingest(auth)

    with transaction(auth.security) as cur:
        jurisdiction_id = jurisdiction_path = None
        if body.camera_id is not None:
            cur.execute(
                "SELECT jurisdiction_id, jurisdiction_path FROM app.camera WHERE id = %s",
                (body.camera_id,),
            )
            cam = cur.fetchone()
            if cam:
                jurisdiction_id, jurisdiction_path = cam["jurisdiction_id"], cam["jurisdiction_path"]

        # Upsert by alert_key: try the insert; on a collision with an already-
        # open alert of the same key, fetch it instead of writing to it.
        #
        # Deliberately fetch-not-mutate on the collision path, for a reason
        # that only shows up once you try it the other way: app.alert's
        # `alert_update` RLS policy gates on 'alert.acknowledge', because in
        # every other caller of that policy an UPDATE *is* an operator's
        # triage decision. But a repeat delivery under the same alert_key is
        # not a triage decision — it is either the sink's own retry after a
        # failed send (services/analytics/sink.py's AlertSink), which resends
        # identical content and needs no write at all, or a second real
        # sighting inside the rule engine's cooldown window (rules.py's
        # `_due()`), which belongs in the append-only watchlist_match log
        # rather than overwriting the first sighting's alert row. Either way,
        # the correct permission for a machine ingest principal to hold is
        # 'watchlist.read' / 'camera.health_write' (checked above) — not
        # 'alert.acknowledge', which stays an operator-only permission with
        # nothing here that would need to widen it.
        #
        # This is also INSERT-first rather than SELECT-then-branch: a
        # `SELECT ... FOR UPDATE` pre-check was tried first and rejected for
        # a parallel reason — under Postgres RLS, `FOR UPDATE` filters rows
        # through the UPDATE policy's USING clause *in addition to* the SELECT
        # policy's, so the same permission problem would appear one query
        # earlier. Letting the unique index (`alert_open_key_idx`) be the
        # arbiter needs no permission beyond ordinary INSERT, and it is the
        # actual correctness mechanism for a true concurrent race anyway: two
        # overlapping requests can both pass any "does it exist" pre-check,
        # and only the constraint can arbitrate between them.
        was_insert = True
        try:
            with cur.connection.transaction():  # SAVEPOINT: confine a UniqueViolation
                cur.execute(
                    f"""
                    INSERT INTO app.alert (
                        kind, severity, camera_id, jurisdiction_id, jurisdiction_path,
                        alert_key, track_ids, source_event_kinds, summary, detail,
                        case_reference, opened_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_ALERT_COLUMNS}
                    """,
                    (
                        body.kind,
                        body.severity,
                        body.camera_id,
                        jurisdiction_id,
                        jurisdiction_path,
                        body.alert_key,
                        body.track_ids,
                        body.source_event_kinds,
                        body.summary,
                        json.dumps(body.detail),
                        body.case_reference,
                        body.ts,
                    ),
                )
                row = cur.fetchone()
        except UniqueViolation:
            was_insert = False
            cur.execute(
                f"SELECT {_ALERT_COLUMNS} FROM app.alert WHERE alert_key = %s AND status IN ('open', 'acknowledged')",
                (body.alert_key,),
            )
            row = cur.fetchone()
            if row is None:
                # The conflicting alert was closed between our failed insert
                # and this fetch — vanishingly unlikely, but not impossible.
                # Retrying the whole request is the caller's job (the sink's
                # own retry-with-backoff); a 409 says so rather than pretending
                # a row that no longer qualifies is the one that was ingested.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="alert_key conflicted with a row that no longer matches an open alert",
                )

        if body.watchlist_entry_id is not None:
            cur.execute(
                """
                INSERT INTO app.watchlist_match (
                    entry_id, camera_id, track_id, basis, confidence, raw_value, matched_at, alert_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    body.watchlist_entry_id,
                    body.camera_id,
                    body.match_track_id,
                    body.match_basis or "plate",
                    body.match_confidence,
                    body.match_raw_value,
                    body.ts,
                    row["id"],
                ),
            )

        if was_insert:
            record(
                cur,
                AuditAction.ALERT_INGEST,
                actor_user_id=auth.security.user_id,
                actor_username=auth.security.username,
                actor_ip=auth.client_ip,
                actor_agent=auth.user_agent,
                resource_type="alert",
                resource_id=row["id"],
                case_reference=body.case_reference,
                detail={"kind": body.kind, "severity": body.severity, "camera_id": body.camera_id},
            )

    broadcaster.publish({"type": "alert.created" if was_insert else "alert.updated", "alert": row})
    return {"id": row["id"], "status": "created" if was_insert else "updated"}


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(body: AnalyticsEventIngest, auth: AuthContext = Depends(get_auth_context)) -> dict:
    """Broadcast-only — see module docstring. No database write, no audit row:
    there is nothing here that outlives the SSE connections open right now."""
    broadcaster.publish(
        {
            "type": "event",
            "event": {
                "kind": body.kind,
                "camera_id": body.camera_id,
                "ts": body.ts.isoformat(),
                "track_ids": body.track_ids,
                "detail": body.detail,
            },
        }
    )
    return {"status": "broadcast", "subscribers": broadcaster.subscriber_count}
