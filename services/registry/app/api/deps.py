"""Authentication and authorisation dependencies shared by every router.

Two ways in, one identity model out.

**Bearer JWT** (``Authorization: Bearer <access token>``) is how the operator
console authenticates a person. ``tokens.verify_access_token`` checks the
signature statelessly; everything after that is a fresh database lookup
(``db.load_security_context``) rather than anything read out of the token, so a
role change or a deactivated account takes effect on the very next request —
see ``core/tokens.py``'s module docstring for why that split exists at all.

**API key** (``Authorization: ApiKey <key>`` or ``X-API-Key: <key>``) is how a
machine calls in — today, the analytics worker's ``AlertHttpTransport`` and
``HttpTransport`` posting alerts/events, and the same worker's periodic
``GET /api/analytics/watchlist`` pull. Every key resolves to a machine
``app_user`` row (``008_api_key_user.sql``) and from there to a
``SecurityContext`` built the *same* way a human's is — one ``app.user_id``
claim, everything else derived in SQL. There is no second authorisation code
path to keep in sync with the first.

Either dependency yields a plain ``AuthContext``: the resolved
``SecurityContext`` plus enough about the request (client IP, user agent) to
write an audit row. ``require_permission`` composes on top for routes that need
one specific permission rather than merely "any authenticated caller".
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import get_settings
from ..core import tokens
from ..core.tokens import TokenError
from ..db import SecurityContext, load_security_context, transaction


@dataclass(slots=True)
class AuthContext:
    security: SecurityContext
    client_ip: str | None
    user_agent: str | None
    auth_method: str  # 'jwt' | 'api_key' — carried into audit rows' detail.


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP for the audit trail.

    Trusts ``X-Forwarded-For`` because this API is only ever reached through
    nginx (``web/nginx.conf`` sets it on every proxied request) or directly
    during development, where there is no proxy to spoof it in front of. A
    deployment that puts anything else in front of this API would need to
    re-examine this the same way any reverse-proxy trust boundary would.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_auth_context(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthContext:
    """Resolve whichever credential the caller presented.

    Checked in a fixed order — API key first — so a request that (mistakenly or
    maliciously) carries both is judged by one credential, not whichever a future
    refactor happens to check last.
    """
    settings = get_settings()
    client_ip = _client_ip(request)
    user_agent = request.headers.get("user-agent")

    api_key = x_api_key
    if api_key is None and authorization and authorization.lower().startswith("apikey "):
        api_key = authorization[7:].strip()

    if api_key:
        return _authenticate_api_key(api_key, client_ip=client_ip, user_agent=user_agent)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("missing bearer token or API key")

    token = authorization[7:].strip()
    try:
        claims = tokens.verify_access_token(token, secret=settings.jwt_secret)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    with transaction(None) as cur:
        ctx = load_security_context(cur, claims.user_id)
    if ctx is None:
        raise _unauthorized("account is inactive or no longer exists")

    return AuthContext(security=ctx, client_ip=client_ip, user_agent=user_agent, auth_method="jwt")


def _authenticate_api_key(api_key: str, *, client_ip: str | None, user_agent: str | None) -> AuthContext:
    try:
        prefix = tokens.split_api_key(api_key)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    key_hash = tokens.hash_api_key(api_key)

    with transaction(None) as cur:
        cur.execute(
            """
            SELECT id, key_hash, user_id, is_active, expires_at
            FROM   app.api_key
            WHERE  key_prefix = %s
            """,
            (prefix,),
        )
        row = cur.fetchone()

        # Constant-ish time even for an unknown prefix: hash comparison still
        # runs, so "unknown prefix" and "wrong secret" cost the same.
        from datetime import datetime, timezone

        from ..core.passwords import constant_time_equals

        valid = bool(row) and constant_time_equals(key_hash, row["key_hash"])
        if not row or not valid:
            raise _unauthorized("invalid API key")
        if not row["is_active"]:
            raise _unauthorized("API key has been revoked")
        if row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc):
            raise _unauthorized("API key has expired")
        if row["user_id"] is None:
            # A key minted before 008_api_key_user.sql, or created without the
            # machine app_user it needs. Fail loudly rather than silently
            # granting no permissions, which would look like a mysterious 403
            # three layers away from this cause.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="API key is not linked to a machine account",
            )

        cur.execute("UPDATE app.api_key SET last_used_at = now() WHERE id = %s", (row["id"],))
        ctx = load_security_context(cur, row["user_id"])

    if ctx is None:
        raise _unauthorized("the account behind this API key is inactive")

    return AuthContext(security=ctx, client_ip=client_ip, user_agent=user_agent, auth_method="api_key")


def require_permission(permission: str):
    """Dependency factory: 403 unless the caller holds ``permission``.

    A denial is audited out-of-band (see ``core/audit.record_out_of_band``) by
    the router that raises it, not here — this dependency only knows *that*
    access was denied, not which resource was being reached for, and a useful
    audit row needs both.
    """

    async def _check(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not auth.security.has(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing permission: {permission}",
            )
        return auth

    return _check


async def get_optional_auth_context(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthContext | None:
    """Same resolution as ``get_auth_context``, but ``None`` instead of a 401.

    For endpoints reachable both with and without a login — the SOS stream
    reader today. Never used to relax a write path: no endpoint should accept an
    unauthenticated write except ``POST /api/sos`` itself, which does not go
    through this dependency at all (see ``routers/alerts.py``'s module
    docstring on why that path is intentionally unauthenticated by design,
    not merely optionally so).
    """
    if not authorization and not x_api_key:
        return None
    try:
        return await get_auth_context(request, authorization, x_api_key)
    except HTTPException:
        return None
