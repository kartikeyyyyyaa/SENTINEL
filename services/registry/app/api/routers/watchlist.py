"""The searchable watchlist — Step 3's stolen-vehicle / wanted / missing /
blacklisted / suspect database — and the "criminal mapping" query over it.

Matching itself never happens here; it happens at the edge
(``services/analytics/watchlist.py``). This router is where an operator adds
an entry, where the worker's periodic refresh pulls the active list from
(``GET /api/analytics/watchlist``, in ``routers/analytics.py`` — kept
separate because it authenticates as a machine, not a person), and where "every
sighting of entry X, across every camera, in order" is answered from
``app.watchlist_match`` — the accumulated trail that *is* the criminal-mapping
capability, built from nothing more than alerts arriving over time.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import AuthContext, require_permission
from ..schemas import WatchlistEntryCreate, WatchlistEntryOut, WatchlistEntryUpdate, WatchlistMatchOut
from ...core.audit import AuditAction, record
from ...db import transaction

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _row_to_out(row: dict) -> WatchlistEntryOut:
    return WatchlistEntryOut(
        id=row["id"],
        entry_type=row["entry_type"],
        risk_level=row["risk_level"],
        status=row["status"],
        plate_number=row["plate_number"],
        label=row["label"],
        case_reference=row["case_reference"],
        notes=row["notes"],
        department_id=row["department_id"],
        jurisdiction_id=row["jurisdiction_id"],
        source=row["source"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_face_embedding=row.get("person_embedding") is not None,
    )


_ENTRY_COLUMNS = """
    id, entry_type, risk_level, status, plate_number, label, case_reference,
    notes, department_id, jurisdiction_id, source, expires_at, created_at,
    updated_at, person_embedding
"""


@router.get("", response_model=list[WatchlistEntryOut])
async def search_watchlist(
    auth: AuthContext = Depends(require_permission("watchlist.read")),
    q: str | None = Query(default=None, description="plate number or free-text label match"),
    entry_type: str | None = None,
    status_filter: str | None = Query(default=None, alias="status", description="default: active only"),
) -> list[WatchlistEntryOut]:
    clauses = ["true"]
    params: list[object] = []

    if status_filter:
        clauses.append("status = %s")
        params.append(status_filter)
    else:
        clauses.append("status = 'active'")

    if entry_type:
        clauses.append("entry_type = %s")
        params.append(entry_type)

    if q:
        normalized = "".join(ch for ch in q.upper() if ch.isalnum())
        clauses.append("(plate_number LIKE %s OR label ILIKE %s OR case_reference ILIKE %s)")
        params.extend([f"%{normalized}%", f"%{q}%", f"%{q}%"])

    with transaction(auth.security) as cur:
        cur.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM app.watchlist_entry WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT 500",
            params,
        )
        rows = cur.fetchall()
    return [_row_to_out(r) for r in rows]


@router.post("", response_model=WatchlistEntryOut, status_code=status.HTTP_201_CREATED)
async def create_entry(
    body: WatchlistEntryCreate, auth: AuthContext = Depends(require_permission("watchlist.write"))
) -> WatchlistEntryOut:
    plate = body.plate_number.upper().replace(" ", "").replace("-", "") if body.plate_number else None

    with transaction(auth.security) as cur:
        cur.execute(
            f"""
            INSERT INTO app.watchlist_entry (
                entry_type, risk_level, plate_number, person_embedding, embedding_model,
                label, case_reference, notes, department_id, jurisdiction_id,
                source, expires_at, created_by, updated_by
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s
            )
            RETURNING {_ENTRY_COLUMNS}
            """,
            (
                body.entry_type,
                body.risk_level,
                plate,
                json.dumps(body.person_embedding) if body.person_embedding else None,
                body.embedding_model,
                body.label,
                body.case_reference,
                body.notes,
                body.department_id,
                body.jurisdiction_id,
                body.expires_at,
                auth.security.user_id,
                auth.security.user_id,
            ),
        )
        row = cur.fetchone()
        record(
            cur,
            AuditAction.WATCHLIST_CREATE,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
            resource_type="watchlist_entry",
            resource_id=row["id"],
            case_reference=body.case_reference,
            detail={"entry_type": body.entry_type, "risk_level": body.risk_level},
        )
    return _row_to_out(row)


@router.patch("/{entry_id}", response_model=WatchlistEntryOut)
async def update_entry(
    entry_id: int,
    body: WatchlistEntryUpdate,
    auth: AuthContext = Depends(require_permission("watchlist.write")),
) -> WatchlistEntryOut:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no fields to update")

    set_clause = ", ".join(f"{k} = %s" for k in fields) + ", updated_by = %s"
    params = [*fields.values(), auth.security.user_id, entry_id]

    with transaction(auth.security) as cur:
        cur.execute(
            f"UPDATE app.watchlist_entry SET {set_clause} WHERE id = %s RETURNING {_ENTRY_COLUMNS}",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watchlist entry not found")
        record(
            cur,
            AuditAction.WATCHLIST_UPDATE,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
            resource_type="watchlist_entry",
            resource_id=entry_id,
            detail=fields,
        )
    return _row_to_out(row)


@router.get("/{entry_id}/trail", response_model=list[WatchlistMatchOut])
async def watchlist_trail(
    entry_id: int, auth: AuthContext = Depends(require_permission("watchlist.read"))
) -> list[WatchlistMatchOut]:
    """Every sighting of one watchlist entry, in order — the "start mapping the
    criminal" query. Two matches from two cameras minutes apart is, by
    construction, a trail: which camera, when, at what confidence."""
    with transaction(auth.security) as cur:
        cur.execute(
            """
            SELECT id, entry_id, camera_id, track_id, basis, confidence, raw_value,
                   matched_at, alert_id
            FROM   app.watchlist_match
            WHERE  entry_id = %s
            ORDER BY matched_at ASC
            LIMIT 2000
            """,
            (entry_id,),
        )
        rows = cur.fetchall()
    if not rows:
        # Confirm the entry itself is visible before returning an empty trail,
        # so "no sightings yet" and "not your jurisdiction" are distinguishable.
        with transaction(auth.security) as cur:
            cur.execute("SELECT 1 FROM app.watchlist_entry WHERE id = %s", (entry_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watchlist entry not found")
    return [WatchlistMatchOut(**r) for r in rows]
