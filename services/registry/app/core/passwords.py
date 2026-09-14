"""Password hashing and login-attempt policy.

bcrypt, cost 12. Not because bcrypt is the strongest available — Argon2id is —
but because ``bcrypt`` is a two-megabyte wheel with no build step, while
``argon2-cffi`` pulls a C toolchain into the image. With a hard deadline and a
fleet of machines we do not control, a dependency that fails to install is a
worse outcome than a KDF that is merely very good. Cost 12 puts a single guess at
roughly a quarter of a second on commodity hardware, which combined with the
lockout below makes online guessing hopeless.

**The 72-byte problem.** bcrypt silently ignores everything past byte 72 of its
input. Left alone, that means a 100-character passphrase is no stronger than its
first 72 bytes, and — worse — some bcrypt builds truncate at the first NUL byte,
so a password of "abc\\0<anything>" verifies against "abc". Both are avoided the
standard way: SHA-256 the password first and base64 the digest, yielding a fixed
44-byte, NUL-free input. Length is then unbounded from the user's point of view
and no truncation can occur.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt

BCRYPT_ROUNDS = 12

# A dummy hash of a random password, used to keep the timing of a login attempt
# against a nonexistent user indistinguishable from one against a real user.
# Computed once at import.
_DUMMY_HASH: bytes = bcrypt.hashpw(
    base64.b64encode(hashlib.sha256(secrets.token_bytes(32)).digest()),
    bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
)


def _prepare(password: str) -> bytes:
    """Fixed-length, NUL-free bcrypt input. See the module docstring."""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Check a password. Constant-ish time whether or not the user exists.

    When ``stored_hash`` is None — no such user, or an account with no password
    set — we still perform a full bcrypt comparison against a dummy hash before
    returning False. Skipping it would make "unknown username" measurably faster
    than "wrong password", handing an attacker a free account-enumeration oracle
    on a government system. The wasted quarter-second is the entire point.
    """
    candidate = _prepare(password)
    if not stored_hash:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(candidate, stored_hash.encode("ascii"))
    except ValueError:
        # Malformed hash in the database. Treat as a failure, never as a pass.
        return False


def needs_rehash(stored_hash: str | None) -> bool:
    """True if a stored hash was made with a weaker cost than we now use.

    Called after a successful login so cost can be raised over the life of the
    system without forcing a password reset on everyone.
    """
    if not stored_hash:
        return False
    m = re.match(r"^\$2[aby]\$(\d{2})\$", stored_hash)
    if not m:
        return True
    return int(m.group(1)) < BCRYPT_ROUNDS


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

# Passwords that would pass a naive "12 chars, mixed case, digit, symbol" rule
# while being among the first things any attacker tries. Deliberately short and
# targeted at this deployment rather than a generic top-10k list.
_BANNED_SUBSTRINGS = (
    "password",
    "sentinel",
    "gujarat",
    "police",
    "cctv",
    "admin",
    "welcome",
    "qwerty",
    "123456",
    "letmein",
    "changeme",
)

MIN_LENGTH = 12
MAX_LENGTH = 256  # Bound the input so nobody can post a 10 MB "password".


@dataclass(frozen=True, slots=True)
class PolicyResult:
    ok: bool
    problems: tuple[str, ...] = ()


def check_policy(password: str, *, username: str | None = None) -> PolicyResult:
    """Validate a new password.

    Length is weighted far more heavily than character-class rules, which mostly
    teach people to write ``Password1!``. A long passphrase clears this easily;
    a short scrambled string does not.
    """
    problems: list[str] = []

    if len(password) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters")

    lowered = password.lower()
    for banned in _BANNED_SUBSTRINGS:
        if banned in lowered:
            problems.append(f"must not contain the common phrase '{banned}'")
            break

    if username and len(username) >= 4 and username.lower() in lowered:
        problems.append("must not contain your username")

    # Character variety, but only as a fallback for shortish passwords. Anything
    # 20+ characters long has enough entropy from length alone.
    if len(password) < 20:
        classes = sum(
            bool(re.search(pattern, password))
            for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
        )
        if classes < 3:
            problems.append(
                "must mix at least three of: lowercase, uppercase, digits, symbols "
                "(or be 20+ characters long)"
            )

    if len(set(password)) <= 4 and len(password) >= MIN_LENGTH:
        problems.append("must not repeat only a handful of characters")

    return PolicyResult(ok=not problems, problems=tuple(problems))


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockoutDecision:
    locked: bool
    locked_until: datetime | None
    failed_attempts: int
    reset_counter: bool


def evaluate_lockout(
    *,
    failed_attempts: int,
    locked_until: datetime | None,
    success: bool,
    max_failures: int,
    lockout_minutes: int,
    now: datetime | None = None,
) -> LockoutDecision:
    """Decide the new lockout state after one login attempt.

    Pure function of the current counters, so it is unit-testable without a
    database or a clock. The caller persists the result.

    Lockout is time-boxed rather than permanent: a permanent lock on a police
    account turns a trivial denial-of-service into an operational incident during
    an emergency, since anyone who knows a username can lock its owner out. A
    fifteen-minute window makes brute force arithmetically hopeless while keeping
    the worst case "wait fifteen minutes", not "call the state administrator at
    2am".
    """
    now = now or datetime.now(timezone.utc)

    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)

    # An expired lock is over, whatever the attempt's outcome.
    currently_locked = locked_until is not None and locked_until > now

    if currently_locked:
        return LockoutDecision(
            locked=True,
            locked_until=locked_until,
            failed_attempts=failed_attempts,
            reset_counter=False,
        )

    if success:
        return LockoutDecision(
            locked=False, locked_until=None, failed_attempts=0, reset_counter=True
        )

    # A failure after the lock expired starts a fresh count rather than
    # continuing the old one, so the counter cannot creep upward across days and
    # lock someone out on their first typo of the week.
    attempts = (0 if locked_until is not None else failed_attempts) + 1

    if attempts >= max_failures:
        return LockoutDecision(
            locked=True,
            locked_until=now + timedelta(minutes=lockout_minutes),
            failed_attempts=attempts,
            reset_counter=False,
        )

    return LockoutDecision(
        locked=False, locked_until=None, failed_attempts=attempts, reset_counter=False
    )


def constant_time_equals(a: str, b: str) -> bool:
    """For comparing tokens and API keys, where a timing leak reveals a prefix."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
