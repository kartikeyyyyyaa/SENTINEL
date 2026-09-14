"""Area-wise weekly incident reports — Sentinel's own alert history, aggregated.

Deliberately **not** called "crime reports". This system has no source of
official Gujarat crime statistics and must not fabricate one — see
docs/DATA_HANDLING_AND_RETENTION.md. What it does have is real: every alert
this platform itself opened (watchlist matches, women's-safety flags, SOS,
stream-gap escalations), each already visible to the caller one at a time via
GET /api/alerts. This endpoint is the same data, grouped by jurisdiction and
week — an aggregate view of what Sentinel detected, not a claim about crime in
that area beyond what the platform itself observed.

No new permission: if you may read alerts, you may read their aggregate; RLS
on ``app.alert`` (007_watchlist.sql) does the actual scoping either way, the
same way it already does for GET /api/alerts.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from ..deps import AuthContext, require_permission
from ..schemas import IncidentReportOut, IncidentWeekRow
from ...db import transaction

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/incidents", response_model=IncidentReportOut)
async def incident_report(
    auth: AuthContext = Depends(require_permission("alert.read")),
    weeks: int = Query(default=8, ge=1, le=52),
) -> IncidentReportOut:
    with transaction(auth.security) as cur:
        cur.execute(
            """
            SELECT coalesce(j.id, 0)              AS jurisdiction_id,
                   coalesce(j.path, 'UNASSIGNED')  AS jurisdiction_path,
                   coalesce(j.name, 'Unassigned')  AS jurisdiction_name,
                   date_trunc('week', a.opened_at) AS week_start,
                   a.kind, a.severity,
                   count(*)::int AS count
            FROM   app.alert a
            LEFT   JOIN app.jurisdiction j ON j.id = a.jurisdiction_id
            WHERE  a.opened_at >= now() - make_interval(weeks => %s)
            GROUP BY j.id, j.path, j.name, week_start, a.kind, a.severity
            ORDER BY week_start DESC, jurisdiction_path, a.kind
            """,
            (weeks,),
        )
        rows = cur.fetchall()
        # RLS on app.alert already confined the rows aggregated above to what
        # this caller may see (alert_select, 007_watchlist.sql) — the GROUP BY
        # ran over an already-scoped result set, nothing further to filter here.

    return IncidentReportOut(
        generated_at=datetime.now(timezone.utc),
        weeks=weeks,
        rows=[IncidentWeekRow(**row) for row in rows],
    )
