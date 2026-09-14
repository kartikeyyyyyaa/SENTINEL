"""Tokens: signing, verification, and the forgeries that must fail."""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone

import jwt

from app.core import tokens
from app.core.tokens import TokenError

SECRET = "test-secret-not-used-anywhere-real-0123456789"
OTHER_SECRET = "a-different-secret-entirely-9876543210"


class AccessTokens(unittest.TestCase):
    def test_issue_and_verify(self) -> None:
        session = tokens.new_session_id()
        token, claims = tokens.issue_access_token(
            secret=SECRET, user_id=42, session=session, ttl_minutes=15
        )
        verified = tokens.verify_access_token(token, secret=SECRET)
        self.assertEqual(verified.user_id, 42)
        self.assertEqual(verified.session, session)
        self.assertEqual(verified.jti, claims.jti)

    def test_each_token_has_a_unique_jti(self) -> None:
        jtis = {
            tokens.issue_access_token(
                secret=SECRET, user_id=1, session="s", ttl_minutes=15
            )[1].jti
            for _ in range(200)
        }
        self.assertEqual(len(jtis), 200)

    def test_carries_a_kid_header_for_future_key_rotation(self) -> None:
        token, _ = tokens.issue_access_token(
            secret=SECRET, user_id=1, session="s", ttl_minutes=15, key_id="v2"
        )
        self.assertEqual(jwt.get_unverified_header(token).get("kid"), "v2")

    def test_carries_no_authorisation_claims(self) -> None:
        """The token must not be a capability.

        If permissions, department or jurisdiction rode inside the token, they
        would survive revocation for the token's whole lifetime. The database is
        the authority; the token only says *who*.
        """
        token, _ = tokens.issue_access_token(
            secret=SECRET, user_id=1, session="s", ttl_minutes=15
        )
        payload = jwt.decode(token, SECRET, algorithms=["HS256"], audience=tokens.AUDIENCE)
        for forbidden in (
            "permissions",
            "perms",
            "roles",
            "role",
            "department_id",
            "jurisdiction",
            "jurisdiction_path",
            "is_statewide",
            "scope",
        ):
            self.assertNotIn(forbidden, payload)
        self.assertEqual(set(payload) , {"sub", "jti", "sid", "typ", "iss", "aud", "iat", "nbf", "exp"})

    def test_rejects_empty_and_garbage(self) -> None:
        for bad in ("", "not-a-token", "a.b.c", "..."):
            with self.subTest(bad=bad), self.assertRaises(TokenError):
                tokens.verify_access_token(bad, secret=SECRET)

    def test_rejects_wrong_secret(self) -> None:
        token, _ = tokens.issue_access_token(
            secret=SECRET, user_id=1, session="s", ttl_minutes=15
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(token, secret=OTHER_SECRET)

    def test_rejects_expired(self) -> None:
        token, _ = tokens.issue_access_token(
            secret=SECRET, user_id=1, session="s", ttl_minutes=-1
        )
        with self.assertRaises(TokenError) as ctx:
            tokens.verify_access_token(token, secret=SECRET, leeway_seconds=0)
        self.assertIn("expired", str(ctx.exception))

    def test_rejects_alg_none_forgery(self) -> None:
        """The canonical JWT attack: strip the signature and set alg to none.

        Works against any verifier that trusts the header's algorithm. We pin
        algorithms=["HS256"], so it cannot.
        """
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": tokens.ISSUER,
                "aud": tokens.AUDIENCE,
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            key="",
            algorithm=None,
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_token_signed_with_a_different_algorithm(self) -> None:
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": tokens.ISSUER,
                "aud": tokens.AUDIENCE,
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            SECRET,
            algorithm="HS512",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_missing_expiry(self) -> None:
        """A token with no exp must not be treated as valid forever."""
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": tokens.ISSUER,
                "aud": tokens.AUDIENCE,
                "iat": int(time.time()),
                "nbf": int(time.time()),
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_wrong_audience(self) -> None:
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": tokens.ISSUER,
                "aud": "some-other-service",
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_wrong_issuer(self) -> None:
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": "somebody-else",
                "aud": tokens.AUDIENCE,
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_wrong_token_type(self) -> None:
        """A token minted for another purpose must not be replayable here."""
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "password_reset",
                "iss": tokens.ISSUER,
                "aud": tokens.AUDIENCE,
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_non_numeric_subject(self) -> None:
        forged = jwt.encode(
            {
                "sub": "'; DROP TABLE app.camera; --",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": tokens.ISSUER,
                "aud": tokens.AUDIENCE,
                "iat": int(time.time()),
                "nbf": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_rejects_not_yet_valid(self) -> None:
        future = int(time.time()) + 600
        forged = jwt.encode(
            {
                "sub": "1",
                "jti": "x",
                "sid": "s",
                "typ": "access",
                "iss": tokens.ISSUER,
                "aud": tokens.AUDIENCE,
                "iat": future,
                "nbf": future,
                "exp": future + 3600,
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            tokens.verify_access_token(forged, secret=SECRET)

    def test_payload_tamper_breaks_the_signature(self) -> None:
        """Escalate by editing the subject: sub 42 -> sub 1 (the state admin)."""
        import base64

        token, _ = tokens.issue_access_token(
            secret=SECRET, user_id=42, session="s", ttl_minutes=15
        )
        header_b64, payload_b64, sig = token.split(".")

        def b64d(s: str) -> bytes:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

        def b64e(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

        payload = json.loads(b64d(payload_b64))
        payload["sub"] = "1"
        tampered = f"{header_b64}.{b64e(json.dumps(payload).encode())}.{sig}"

        with self.assertRaises(TokenError):
            tokens.verify_access_token(tampered, secret=SECRET)


class RefreshTokens(unittest.TestCase):
    def test_token_and_hash_are_consistent(self) -> None:
        token, digest = tokens.new_refresh_token()
        self.assertEqual(tokens.hash_refresh_token(token), digest)

    def test_hash_does_not_reveal_the_token(self) -> None:
        token, digest = tokens.new_refresh_token()
        self.assertNotIn(token, digest)
        self.assertEqual(len(digest), 64)

    def test_tokens_are_unique_and_long(self) -> None:
        seen = {tokens.new_refresh_token()[0] for _ in range(500)}
        self.assertEqual(len(seen), 500)
        # 32 random bytes, urlsafe-base64 encoded.
        self.assertGreaterEqual(len(next(iter(seen))), 40)

    def test_session_ids_are_unique(self) -> None:
        self.assertEqual(len({tokens.new_session_id() for _ in range(500)}), 500)


class ApiKeys(unittest.TestCase):
    def test_shape_and_hash(self) -> None:
        key = tokens.new_api_key()
        self.assertTrue(key.full_key.startswith("sk_"))
        self.assertEqual(tokens.split_api_key(key.full_key), key.prefix)
        self.assertEqual(tokens.hash_api_key(key.full_key), key.key_hash)

    def test_prefix_alone_does_not_authenticate(self) -> None:
        key = tokens.new_api_key()
        self.assertNotEqual(tokens.hash_api_key(f"sk_{key.prefix}_"), key.key_hash)

    def test_keys_are_unique(self) -> None:
        keys = [tokens.new_api_key() for _ in range(200)]
        self.assertEqual(len({k.full_key for k in keys}), 200)
        self.assertEqual(len({k.prefix for k in keys}), 200)

    def test_malformed_keys_are_rejected(self) -> None:
        for bad in ("", "sk_", "sk_abc", "abc_def_ghi", "pk_abc_def", "sk__def", "sk_abc_"):
            with self.subTest(bad=bad), self.assertRaises(TokenError):
                tokens.split_api_key(bad)


if __name__ == "__main__":
    unittest.main()
