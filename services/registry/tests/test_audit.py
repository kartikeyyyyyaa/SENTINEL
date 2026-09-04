"""The audit chain: format parity with SQL, and tamper detection.

The tests that matter most here are the *format* tests. If the Python payload
diverges from the SQL payload by a single character, the independent verifier
declares a perfectly good chain to be forged — and a tamper-evident log that
cries wolf is worse than none, because the first thing anyone learns is to ignore
it. So the field order, the timestamp rendering and the NULL handling are all
pinned to literal expected strings rather than checked against another
implementation.
"""
from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from app.core import audit
from app.core.audit import AuditRecord, Outcome


def _chain(records: list[AuditRecord]) -> list[AuditRecord]:
    """Link records the way the database trigger would."""
    prev: str | None = None
    for rec in records:
        rec.prev_hash = prev
        rec.row_hash = rec.compute_hash(prev)
        prev = rec.row_hash
    return records


def _record(i: int, **kw) -> AuditRecord:
    base = dict(
        id=i,
        at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=i),
        action=audit.AuditAction.CAMERA_VIEW,
        actor_user_id=7,
        actor_username="operator.ahd",
        actor_ip="10.20.30.40",
        resource_type="camera",
        resource_id=str(1000 + i),
        outcome=Outcome.SUCCESS,
    )
    base.update(kw)
    return AuditRecord(**base)  # type: ignore[arg-type]


