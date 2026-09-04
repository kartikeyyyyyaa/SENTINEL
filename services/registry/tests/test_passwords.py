"""Password hashing, policy, and the lockout state machine."""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone

from app.core import passwords


class Hashing(unittest.TestCase):
    def test_round_trip(self) -> None:
        h = passwords.hash_password("correct horse battery staple")
        self.assertTrue(passwords.verify_password("correct horse battery staple", h))
        self.assertFalse(passwords.verify_password("wrong horse battery staple", h))

    def test_salted_so_equal_passwords_hash_differently(self) -> None:
        a = passwords.hash_password("same-password")
        b = passwords.hash_password("same-password")
        self.assertNotEqual(a, b)
        self.assertTrue(passwords.verify_password("same-password", a))
        self.assertTrue(passwords.verify_password("same-password", b))

    def test_long_passwords_are_not_truncated_at_72_bytes(self) -> None:
        """Bare bcrypt ignores bytes past 72, so these two would collide."""
        base = "A" * 72
        h = passwords.hash_password(base + "-tail-one")
        self.assertTrue(passwords.verify_password(base + "-tail-one", h))
        self.assertFalse(passwords.verify_password(base + "-tail-two", h))
        self.assertFalse(passwords.verify_password(base, h))

    def test_nul_byte_does_not_truncate(self) -> None:
        """Some bcrypt builds stop at the first NUL. Pre-hashing removes the risk."""
        h = passwords.hash_password("abc\x00secret-tail")
        self.assertTrue(passwords.verify_password("abc\x00secret-tail", h))
        self.assertFalse(passwords.verify_password("abc", h))
        self.assertFalse(passwords.verify_password("abc\x00other-tail", h))

    def test_unicode_passwords_work(self) -> None:
        h = passwords.hash_password("પાસવર્ડ-૧૨૩-ગુજરાત")
        self.assertTrue(passwords.verify_password("પાસવર્ડ-૧૨૩-ગુજરાત", h))

    def test_missing_hash_is_a_failure_not_a_crash(self) -> None:
        self.assertFalse(passwords.verify_password("anything", None))
        self.assertFalse(passwords.verify_password("anything", ""))

    def test_corrupt_stored_hash_is_a_failure_not_a_crash(self) -> None:
        self.assertFalse(passwords.verify_password("anything", "not-a-bcrypt-hash"))

    def test_unknown_user_costs_the_same_as_a_wrong_password(self) -> None:
        """Account-enumeration oracle check.

        If "no such user" returned before doing any bcrypt work, its latency
        would be an order of magnitude lower than a real comparison and an
        attacker could enumerate valid usernames. Both paths must pay for one
        bcrypt round.
        """
        real = passwords.hash_password("some-real-password")

        def timed(fn, n=5) -> float:
            start = time.perf_counter()
            for _ in range(n):
                fn()
            return (time.perf_counter() - start) / n

        wrong_password = timed(lambda: passwords.verify_password("guess", real))
        no_such_user = timed(lambda: passwords.verify_password("guess", None))

        # Generous bound: we care that the missing-user path is not ~free, not
        # that the two are identical to the microsecond.
        self.assertGreater(
            no_such_user,
            wrong_password * 0.5,
            f"unknown-user path is suspiciously fast: {no_such_user:.4f}s "
            f"vs {wrong_password:.4f}s — enumeration oracle",
        )

    def test_needs_rehash_detects_weaker_cost(self) -> None:
        weak = "$2b$04$" + "x" * 53
        self.assertTrue(passwords.needs_rehash(weak))
        self.assertFalse(passwords.needs_rehash(passwords.hash_password("x" * 14)))
        self.assertTrue(passwords.needs_rehash("garbage"))
        self.assertFalse(passwords.needs_rehash(None))


class Policy(unittest.TestCase):
    def test_accepts_a_reasonable_passphrase(self) -> None:
        self.assertTrue(passwords.check_policy("Monsoon-Bridge-77").ok)

    def test_accepts_a_long_all_lowercase_passphrase(self) -> None:
        # 20+ characters is enough entropy without character-class gymnastics.
        result = passwords.check_policy("seven rivers meet the sea")
        self.assertTrue(result.ok, result.problems)

    def test_rejects_short(self) -> None:
        result = passwords.check_policy("Sh0rt!")
        self.assertFalse(result.ok)
        self.assertTrue(any("at least" in p for p in result.problems))

    def test_rejects_absurdly_long_input(self) -> None:
        self.assertFalse(passwords.check_policy("a" * 5000).ok)

    def test_rejects_deployment_specific_words(self) -> None:
        for bad in ("GujaratPolice@2026", "Sentinel-Admin-99", "MyPassword123!"):
            with self.subTest(bad=bad):
                self.assertFalse(passwords.check_policy(bad).ok)

    def test_rejects_password_containing_username(self) -> None:
        result = passwords.check_policy("Rajkot-operator-2026", username="operator")
        self.assertFalse(result.ok)
        self.assertTrue(any("username" in p for p in result.problems))

    def test_rejects_low_variety_short_password(self) -> None:
        self.assertFalse(passwords.check_policy("abababababab").ok)

    def test_rejects_single_repeated_character(self) -> None:
        self.assertFalse(passwords.check_policy("aaaaaaaaaaaaaaaa").ok)

    def test_short_username_is_not_matched(self) -> None:
        # A 2-3 char username would otherwise ban most passwords containing it.
        result = passwords.check_policy("Monsoon-Bridge-77", username="ab")
        self.assertTrue(result.ok, result.problems)


