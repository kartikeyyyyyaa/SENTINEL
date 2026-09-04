"""Login, refresh and logout.

Every branch — success, bad password, lockout, reuse detection — writes an
audit row, because a login endpoint that only logs success cannot answer "who
tried to get in and failed" during an incident review, which is exactly the
question an incident review asks first.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import AuthContext, get_auth_context
from ..schemas import LoginRequest, RefreshRequest, TokenResponse, UserSummary
from ...config import get_settings
from ...core import tokens
from ...core.audit import AuditAction, Outcome, record, record_out_of_band
from ...core.passwords import evaluate_lockout, needs_rehash, verify_password
from ...core.tokens import TokenError
from ...db import load_security_context, transaction

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    settings = get_settings()
    ip = _client_ip(request)
    agent = request.headers.get("user-agent")

    with transaction(None) as cur:
        cur.execute(
            """
            SELECT id, username, full_name, password_hash, is_active,
                   failed_attempts, locked_until
            FROM   app.app_user
            WHERE  username = %s
            """,
            (body.username,),
        )
        row = cur.fetchone()

        ok = verify_password(body.password, row["password_hash"] if row else None)
        ok = ok and bool(row) and row["is_active"]

        if row:
            decision = evaluate_lockout(
                failed_attempts=row["failed_attempts"],
                locked_until=row["locked_until"],
                success=ok,
                max_failures=settings.max_failed_logins,
                lockout_minutes=settings.lockout_minutes,
            )
            cur.execute(
                "UPDATE app.app_user SET failed_attempts = %s, locked_until = %s WHERE id = %s",
                (decision.failed_attempts, decision.locked_until, row["id"]),
            )
            if decision.locked and not ok:
                record_out_of_band(
                    AuditAction.LOGIN_LOCKED,
                    actor_username=body.username,
                    actor_ip=ip,
                    actor_agent=agent,
                    outcome=Outcome.DENIED,
                    resource_type="app_user",
                    resource_id=row["id"],
                )
                raise HTTPException(
                    status_code=status.HTTP_423_LOCKED,
                    detail=f"account locked for {settings.lockout_minutes} minutes after repeated failures",
                )
            if decision.locked and ok:
                # Lock had already expired and this attempt cleared it — fine,
                # evaluate_lockout already returned locked=False for that case,
                # so this branch is unreachable; kept only as a documented
                # invariant, not a real code path.
                pass  # pragma: no cover

        if not ok:
            record_out_of_band(
                AuditAction.LOGIN_FAILED,
                actor_username=body.username,
                actor_ip=ip,
                actor_agent=agent,
                outcome=Outcome.DENIED,
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

        if needs_rehash(row["password_hash"]):
            from ...core.passwords import hash_password

            cur.execute(
                "UPDATE app.app_user SET password_hash = %s WHERE id = %s",
                (hash_password(body.password), row["id"]),
            )

        cur.execute(
            "UPDATE app.app_user SET last_login_at = now(), last_login_ip = %s WHERE id = %s",
            (ip, row["id"]),
        )

        ctx = load_security_context(cur, row["id"])
        if ctx is None:  # pragma: no cover - would mean is_active flipped mid-transaction
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account is inactive")

        session_id = tokens.new_session_id()
        refresh_token, refresh_hash = tokens.new_refresh_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.refresh_token_ttl_hours)
        cur.execute(
            """
            INSERT INTO app.refresh_token (user_id, token_hash, family_id, expires_at, issued_ip, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (ctx.user_id, refresh_hash, session_id, expires_at, ip, agent),
        )

        access_token, claims = tokens.issue_access_token(
            secret=settings.jwt_secret,
            user_id=ctx.user_id,
            session=session_id,
            ttl_minutes=settings.access_token_ttl_minutes,
        )

        record(
            cur,
            AuditAction.LOGIN_SUCCESS,
            actor_user_id=ctx.user_id,
            actor_username=row["username"],
            actor_ip=ip,
            actor_agent=agent,
        )

        full_name = row["full_name"]

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserSummary(
            id=ctx.user_id,
            username=ctx.username,
            full_name=full_name,
            jurisdiction_path=ctx.jurisdiction_path,
            department_id=ctx.department_id,
            is_statewide=ctx.is_statewide,
            permissions=sorted(ctx.permissions),
        ),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, request: Request) -> TokenResponse:
    """Rotate a refresh token. Reuse of an already-consumed one burns the
    whole session family — see 006_token_rotation.sql's module docstring."""
    settings = get_settings()
    ip = _client_ip(request)
    agent = request.headers.get("user-agent")
    token_hash = tokens.hash_refresh_token(body.refresh_token)

    with transaction(None) as cur:
        cur.execute("SELECT * FROM app.consume_refresh_token(%s)", (token_hash,))
        result = cur.fetchone()

        if result["status"] == "reused":
            record_out_of_band(
                AuditAction.TOKEN_REUSE_DETECTED,
                actor_user_id=result["user_id"],
                actor_ip=ip,
                actor_agent=agent,
                outcome=Outcome.DENIED,
                detail={"family_id": str(result["family_id"])},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh token already used — session revoked, please log in again",
            )
        if result["status"] != "ok":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token is not valid")

        ctx = load_security_context(cur, result["user_id"])
        if ctx is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account is inactive")

        new_refresh, new_hash = tokens.new_refresh_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.refresh_token_ttl_hours)
        cur.execute(
            """
            INSERT INTO app.refresh_token (user_id, token_hash, family_id, expires_at, issued_ip, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (ctx.user_id, new_hash, result["family_id"], expires_at, ip, agent),
        )
        new_id = cur.fetchone()["id"]
        cur.execute("UPDATE app.refresh_token SET replaced_by = %s WHERE id = %s", (new_id, result["token_id"]))

        access_token, _ = tokens.issue_access_token(
            secret=settings.jwt_secret,
            user_id=ctx.user_id,
            session=str(result["family_id"]),
            ttl_minutes=settings.access_token_ttl_minutes,
        )

        record(cur, AuditAction.TOKEN_REFRESH, actor_user_id=ctx.user_id, actor_ip=ip, actor_agent=agent)

        cur.execute("SELECT full_name FROM app.app_user WHERE id = %s", (ctx.user_id,))
        full_name = cur.fetchone()["full_name"]

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserSummary(
            id=ctx.user_id,
            username=ctx.username,
            full_name=full_name,
            jurisdiction_path=ctx.jurisdiction_path,
            department_id=ctx.department_id,
            is_statewide=ctx.is_statewide,
            permissions=sorted(ctx.permissions),
        ),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, auth: AuthContext = Depends(get_auth_context)) -> None:
    """Revoke the presented session's whole token family.

    Takes the refresh token in the body (not just the access token in the
    header) because logging out must kill the *family*, and the family id is
    embedded in the refresh chain, not derivable from an access token alone
    without a lookup this endpoint already has to do.
    """
    token_hash = tokens.hash_refresh_token(body.refresh_token)
    with transaction(auth.security) as cur:
        cur.execute("SELECT family_id, user_id FROM app.refresh_token WHERE token_hash = %s", (token_hash,))
        row = cur.fetchone()
        if row and row["user_id"] == auth.security.user_id:
            cur.execute("SELECT app.revoke_token_family(%s, %s)", (row["family_id"], "user logout"))
        record(
            cur,
            AuditAction.LOGOUT,
            actor_user_id=auth.security.user_id,
            actor_username=auth.security.username,
            actor_ip=auth.client_ip,
            actor_agent=auth.user_agent,
        )
