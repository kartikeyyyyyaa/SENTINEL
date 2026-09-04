"""The audit trail, and an independent verifier for it.

The database builds the hash chain (see ``db/migrations/003_audit.sql``). This
module does two things:

1. Provides the helper every endpoint uses to *write* an audit row, including the
   canonical-JSON encoding of the ``detail`` field.
2. Reimplements the hash computation **byte-for-byte in Python**, so the chain can
   be verified by something that is not the database that produced it.

Point 2 is the one that matters for evidence. "Our audit log is tamper-evident"
is a much weaker claim when the only thing that can check it is the same system
that would have been subverted. Because the payload format is plain text with a
fixed field order, a court-appointed examiner can re-derive every hash from a CSV
export using nothing but a SHA-256 implementation — no Postgres, no our code, no
trust in us. ``docs/`` will carry the format as a written specification for
exactly that reason.

The payload, fields joined by ``|``:

    prev_hash or 'GENESIS' | id | at (UTC, 'YYYY-MM-DD HH24:MI:SS.US') |
    actor_user_id | actor_username | actor_ip (host form) | action |
    resource_type | resource_id | purpose | case_reference | outcome | detail

NULLs become empty strings. ``at`` is rendered in UTC with exactly six
fractional digits. There is no escaping of ``|`` inside fields — see the note on
``_FIELD_SEP`` below for why that is acceptable here and where the limit is.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_FIELD_SEP = "|"
_GENESIS = "GENESIS"

# A note on delimiter collision. A pipe inside `detail` or `actor_username` could
# in principle let two different field tuples produce the same payload string.
# It is not exploitable here: `detail` is always canonical JSON (so a literal pipe
# is inside a quoted string and cannot swallow a field boundary), `outcome` and
# `action` are drawn from fixed vocabularies, `id` and `at` are machine-generated,
# and `actor_ip` is an inet. The remaining free-text fields sit *between*
# structurally constrained ones, so any ambiguity would require forging an id or a
# timestamp too. Length-prefixing every field would remove the concern entirely at
# the cost of a payload no human can read in psql; for an audit format whose whole
# value is third-party verifiability, legibility won.


class AuditAction:
    """Action vocabulary. Strings, not an enum, because they cross into SQL and
    out to JSON constantly and an enum only adds `.value` noise at every site."""

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_LOCKED = "auth.login.locked"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token.refresh"
    TOKEN_REUSE_DETECTED = "auth.token.reuse_detected"
    PASSWORD_CHANGED = "auth.password.changed"

    CAMERA_CREATE = "camera.create"
    CAMERA_UPDATE = "camera.update"
    CAMERA_DELETE = "camera.delete"
    CAMERA_VIEW = "camera.view"
    CAMERA_LIST = "camera.list"
    CAMERA_EXPORT = "camera.export"
    CAMERA_IMPORT = "camera.import"
    CREDENTIAL_READ = "camera.credential.read"
    CREDENTIAL_WRITE = "camera.credential.write"
    HEALTH_PROBE = "camera.health.probe"

    WATCHLIST_CREATE = "watchlist.create"
    WATCHLIST_UPDATE = "watchlist.update"
    WATCHLIST_MATCH_INGEST = "watchlist.match.ingest"

    ALERT_INGEST = "alert.ingest"
    ALERT_ACKNOWLEDGE = "alert.acknowledge"
    ALERT_CLOSE = "alert.close"

    SOS_CREATE = "sos.create"
    SOS_RESOLVE = "sos.resolve"

    REPORT_GAP_ANALYSIS = "report.gap_analysis"
    REPORT_AGEING = "report.ageing"

    AUDIT_READ = "audit.read"
    AUDIT_VERIFY = "audit.verify"

    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    ROLE_ASSIGN = "role.assign"
    APIKEY_ISSUE = "apikey.issue"
    APIKEY_REVOKE = "apikey.revoke"

    PERMISSION_DENIED = "access.denied"


class Outcome:
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


def canonical_json(obj: Any) -> str | None:
    """Deterministic JSON for the ``detail`` column.

    Sorted keys and no whitespace, so the same logical detail always hashes to
    the same value regardless of dict insertion order. ``ensure_ascii=False``
    keeps Gujarati and Hindi place names readable in psql instead of turning them
    into ``\\uXXXX`` soup; the column is UTF-8 either way.
    """
    if obj is None:
        return None
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def format_timestamp(at: datetime) -> str:
    """Render a timestamp exactly as Postgres' to_char(..., 'YYYY-MM-DD HH24:MI:SS.US') does.

    A naive datetime is treated as UTC, matching how Postgres would interpret a
    timestamp without time zone. ``%f`` is always six digits, which is what
    ``.US`` produces.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def format_ip(value: Any) -> str:
    """Match Postgres' ``host(inet)``: address only, no prefix length.

    Postgres abbreviates IPv6 the same way Python's ipaddress module does, so
    round-tripping through ip_address gives us the identical string.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        # Not a parseable address. Hash it verbatim rather than silently dropping
        # it; a malformed value in the trail is itself worth preserving.
        return text


@dataclass(slots=True)
class AuditRecord:
    """One row of the trail, as Python sees it."""

    id: int
    at: datetime
    action: str
    outcome: str = Outcome.SUCCESS
    actor_user_id: int | None = None
    actor_username: str | None = None
    actor_ip: Any = None
    actor_agent: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    purpose: str | None = None
    case_reference: str | None = None
    detail: str | None = None
    prev_hash: str | None = None
    row_hash: str | None = None

    def payload(self, prev_hash: str | None = ...) -> str:  # type: ignore[assignment]
        """The exact string the database hashes.

        Pass ``prev_hash`` explicitly when walking a chain; omit it to use the
        value stored on the row.
        """
        prev = self.prev_hash if prev_hash is ... else prev_hash
        parts = [
            prev if prev else _GENESIS,
            str(self.id),
            format_timestamp(self.at),
            "" if self.actor_user_id is None else str(self.actor_user_id),
            self.actor_username or "",
            format_ip(self.actor_ip),
            self.action,
            self.resource_type or "",
            self.resource_id or "",
            self.purpose or "",
            self.case_reference or "",
            self.outcome,
            self.detail or "",
        ]
        return _FIELD_SEP.join(parts)

    def compute_hash(self, prev_hash: str | None = ...) -> str:  # type: ignore[assignment]
        return hashlib.sha256(self.payload(prev_hash).encode("utf-8")).hexdigest()

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AuditRecord:
        """Build from a DB row or a CSV export. Tolerates ISO-8601 strings for
        ``at`` so an examiner can verify straight from exported text."""
        at = row["at"]
        if isinstance(at, str):
            at = datetime.fromisoformat(at)
        return cls(
            id=int(row["id"]),
            at=at,
            action=row["action"],
            outcome=row.get("outcome") or Outcome.SUCCESS,
            actor_user_id=row.get("actor_user_id"),
            actor_username=row.get("actor_username"),
            actor_ip=row.get("actor_ip"),
            actor_agent=row.get("actor_agent"),
            resource_type=row.get("resource_type"),
            resource_id=row.get("resource_id"),
            purpose=row.get("purpose"),
            case_reference=row.get("case_reference"),
            detail=row.get("detail"),
            prev_hash=row.get("prev_hash"),
            row_hash=row.get("row_hash"),
        )


@dataclass(slots=True)
class ChainVerdict:
    checked_rows: int
    is_intact: bool
    broken_at_id: int | None = None
    reason: str | None = None
    verified_range: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_rows": self.checked_rows,
            "is_intact": self.is_intact,
            "broken_at_id": self.broken_at_id,
            "reason": self.reason,
            "verified_range": list(self.verified_range) if self.verified_range else None,
        }


def verify_chain(rows: Iterable[Mapping[str, Any] | AuditRecord]) -> ChainVerdict:
    """Verify a contiguous run of audit rows, in ascending id order.

    Mirrors ``app.verify_audit_chain``, including its behaviour when starting
    mid-chain: the first row's stored ``prev_hash`` is adopted as the baseline,
    since the row it points at may be outside the supplied range.

    Two distinct failures are reported separately because they mean different
    things operationally. A ``prev_hash`` mismatch says rows were **removed or
    reordered**. A ``row_hash`` mismatch says a row's **contents were edited**.
    """
    prev: str | None = None
    n = 0
    first = True
    first_id: int | None = None
    last_id: int | None = None

    for raw in rows:
        rec = raw if isinstance(raw, AuditRecord) else AuditRecord.from_row(raw)

        if first:
            prev = rec.prev_hash
            first = False
            first_id = rec.id

        if (rec.prev_hash or "") != (prev or ""):
            return ChainVerdict(
                checked_rows=n,
                is_intact=False,
                broken_at_id=rec.id,
                reason="prev_hash does not match the preceding row's row_hash; "
                "rows were removed or reordered",
                verified_range=(first_id, last_id) if last_id is not None else None,
            )

        expected = rec.compute_hash(prev)
        if expected != rec.row_hash:
            return ChainVerdict(
                checked_rows=n,
                is_intact=False,
                broken_at_id=rec.id,
                reason="row_hash does not match recomputed hash; row contents were altered",
                verified_range=(first_id, last_id) if last_id is not None else None,
            )

        prev = rec.row_hash
        last_id = rec.id
        n += 1

    return ChainVerdict(
        checked_rows=n,
        is_intact=True,
        verified_range=(first_id, last_id) if first_id is not None else None,
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

_INSERT_SQL = """
SELECT * FROM app.write_audit_row(
    %(actor_user_id)s, %(actor_username)s, %(actor_ip)s, %(actor_agent)s,
    %(action)s, %(resource_type)s, %(resource_id)s,
    %(purpose)s, %(case_reference)s, %(outcome)s, %(detail)s
)
"""
# A plain `INSERT ... RETURNING id, at, prev_hash, row_hash` looks equivalent
# and very nearly was what shipped here — but RETURNING requires the new row
# to satisfy the table's SELECT policy, not just the INSERT policy's WITH
# CHECK, and the actor writing the row most worth capturing (anonymous, or
# denied the very permission being logged) is exactly the actor who never
# satisfies it. See 010_audit_insert_return.sql for the full story and the
# SECURITY DEFINER function this now calls instead — it does the same insert
# and read-back, just without asking the caller's own RLS view to agree.


def record(
    cur: Any,
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
    actor_ip: str | None = None,
    actor_agent: str | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    purpose: str | None = None,
    case_reference: str | None = None,
    outcome: str = Outcome.SUCCESS,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append one row inside the caller's transaction. The trigger fills
    ``prev_hash`` and ``row_hash``.

    Sharing the caller's transaction is the point: if the action rolls back, so
    does its audit row, and the trail never claims something happened that did
    not. The converse — an action that commits without its audit row — is
    impossible for the same reason.

    The insert runs inside a **savepoint**. In Postgres a failed statement aborts
    the whole transaction, so simply catching the exception would leave the
    connection poisoned and turn a working request into a baffling "current
    transaction is aborted" further down. The savepoint confines the damage, so a
    failed audit write costs us the audit row and nothing else.

    Use this on paths where the transaction is healthy. From an ``except`` block,
    where it may already be aborted, use :func:`record_out_of_band`.
    """
    params = _params(
        action,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        actor_ip=actor_ip,
        actor_agent=actor_agent,
        resource_type=resource_type,
        resource_id=resource_id,
        purpose=purpose,
        case_reference=case_reference,
        outcome=outcome,
        detail=detail,
    )
    try:
        with cur.connection.transaction():  # SAVEPOINT / RELEASE
            cur.execute(_INSERT_SQL, params)
            return cur.fetchone()
    except Exception:  # noqa: BLE001 - see docstring
        log.warning(
            "audit write failed for action=%s resource=%s/%s",
            action,
            resource_type,
            resource_id,
            exc_info=True,
        )
        return None


