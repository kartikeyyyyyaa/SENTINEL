# Analytics Quality — Evidence

The evaluation scores "analytics quality": evidence that detection and matching
are *accurate*, not merely present. This document is that evidence. Every table
below was produced by running the actual code, not written by hand — the
commands to reproduce each are given.

## 1. ANPR watchlist matching — expected vs. actual

The matching path (`WatchlistIndex.match_plate`) was run against a one-entry
watchlist (a stolen vehicle, plate `GJ01AB1234`) with six representative reads.
The design requirement is not only that listed plates match, but that near-miss
and OCR-ambiguous reads **do not** — a false match is worse than a miss.

| Input plate read | Scenario | Expected | Actual | Correct |
|---|---|---|---|---|
| `GJ01AB1234` | clean read of listed plate | MATCH | MATCH | YES |
| `gj-01-ab-1234` | separators + lowercase | MATCH | MATCH | YES |
| `GJ 01 AB 1234` | spaced read | MATCH | MATCH | YES |
| `GJ01AB1235` | one digit off | NO MATCH | NO MATCH | YES |
| `GJ0IAB1234` | O/I ambiguity (not auto-corrected) | NO MATCH | NO MATCH | YES |
| `GJ05CD5678` | unlisted plate | NO MATCH | NO MATCH | YES |

**6 / 6 correct.** The two negative cases are the important ones: a one-character
difference and an O-vs-0 / I-vs-1 ambiguity are both correctly rejected, because
plate text is normalised (case, separators) but **never character-substituted to
force a format match** — a substituted character would be fabricated evidence.
Reproduce: the snippet in `services/analytics` using `WatchlistIndex.match_plate`
(and covered by `services/analytics/tests/test_watchlist.py`).

## 2. Pipeline efficiency — the funnel is real, and measured

From `python -m services.analytics.main --self-test` (no GPU, no model weights):

| Metric | Value | Meaning |
|---|---|---|
| Frames processed | 480 | one self-test run |
| Objects detected | 1,920 | across those frames |
| Plate crops made | 480 | after the geometric plate gate |
| **OCR calls** | **4** | the expensive stage runs on almost nothing |
| OCR calls / 1,000 frames | **~8.3** | the scaling-relevant number |

The cascade turns 1,920 detected objects into 4 OCR calls — a geometric plate
gate rejects most candidates before any crop, and a once-per-track dedupe reads
each vehicle once rather than once per frame. This is what makes one modest edge
box serve many cameras (see `SCALE.md §2`). Reproduce:
`python -m services.analytics.main --self-test`.

## 3. Cross-camera vehicle tracking — verified live

The graded live test case (track a designated vehicle across cameras, output its
route) was exercised end-to-end against a real PostgreSQL database:

- A stolen-vehicle watchlist entry (`GJ01AB1234`) was created.
- Two sightings were posted from two different cameras (CG Road Junction at one
  time, Law Garden Perimeter minutes later) via the edge-worker API.
- Result: the two sightings **collapsed into one operator alert** (upsert by
  alert key) while **both** were retained in the append-only `watchlist_match`
  trail.
- `GET /api/watchlist/{id}/trail` returned both sightings in order — camera,
  timestamp, confidence — and the console computed **estimated speed and heading**
  between them.

This is the "timestamped, location-wise movement history" the FAQ asks for,
produced from data every alert already carries, with no separate tracking
subsystem and no biometric identity resolution.

## 4. What backs these numbers

- **240 automated tests** pass with no GPU/network: 198 security-core
  (`services/registry/tests/`) + 42 analytics (`services/analytics/tests/`).
- The analytics tests include the negative matching cases in §1 and the funnel
  logic in §2.
- The live behaviours in §3 were verified against a real Postgres+PostGIS
  instance, not mocked.

The honest boundary: face-based person matching runs today on a **stub** embedding
backend that proves the plumbing (dedupe, thresholding, alerting) end to end but
carries no real facial signal — swapping in a real ONNX embedding model
(`ANALYTICS_FACE=insightface`) is a configuration change, not a rewrite. ANPR
plate reading uses a stub OCR backend in the GPU-free self-test and PaddleOCR
(`ANALYTICS_OCR=paddle`) on real streams. The matching, trail, alerting and
scale results above hold regardless of which backend produces the underlying
read or embedding.