class Lockout(unittest.TestCase):
    """Pure state machine, so every branch is cheap to pin down."""

    def setUp(self) -> None:
        self.now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.kw = dict(max_failures=5, lockout_minutes=15, now=self.now)

    def test_first_failure_counts_but_does_not_lock(self) -> None:
        d = passwords.evaluate_lockout(
            failed_attempts=0, locked_until=None, success=False, **self.kw
        )
        self.assertFalse(d.locked)
        self.assertEqual(d.failed_attempts, 1)

    def test_locks_on_reaching_the_threshold(self) -> None:
        d = passwords.evaluate_lockout(
            failed_attempts=4, locked_until=None, success=False, **self.kw
        )
        self.assertTrue(d.locked)
        self.assertEqual(d.failed_attempts, 5)
        self.assertEqual(d.locked_until, self.now + timedelta(minutes=15))

    def test_does_not_lock_one_short_of_the_threshold(self) -> None:
        d = passwords.evaluate_lockout(
            failed_attempts=3, locked_until=None, success=False, **self.kw
        )
        self.assertFalse(d.locked)
        self.assertEqual(d.failed_attempts, 4)

    def test_success_clears_the_counter(self) -> None:
        d = passwords.evaluate_lockout(
            failed_attempts=4, locked_until=None, success=True, **self.kw
        )
        self.assertFalse(d.locked)
        self.assertEqual(d.failed_attempts, 0)
        self.assertTrue(d.reset_counter)

    def test_correct_password_during_an_active_lock_still_fails(self) -> None:
        """Otherwise the lock is decorative: an attacker who eventually guesses
        right would be let in mid-lockout."""
        d = passwords.evaluate_lockout(
            failed_attempts=5,
            locked_until=self.now + timedelta(minutes=5),
            success=True,
            **self.kw,
        )
        self.assertTrue(d.locked)

    def test_lock_does_not_extend_on_further_attempts(self) -> None:
        """A lock that renews on every attempt lets anyone keep a user out
        indefinitely by hammering the endpoint."""
        expiry = self.now + timedelta(minutes=5)
        d = passwords.evaluate_lockout(
            failed_attempts=5, locked_until=expiry, success=False, **self.kw
        )
        self.assertEqual(d.locked_until, expiry)

    def test_expired_lock_lets_a_correct_password_through(self) -> None:
        d = passwords.evaluate_lockout(
            failed_attempts=5,
            locked_until=self.now - timedelta(minutes=1),
            success=True,
            **self.kw,
        )
        self.assertFalse(d.locked)
        self.assertEqual(d.failed_attempts, 0)

    def test_failure_after_an_expired_lock_starts_a_fresh_count(self) -> None:
        """Without this, the counter creeps upward across days and locks someone
        out on their first typo of the week."""
        d = passwords.evaluate_lockout(
            failed_attempts=5,
            locked_until=self.now - timedelta(hours=1),
            success=False,
            **self.kw,
        )
        self.assertFalse(d.locked)
        self.assertEqual(d.failed_attempts, 1)

    def test_naive_locked_until_is_treated_as_utc(self) -> None:
        d = passwords.evaluate_lockout(
            failed_attempts=5,
            locked_until=(self.now + timedelta(minutes=5)).replace(tzinfo=None),
            success=False,
            **self.kw,
        )
        self.assertTrue(d.locked)

    def test_five_wrong_then_wait_then_right(self) -> None:
        """End-to-end walk through the state machine."""
        attempts, locked_until = 0, None
        for _ in range(5):
            d = passwords.evaluate_lockout(
                failed_attempts=attempts, locked_until=locked_until, success=False, **self.kw
            )
            attempts, locked_until = d.failed_attempts, d.locked_until
        self.assertTrue(d.locked)

        later = dict(self.kw, now=self.now + timedelta(minutes=16))
        d = passwords.evaluate_lockout(
            failed_attempts=attempts, locked_until=locked_until, success=True, **later
        )
        self.assertFalse(d.locked)
        self.assertEqual(d.failed_attempts, 0)


class ConstantTime(unittest.TestCase):
    def test_compares_correctly(self) -> None:
        self.assertTrue(passwords.constant_time_equals("abc", "abc"))
        self.assertFalse(passwords.constant_time_equals("abc", "abd"))
        self.assertFalse(passwords.constant_time_equals("abc", "abcd"))
        self.assertTrue(passwords.constant_time_equals("", ""))

    def test_handles_unicode(self) -> None:
        self.assertTrue(passwords.constant_time_equals("ગુજરાત", "ગુજરાત"))
        self.assertFalse(passwords.constant_time_equals("ગુજરાત", "गुजरात"))


if __name__ == "__main__":
    unittest.main()
