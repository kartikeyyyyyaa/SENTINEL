"""Database access.

Three deliberate choices:

1. **Plain SQL, no ORM.** Fewer moving parts, and the RLS behaviour stays obvious
   instead of being hidden behind a session/identity-map abstraction.

2. **The application asserts exactly one thing: `app.user_id`.** Jurisdiction
   scope, department, permissions and statewide status are all *derived inside
   the database* by `004_rls.sql`, which joins the identity tables itself. A
   compromised API process cannot widen its own scope by asserting a bigger
   claim, because there is no bigger claim to assert. This is why
   `SecurityContext` below carries permissions but never sends them: they are a
   cache for cheap pre-checks and clear 403 messages, not the authority.

3. **`set_config(..., is_local => true)`** scopes the value to the transaction,
   so a pooled connection cannot leak one user's identity into the next user's
   request. RLS policies read it via `current_setting(..., true)` and fail closed
   when it is absent, so an endpoint that forgets to set the context returns
   nothing rather than everything.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            settings.database_url,
            min_size=2,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        return init_pool()
    return _pool


class SecurityContext:
    """Who the caller is, plus a read-only cache of what the database already
    knows about them.

    Only `user_id` is ever sent to Postgres. The rest exists so the API can
    reject a request with a precise 403 before doing work, and so responses can
    explain scope to the operator console. If this cache ever disagreed with the
    database, the database would win — every protected table is behind RLS.
    """

    __slots__ = (
        "user_id",
        "username",
        "jurisdiction_path",
        "department_id",
        "permissions",
        "is_statewide",
    )

    def __init__(
        self,
        user_id: int,
        username: str = "",
        jurisdiction_path: str = "",
        department_id: int | None = None,
        permissions: frozenset[str] | set[str] = frozenset(),
        is_statewide: bool = False,
    ) -> None:
        self.user_id = int(user_id)
        self.username = username
        self.jurisdiction_path = jurisdiction_path
        self.department_id = department_id
        self.permissions = frozenset(permissions)
        self.is_statewide = is_statewide

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SecurityContext(user_id={self.user_id}, username={self.username!r}, "
            f"jurisdiction={self.jurisdiction_path!r}, statewide={self.is_statewide})"
        )


@contextlib.contextmanager
def transaction(ctx: SecurityContext | None = None) -> Iterator[psycopg.Cursor]:
    """Open a transaction, assert the caller's identity, yield a cursor.

    Passing ctx=None yields an *unscoped* transaction. RLS policies fail closed,
    so such a transaction sees no rows in protected tables — which is what we
    want for login, where no identity exists yet, and for health probes.

    The pool's context manager commits on clean exit and rolls back on exception,
    so a failed request cannot leave a partial write behind.
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if ctx is not None:
                # One claim, one parameter. Everything else is derived in SQL.
                cur.execute(
                    "SELECT set_config('app.user_id', %s, true)",
                    (str(ctx.user_id),),
                )
            yield cur


@contextlib.contextmanager
def admin_transaction() -> Iterator[psycopg.Cursor]:
    """Superuser connection. Migrations and seeding only — never request paths.

    Note that this role has NOBYPASSRLS *not* set, i.e. it bypasses RLS. That is
    exactly why no request handler may use it.
    """
    settings = get_settings()
    if not settings.database_admin_url:
        raise RuntimeError("DATABASE_ADMIN_URL is not configured")
    with psycopg.connect(settings.database_admin_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


def load_security_context(cur: psycopg.Cursor, user_id: int) -> SecurityContext | None:
    """Build a SecurityContext by asking the database what this user may do.

    Run this on an *unscoped* transaction (or an admin one) during token
    verification. It reads identity tables directly rather than trusting
    anything in the token beyond the user id, so revoking a role takes effect on
    the next request instead of when the access token expires.
    """
    cur.execute(
        """
        SELECT u.id,
               u.username,
               u.department_id,
               j.path                                      AS jurisdiction_path,
               coalesce(bool_or(r.is_statewide), false)     AS is_statewide,
               coalesce(
                   array_agg(DISTINCT rp.permission_code)
                       FILTER (WHERE rp.permission_code IS NOT NULL),
                   '{}'
               )                                           AS permissions
        FROM   app.app_user u
        JOIN   app.jurisdiction j       ON j.id = u.jurisdiction_id
        LEFT   JOIN app.user_role ur    ON ur.user_id = u.id
        LEFT   JOIN app.role r          ON r.id = ur.role_id
        LEFT   JOIN app.role_permission rp ON rp.role_id = ur.role_id
        WHERE  u.id = %s
          AND  u.is_active
        GROUP BY u.id, u.username, u.department_id, j.path
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return SecurityContext(
        user_id=row["id"],
        username=row["username"],
        jurisdiction_path=row["jurisdiction_path"],
        department_id=row["department_id"],
        permissions=frozenset(row["permissions"] or ()),
        is_statewide=row["is_statewide"],
    )


def fetch_all(
    cur: psycopg.Cursor, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> list[dict]:
    cur.execute(sql, params)
    return list(cur.fetchall())


def fetch_one(
    cur: psycopg.Cursor, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> dict | None:
    cur.execute(sql, params)
    return cur.fetchone()
