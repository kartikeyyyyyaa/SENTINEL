"""Plate normalisation — the evidential-integrity boundary of ANPR.

``normalise`` is four lines of code guarding one of the most consequential
decisions in the whole system: it upper-cases and strips separators and does
**nothing else**. The temptation it resists — "fixing" the classic OCR
confusions (O/0, I/1, S/5, B/8) using the Indian plate format — would raise the
apparent match rate and fabricate evidence, because a substituted character is
a character the machine did not read. Most of the tests below are that negative
property: proof the function does *not* do the helpful-but-wrong thing. A
round-trip test would show the code works; only these show it is safe.
"""
from __future__ import annotations

import unittest

from services.analytics.stages.plate import normalise


class Normalise(unittest.TestCase):
    def test_uppercases_and_strips_separators(self):
        self.assertEqual(normalise("gj-01-ab-1234"), "GJ01AB1234")

    def test_strips_interior_spaces(self):
        # This is the exact shape StubOcrReader emits ("GJ 01 AB 1234").
        self.assertEqual(normalise("GJ 01 AB 1234"), "GJ01AB1234")

    def test_empty_input_is_empty_output(self):
        self.assertEqual(normalise(""), "")

    def test_strips_non_alphanumeric_including_unicode(self):
        self.assertEqual(normalise("GJ·01·AB·1234"), "GJ01AB1234")
        self.assertEqual(normalise("GJ01AB1234\n"), "GJ01AB1234")

    def test_does_not_substitute_letter_O_for_digit_zero(self):
        # The core evidential-integrity property. If OCR read an 'O' where the
        # format "wants" a '0', normalise must leave the O intact rather than
        # rewrite the read to match the expected format. A rewritten character
        # is a fabricated one, and the operator would have no way to tell.
        self.assertEqual(normalise("GJO1AB1234"), "GJO1AB1234")
        self.assertNotEqual(normalise("GJO1AB1234"), "GJ01AB1234")

    def test_does_not_substitute_letter_I_for_digit_one(self):
        self.assertEqual(normalise("GJ0IAB1234"), "GJ0IAB1234")

    def test_does_not_substitute_S_for_5_or_B_for_8(self):
        self.assertEqual(normalise("SB1234"), "SB1234")  # not "581234"

    def test_is_idempotent(self):
        once = normalise("gj 01 ab 1234")
        self.assertEqual(normalise(once), once)

    def test_lowercase_letters_are_preserved_as_uppercase_not_dropped(self):
        # A guard against a regex that only kept [A-Z0-9] *without* upper-casing
        # first, which would silently delete every lowercase read.
        self.assertEqual(normalise("gjabcd"), "GJABCD")


if __name__ == "__main__":
    unittest.main()
