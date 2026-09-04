"""Access tokens, refresh tokens and machine API keys.

**Two different mechanisms on purpose.**

*Access tokens* are short-lived JWTs (15 minutes). They are self-contained so the
hot path — every API request — needs no database round trip to authenticate.

*Refresh tokens* are opaque random strings, not JWTs. There is nothing to read
inside them; their entire meaning is a row in ``app.refresh_token``. That makes
them revocable instantly, which a stateless JWT is not.

The split matters because the two requirements genuinely conflict. Stateless
verification is fast but unrevokable; stateful verification is revokable but
costs a query. Giving the fast path a 15-minute blast radius and the long-lived
credential full revocability gets both properties where each one is needed.

**What is deliberately *not* in the access token.** No permissions, no
jurisdiction, no department, no role. Only the user id and a session marker. Two
reasons:

* An access token carrying ``permissions: [...]`` is a capability that survives
  revocation. Strip a compromised account's rights and it keeps them until the
  token expires. Here, permissions are read from the database on every request,
  so a revocation takes effect on the next call.
* The database is the authority for authorisation (see ``004_rls.sql``). A token
  asserting scope would create a second, competing source of truth, and the
  interesting attacks live in the gap between two such sources.

The cost is one indexed query per request. That is the right price.

**Signing.** HS256 with a secret from the environment. Asymmetric signing (RS256)
buys nothing while one service both issues and verifies; it starts to matter when
Models 2 and 3 verify tokens they did not issue, and the ``kid`` header is already
carried so that migration does not need a flag day. The verification call pins
``algorithms=["HS256"]`` explicitly — omitting it is the classic ``alg: none``
confusion bug.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

ALGORITHM = "HS256"
ISSUER = "sentinel-registry"
AUDIENCE = "sentinel-api"

ACCESS_TYPE = "access"

# Opaque token entropy. 32 bytes = 256 bits, which is not guessable by anything.
REFRESH_BYTES = 32
API_KEY_BYTES = 32
API_KEY_PREFIX_LEN = 12


class TokenError(Exception):
    """Token missing, malformed, expired or not trustworthy."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: int
    jti: str
    session: str
    issued_at: datetime
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------


def issue_access_token(
    *, secret: str, user_id: int, session: str, ttl_minutes: int, key_id: str = "v1"
) -> tuple[str, AccessClaims]:
    now = _now()
    expires = now + timedelta(minutes=ttl_minutes)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "jti": uuid.uuid4().hex,
        "sid": session,  # the refresh-token family, so a session is traceable
        "typ": ACCESS_TYPE,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(claims, secret, algorithm=ALGORITHM, headers={"kid": key_id})
    return token, AccessClaims(
        user_id=user_id,
        jti=claims["jti"],
        session=session,
        issued_at=now,
        expires_at=expires,
    )


def verify_access_token(token: str, *, secret: str, leeway_seconds: int = 5) -> AccessClaims:
    """Verify and decode an access token.

    Every check is explicit. ``require`` forces the claims to be present rather
    than treating a missing ``exp`` as "never expires", which is what a bare
    ``jwt.decode`` would do.
    """
    if not token:
        raise TokenError("no token supplied")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # never trust the header's alg
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=leeway_seconds,  # tolerate small clock skew, nothing more
            options={
                "require": ["exp", "iat", "nbf", "sub", "jti", "iss", "aud"],
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_signature": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signature, wrong audience/issuer, malformed structure.
        # The message is deliberately vague to the client; detail goes to the log.
        raise TokenError("token is not valid") from exc

    if payload.get("typ") != ACCESS_TYPE:
        # Stops a token minted for another purpose being replayed here.
        raise TokenError("wrong token type")

    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise TokenError("subject is not a user id") from exc

    return AccessClaims(
        user_id=user_id,
        jti=payload["jti"],
        session=payload.get("sid", ""),
        issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


def new_refresh_token() -> tuple[str, str]:
    """Return ``(token, token_hash)``.

    Only the hash is stored. A read-only leak of the database — a stolen backup,
    an SQL-injection SELECT, an over-broad support query — therefore yields
    nothing replayable. SHA-256 rather than bcrypt because the input is already
    256 bits of uniform randomness: there is no dictionary to slow down, and the
    lookup happens on every refresh.
    """
    token = secrets.token_urlsafe(REFRESH_BYTES)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_id() -> str:
    """A refresh-token family identifier, shared by every token from one login."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Machine API keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewApiKey:
    """The only moment the full key exists. Show it once, store the hash."""

    full_key: str
    prefix: str
    key_hash: str


def new_api_key() -> NewApiKey:
    """Mint a key shaped ``sk_<prefix>_<secret>``.

    The prefix is stored in clear and indexed, so verification is one indexed
    lookup followed by one hash comparison — no scanning every key row and
    hashing against each.

    The recognisable ``sk_`` shape is intentional: it lets secret scanners spot
    the key if it is ever committed to a repository or pasted into a ticket.
    Making a leaked credential *easy to detect* is worth more than making it
    inconspicuous, because obscurity does not survive a public git history.
    """
    prefix = secrets.token_hex(API_KEY_PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(API_KEY_BYTES)
    full = f"sk_{prefix}_{secret}"
    return NewApiKey(full_key=full, prefix=prefix, key_hash=hash_api_key(full))


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def split_api_key(full_key: str) -> str:
    """Extract the lookup prefix, or raise if the key is not our shape."""
    parts = full_key.split("_", 2)
    if len(parts) != 3 or parts[0] != "sk" or not parts[1] or not parts[2]:
        raise TokenError("malformed API key")
    return parts[1]