def record_out_of_band(action: str, **kwargs: Any) -> dict[str, Any] | None:
    """Append one row on a fresh connection, independent of any request transaction.

    Needed for the denial and error paths. A permission check that fails, or an
    unhandled exception, will roll the request's transaction back — and if the
    audit row rode along inside it, the very events most worth recording would be
    the ones that vanish. An attacker probing for authorisation gaps would
    generate a beautifully empty trail.

    So these rows get their own connection and commit on their own. The cost is
    that a denial row is not atomic with the request; that is the correct trade,
    because there is no state change to be atomic *with*.
    """
    from ..db import get_pool  # local import: avoids a cycle at module load

    params = _params(action, **kwargs)
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, params)
                return cur.fetchone()
    except Exception:  # noqa: BLE001
        log.warning("out-of-band audit write failed for action=%s", action, exc_info=True)
        return None


def _params(
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
    actor_ip: str | None = None,
    actor_agent: str | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    purpose: str | None = None,
    case_reference: str | None = None,
    outcome: str = Outcome.SUCCESS,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "actor_user_id": actor_user_id,
        "actor_username": actor_username,
        "actor_ip": actor_ip or None,
        # User agents are attacker-controlled and unbounded. Truncate before the
        # value reaches the hash payload.
        "actor_agent": (actor_agent or "")[:512] or None,
        "action": action,
        "resource_type": resource_type,
        "resource_id": None if resource_id is None else str(resource_id),
        "purpose": purpose,
        "case_reference": case_reference,
        "outcome": outcome,
        "detail": canonical_json(detail),
    }
