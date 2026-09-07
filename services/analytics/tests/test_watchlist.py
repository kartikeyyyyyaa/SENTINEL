"""The watchlist index — the lookup every plate and face is matched against.

Two properties matter most and both are tested as negatives:

* **No fuzzy plate matching.** A plate one character off must *not* match a
  watchlist entry. The whole reason ``normalise`` refuses OCR-confusion
  substitution (see ``test_plate``) would be undone if the index then matched
  approximately — a fabricated match is worse than a missed one.
* **A person entry with no embedding is skipped, not fatal.** A freshly filed
  missing-person report awaiting photo enrolment is a normal, expected row; one
  such row must never take down the periodic refresh for every other entry.

Everything here runs against ``StubFaceMatcher`` embeddings, which carry no real
facial signal — they exist to prove the matching/threshold plumbing, exactly as
the stub OCR reader does for plates.
"""
from __future__ import annotations

import unittest

from services.analytics.stages.face import StubFaceMatcher, cosine_similarity
from services.analytics.stages.plate import normalise
from services.analytics.watchlist import (
    WatchlistEntry,
    WatchlistIndex,
    entry_from_mapping,
)

import numpy as np


def _vehicle(entry_id: str, plate: str, risk: str = "critical") -> WatchlistEntry:
    return WatchlistEntry(
        entry_id=entry_id,
        entry_type="stolen_vehicle",
        risk_level=risk,
        plate_number=normalise(plate),
        label="test vehicle",
        case_reference="FIR-TEST-1",
    )


class PlateMatching(unittest.TestCase):
    def setUp(self):
        self.index = WatchlistIndex([_vehicle("1", "GJ01AB1234")])

    def test_exact_hit_returns_the_entry(self):
        hit = self.index.match_plate("GJ01AB1234")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.entry_id, "1")

    def test_query_is_normalised_before_lookup(self):
        # An operator-facing read with separators must still match a stored plate.
        self.assertIsNotNone(self.index.match_plate("gj-01-ab-1234"))
        self.assertIsNotNone(self.index.match_plate("GJ 01 AB 1234"))

    def test_miss_returns_none(self):
        self.assertIsNone(self.index.match_plate("GJ99ZZ9999"))

    def test_one_character_off_does_not_match(self):
        # No fuzzy/approximate matching, by design — see module docstring.
        self.assertIsNone(self.index.match_plate("GJ01AB1235"))
        self.assertIsNone(self.index.match_plate("GJ01AB123"))

    def test_empty_query_returns_none(self):
        self.assertIsNone(self.index.match_plate(""))

    def test_replace_rebuilds_wholesale(self):
        # A refresh replaces the list; entries no longer on it stop matching.
        self.index.replace([_vehicle("2", "GJ05CD5678")])
        self.assertIsNone(self.index.match_plate("GJ01AB1234"))
        self.assertIsNotNone(self.index.match_plate("GJ05CD5678"))


class FaceMatching(unittest.TestCase):
    def setUp(self):
        self.matcher = StubFaceMatcher(embed_dim=32)
        # A deterministic crop → a deterministic stub embedding.
        self.crop = np.full((64, 64, 3), 7, dtype=np.uint8)
        self.embedding = self.matcher.embed(self.crop)
        entry = WatchlistEntry(
            entry_id="p1",
            entry_type="wanted_person",
            risk_level="high",
            label="test person",
            embedding=self.embedding,
        )
        self.index = WatchlistIndex([entry], face_threshold=0.90)

    def test_identical_embedding_matches_at_similarity_one(self):
        result = self.index.match_face(self.embedding)
        self.assertIsNotNone(result)
        entry, score = result
        self.assertEqual(entry.entry_id, "p1")
        self.assertAlmostEqual(score, 1.0, places=6)

    def test_unrelated_embedding_below_threshold_does_not_match(self):
        other = self.matcher.embed(np.full((64, 64, 3), 200, dtype=np.uint8))
        # Two unrelated stub embeddings are ~orthogonal, well under 0.90.
        self.assertIsNone(self.index.match_face(other))

    def test_threshold_is_respected(self):
        # Same embedding, but an index whose threshold is above 1.0 can never
        # match — proves the threshold gates rather than being decorative.
        strict = WatchlistIndex(
            [WatchlistEntry(entry_id="p1", entry_type="suspect", embedding=self.embedding)],
            face_threshold=1.01,
        )
        self.assertIsNone(strict.match_face(self.embedding))


class CosineSimilarity(unittest.TestCase):
    def test_identical_vectors_are_one(self):
        self.assertAlmostEqual(cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)), 1.0, places=9)

    def test_orthogonal_vectors_are_zero(self):
        self.assertAlmostEqual(cosine_similarity((1.0, 0.0), (0.0, 1.0)), 0.0, places=9)

    def test_length_mismatch_is_zero_not_crash(self):
        self.assertEqual(cosine_similarity((1.0, 2.0), (1.0,)), 0.0)

    def test_zero_vector_is_zero_not_division_error(self):
        self.assertEqual(cosine_similarity((0.0, 0.0), (1.0, 1.0)), 0.0)


class EntryFromMapping(unittest.TestCase):
    def test_builds_vehicle_and_normalises_plate(self):
        entry = entry_from_mapping(
            {"id": 9, "entry_type": "stolen_vehicle", "plate_number": "gj-01-ab-1234",
             "risk_level": "high", "label": "swift"}
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.entry_id, "9")
        self.assertEqual(entry.plate_number, "GJ01AB1234")

    def test_person_entry_without_embedding_is_skipped_not_fatal(self):
        # The key resilience property: a photo-pending missing-person row is a
        # normal state and must return None (skip + log), never raise and abort
        # the whole refresh for every other entry.
        entry = entry_from_mapping(
            {"id": 10, "entry_type": "missing_person", "risk_level": "high", "label": "no photo yet"}
        )
        self.assertIsNone(entry)

    def test_vehicle_entry_without_plate_is_skipped_not_fatal(self):
        entry = entry_from_mapping(
            {"id": 11, "entry_type": "stolen_vehicle", "risk_level": "high", "label": "no plate"}
        )
        self.assertIsNone(entry)

    def test_accepts_entry_id_or_id_key(self):
        by_entry_id = entry_from_mapping(
            {"entry_id": "e5", "entry_type": "stolen_vehicle", "plate_number": "GJ01AB1234"}
        )
        self.assertIsNotNone(by_entry_id)
        self.assertEqual(by_entry_id.entry_id, "e5")


if __name__ == "__main__":
    unittest.main()