class PayloadFormat(unittest.TestCase):
    """Pinned to literals. These are the SQL contract."""

    def test_timestamp_matches_postgres_to_char(self) -> None:
        # to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')
        at = datetime(2026, 9, 7, 4, 5, 6, 78901, tzinfo=timezone.utc)
        self.assertEqual(audit.format_timestamp(at), "2026-09-07 04:05:06.078901")

    def test_timestamp_pads_microseconds_to_six_digits(self) -> None:
        at = datetime(2026, 1, 2, 3, 4, 5, 7, tzinfo=timezone.utc)
        self.assertEqual(audit.format_timestamp(at), "2026-01-02 03:04:05.000007")

    def test_timestamp_with_zero_microseconds_keeps_six_zeros(self) -> None:
        # .US always emits six digits; a naive isoformat() would drop them.
        at = datetime(2026, 1, 2, 3, 4, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(audit.format_timestamp(at), "2026-01-02 03:04:05.000000")

    def test_timestamp_is_converted_to_utc_not_truncated(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        at = datetime(2026, 9, 7, 9, 35, 6, 0, tzinfo=ist)
        self.assertEqual(audit.format_timestamp(at), "2026-09-07 04:05:06.000000")

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        at = datetime(2026, 9, 7, 4, 5, 6, 0)
        self.assertEqual(audit.format_timestamp(at), "2026-09-07 04:05:06.000000")

    def test_ip_matches_postgres_host_function(self) -> None:
        self.assertEqual(audit.format_ip("10.0.0.1"), "10.0.0.1")
        self.assertEqual(audit.format_ip("10.0.0.1/32"), "10.0.0.1")
        self.assertEqual(audit.format_ip("192.168.1.0/24"), "192.168.1.0")
        self.assertEqual(audit.format_ip(None), "")
        self.assertEqual(audit.format_ip(""), "")
        # host() abbreviates IPv6 exactly as Python does.
        self.assertEqual(audit.format_ip("2001:0db8:0000:0000:0000:0000:0000:0001"), "2001:db8::1")
        self.assertEqual(audit.format_ip("::1/128"), "::1")

    def test_unparseable_ip_is_preserved_verbatim(self) -> None:
        # Never silently drop a value that is already in the trail.
        self.assertEqual(audit.format_ip("not-an-ip"), "not-an-ip")

    def test_nulls_become_empty_strings_in_field_order(self) -> None:
        rec = AuditRecord(
            id=1,
            at=datetime(2026, 9, 7, 4, 5, 6, tzinfo=timezone.utc),
            action="auth.login.failed",
            outcome=Outcome.DENIED,
        )
        # Three NULL actor fields (user_id, username, ip) sit between the
        # timestamp and the action, so four consecutive separators.
        self.assertEqual(
            rec.payload(None),
            "GENESIS|1|2026-09-07 04:05:06.000000||||auth.login.failed|||||denied|",
        )

    def test_full_payload_field_order(self) -> None:
        rec = AuditRecord(
            id=42,
            at=datetime(2026, 9, 7, 4, 5, 6, tzinfo=timezone.utc),
            action="camera.credential.read",
            outcome=Outcome.SUCCESS,
            actor_user_id=7,
            actor_username="insp.patel",
            actor_ip="10.1.2.3",
            resource_type="camera",
            resource_id="1234",
            purpose="investigation",
            case_reference="FIR/2026/00871",
            detail='{"reason":"probe"}',
        )
        self.assertEqual(
            rec.payload("abc123"),
            "abc123|42|2026-09-07 04:05:06.000000|7|insp.patel|10.1.2.3|"
            "camera.credential.read|camera|1234|investigation|FIR/2026/00871|"
            'success|{"reason":"probe"}',
        )

    def test_genesis_marker_used_when_no_predecessor(self) -> None:
        rec = _record(1)
        self.assertTrue(rec.payload(None).startswith("GENESIS|"))
        self.assertTrue(rec.payload("").startswith("GENESIS|"))

    def test_known_vector(self) -> None:
        """A frozen test vector, so SQL and Python can be checked against the
        same constant rather than against each other.

        ``db/verify_parity.sql`` asserts Postgres produces this exact hash for
        the same inputs. If either side is edited, one of the two fails.
        """
        rec = AuditRecord(
            id=1,
            at=datetime(2026, 9, 7, 4, 5, 6, tzinfo=timezone.utc),
            action="auth.login.success",
            outcome=Outcome.SUCCESS,
            actor_user_id=1,
            actor_username="state.admin",
            actor_ip="127.0.0.1",
        )
        payload = "GENESIS|1|2026-09-07 04:05:06.000000|1|state.admin|127.0.0.1|auth.login.success|||||success|"
        self.assertEqual(rec.payload(None), payload)
        self.assertEqual(
            rec.compute_hash(None),
            hashlib.sha256(payload.encode()).hexdigest(),
        )
        self.assertEqual(
            rec.compute_hash(None),
            "19ff06c0299c6fc7cc8d930d67ef45636ba2733a1b31cbe7022f718dd60c3d59",
        )


class CanonicalJson(unittest.TestCase):
    def test_key_order_does_not_affect_output(self) -> None:
        a = audit.canonical_json({"b": 2, "a": 1, "c": {"z": 1, "y": 2}})
        b = audit.canonical_json({"c": {"y": 2, "z": 1}, "a": 1, "b": 2})
        self.assertEqual(a, b)
        self.assertEqual(a, '{"a":1,"b":2,"c":{"y":2,"z":1}}')

    def test_no_incidental_whitespace(self) -> None:
        self.assertEqual(audit.canonical_json({"a": [1, 2]}), '{"a":[1,2]}')

    def test_none_stays_none(self) -> None:
        self.assertIsNone(audit.canonical_json(None))

    def test_non_ascii_is_not_escaped(self) -> None:
        # Place names must stay legible in psql and in a court exhibit.
        self.assertEqual(audit.canonical_json({"city": "અમદાવાદ"}), '{"city":"અમદાવાદ"}')

    def test_unserialisable_values_do_not_raise(self) -> None:
        # An audit write must never fail because someone put a datetime in detail.
        out = audit.canonical_json({"at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
        self.assertIn("2026-01-01", out or "")


class ChainVerification(unittest.TestCase):
    def test_intact_chain_verifies(self) -> None:
        rows = _chain([_record(i) for i in range(1, 21)])
        verdict = audit.verify_chain(rows)
        self.assertTrue(verdict.is_intact, verdict.reason)
        self.assertEqual(verdict.checked_rows, 20)
        self.assertEqual(verdict.verified_range, (1, 20))

    def test_empty_chain_is_intact(self) -> None:
        verdict = audit.verify_chain([])
        self.assertTrue(verdict.is_intact)
        self.assertEqual(verdict.checked_rows, 0)

    def test_edited_field_is_detected_and_located(self) -> None:
        rows = _chain([_record(i) for i in range(1, 11)])
        # The classic cover-up: change who did it.
        rows[4].actor_username = "somebody.else"
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 5)
        self.assertIn("contents were altered", verdict.reason or "")

    def test_edited_outcome_is_detected(self) -> None:
        rows = _chain([_record(i) for i in range(1, 6)])
        rows[2].outcome = Outcome.DENIED  # rewriting success as denial, or vice versa
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 3)

    def test_edited_detail_is_detected(self) -> None:
        rows = _chain([_record(i, detail='{"n":1}') for i in range(1, 6)])
        rows[1].detail = '{"n":2}'
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 2)

    def test_backdating_a_row_is_detected(self) -> None:
        rows = _chain([_record(i) for i in range(1, 6)])
        rows[3].at = rows[3].at - timedelta(days=30)
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 4)

    def test_deleted_row_is_detected(self) -> None:
        """Removing an inconvenient row breaks the *link*, not the contents."""
        rows = _chain([_record(i) for i in range(1, 11)])
        del rows[4]
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 6)  # the row that followed the gap
        self.assertIn("removed or reordered", verdict.reason or "")

    def test_reordered_rows_are_detected(self) -> None:
        rows = _chain([_record(i) for i in range(1, 11)])
        rows[3], rows[4] = rows[4], rows[3]
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)

    def test_wholesale_rehash_after_edit_still_breaks_the_link(self) -> None:
        """The sophisticated attempt: edit a row *and* recompute its own hash.

        Recomputing one row's hash makes that row self-consistent, but its
        successor still stores the *old* prev_hash, so the break simply moves one
        row down the chain. Repairing the whole tail is only possible for someone
        who can rewrite every subsequent row — which is exactly what the
        append-only triggers and revoked UPDATE/DELETE privileges prevent.
        """
        rows = _chain([_record(i) for i in range(1, 11)])
        rows[4].resource_id = "999999"
        rows[4].row_hash = rows[4].compute_hash(rows[4].prev_hash)  # self-consistent now

        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 6)
        self.assertIn("removed or reordered", verdict.reason or "")

    def test_appending_a_forged_row_is_detected(self) -> None:
        rows = _chain([_record(i) for i in range(1, 6)])
        forged = _record(6, action="camera.delete")
        forged.prev_hash = rows[-1].row_hash
        forged.row_hash = "0" * 64  # attacker cannot compute it without the format
        rows.append(forged)
        verdict = audit.verify_chain(rows)
        self.assertFalse(verdict.is_intact)
        self.assertEqual(verdict.broken_at_id, 6)

    def test_partial_range_adopts_stored_prev_hash(self) -> None:
        """Verifying a slice must work, since exports are usually date-ranged."""
        rows = _chain([_record(i) for i in range(1, 21)])
        verdict = audit.verify_chain(rows[10:])
        self.assertTrue(verdict.is_intact, verdict.reason)
        self.assertEqual(verdict.checked_rows, 10)
        self.assertEqual(verdict.verified_range, (11, 20))

    def test_verification_works_from_string_timestamps(self) -> None:
        """An examiner verifying from a CSV export has strings, not datetimes."""
        rows = _chain([_record(i) for i in range(1, 6)])
        as_export = [
            {
                "id": r.id,
                "at": r.at.isoformat(),
                "action": r.action,
                "outcome": r.outcome,
                "actor_user_id": r.actor_user_id,
                "actor_username": r.actor_username,
                "actor_ip": r.actor_ip,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "purpose": r.purpose,
                "case_reference": r.case_reference,
                "detail": r.detail,
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
            }
            for r in rows
        ]
        verdict = audit.verify_chain(as_export)
        self.assertTrue(verdict.is_intact, verdict.reason)
        self.assertEqual(verdict.checked_rows, 5)


if __name__ == "__main__":
    unittest.main()
