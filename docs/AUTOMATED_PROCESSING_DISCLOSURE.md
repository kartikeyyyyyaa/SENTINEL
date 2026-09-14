# Automated Processing Disclosure

Sentinel's edge analytics pipeline makes automated determinations about
vehicles and, optionally, faces — determinations that can open a critical
alert an operator then acts on. This document discloses what is automated,
what a human still decides, where the pipeline can be wrong, and how an
affected person or a reviewer can find out a determination was made. It is the
counterpart to `DATA_HANDLING_AND_RETENTION.md` and is linked from the same
**Data & Privacy** panel in the console footer.

## 1. What is automated, and what is not

| Step | Automated? | Human in the loop? |
|---|---|---|
| Object/vehicle detection in a frame | Yes | No |
| Number-plate OCR read | Yes | No |
| Plate-vs-watchlist matching | Yes (`WatchlistIndex.match_plate`) | No |
| Face-embedding comparison for enrolled watchlist entries | Yes, on a **stub** backend in this build (see §3) | No |
| **Opening an alert from a match** | Yes | No |
| **Acting on an alert** — dispatch, closing as false positive, escalation | **No** | **Yes, always** |
| Women's-safety risk scoring (isolated zone + track pattern) | Yes | Operator decides response |

The automated stages decide only whether to **surface** something to a human.
No automated stage in this system takes an action with real-world consequence
— no lock, no block, no notification to a third party — without an operator's
explicit dispatch or acknowledge action, each of which is written to the
append-only audit trail (`SECURITY.md` §6) under the operator's own identity.
This is the "automated decision-making" boundary the DPDP Act 2023 and
comparable frameworks treat as significant: Sentinel automates detection and
triage, never the consequential decision.

## 2. What a match is based on

- **Vehicle plate matches** are based on OCR text compared, after
  normalisation (case, separators — never character substitution), against
  active watchlist entries. `ANALYTICS_QUALITY.md` §1 documents this with the
  actual positive and negative test cases run against the code, including two
  near-miss reads that correctly do *not* match.
- **Face matches**, where enabled, are based on embedding similarity against
  enrolled watchlist entries only — not against the general public, and not a
  live search against unrelated video. No face is compared unless it was
  deliberately enrolled onto a watchlist under a case reference.

## 3. Known accuracy and limitations

Stated plainly, because a system that can flag a person deserves an honest
account of when it might be wrong:

- **ANPR/OCR** runs on PaddleOCR in production configuration
  (`ANALYTICS_OCR=paddle`) and a stub backend in the GPU-free self-test used
  for development and demonstration. Measured funnel behaviour (4 OCR calls
  per 480 frames, ~8.3 per 1,000 — `ANALYTICS_QUALITY.md` §2) shows the
  pipeline is selective, not that every plate in view is read; a plate that is
  obscured, poorly lit, or at a steep angle may simply not produce a read
  rather than produce a wrong one.
- **Face matching runs on a stub embedding backend in this build.** It proves
  the matching, deduplication, thresholding, and alerting logic end-to-end,
  but the embeddings it compares carry no real facial signal. **No face-match
  alert from this build should be treated as a verified biometric
  identification** until a real embedding model
  (`ANALYTICS_FACE=insightface`) is deployed and separately validated for
  accuracy and bias across the population it will be used on. This is stated
  here, not only in `ANALYTICS_QUALITY.md`, because it is the single most
  consequential limitation for anyone relying on a match.
- **False positives are expected and designed for**: every alert carries a
  "close as false positive" action (`api/routers/alerts.py`), and closing one
  writes an audit row — the system is built assuming operators will and should
  reject some automated matches, not built to be trusted blindly.
- **No demographic accuracy breakdown has been produced for this build.**
  Object detection and OCR models are known in the literature to vary in
  accuracy across lighting conditions, plate designs, and — for any face
  pipeline — across demographic groups. Publishing a breakdown for the actual
  deployed model (once a real face backend replaces the stub) is called out
  here as an open item, not glossed over.

## 4. How to find out a determination was made

Every match, alert, and operator action against it is in the append-only,
hash-chained audit trail (`SECURITY.md` §6), each row carrying a `purpose` and
`case_reference`. An oversight body or a data-subject-access process can
request the trail for a specific case reference and receive a record that is
independently verifiable — not dependent on trusting the same code that wrote
it (`verify_chain()` / `db/verify_parity.sql`). This is what makes "was I
matched, and who acted on it" an answerable question rather than an assertion.

## 5. Scope note

This disclosure covers the automated processing implemented in this
hackathon build. It does not cover any capability not present in the code —
there is no predictive policing model, no demographic inference, and no
automated action beyond opening an alert for human review.
