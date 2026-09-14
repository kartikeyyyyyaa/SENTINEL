# Build & Test Plan — Models 1, 2, 3

Written 4 Sep 2026; last revised 6 Sep 2026 against the hackathon's published
Resources + FAQs and the extended deadline. **Deadline: 15 September 2026**
(extended from 7 Sep; event 22–23 Sep) — ~9 days. Everything below marked
"verified" was actually run against your repository, on your machine, not
inferred from reading code.

## UPDATE (6 Sep) — the FAQs and feed spec resolved the two big unknowns

The hackathon's Resources page and FAQs answered exactly what was previously
guessed at. The net effect is very favourable: the highest-risk piece is
already built, and one clearly-scoped seam remains.

**The live test case is our trail feature.** Per FAQ 26–28: after registration
teams onboard ~50 geographically-distributed live-simulated RTSP feeds; on the
day a *designated vehicle number* is given and the system must output that
vehicle's **complete route — timestamped, location-wise movement history —
across cameras**, plus evidence of interoperability, onboarding efficiency and
analytics. That is precisely the watchlist-match → `watchlist_match` trail →
computed speed/heading path already built and demoed (two cameras collapsing to
one alert with a two-point trail). The build task is running it against their
live grid, not inventing it.

**The government feed is a live RTSP/RTP grid, discovered via `GET /api/ingest`
— and our ingestion already targets it.** The Resources "how to connect" spec
lists every way a naïve pipeline breaks on this grid; `services/analytics/
source.py` was written against that same list and already handles all of it,
verified by reading the code:

| Feed-spec requirement | Status in `source.py` |
|---|---|
| Force RTSP over TCP | ✓ forces `rtsp_transport;tcp` before any `VideoCapture` |
| Don't trust `CAP_PROP_FPS` | ✓ all timing derived from PTS, FPS ignored |
| Drive timing from PTS, not arrival | ✓ `StreamClock` on PTS; GOP-replay burst tolerated |
| Tolerate inter-frame gaps | ✓ gaps are not treated as disconnects |
| Reconnect with capped backoff | ✓ jittered exponential backoff, capped |
| Decoder RPS/POC warnings non-fatal | ✓ classified as non-fatal, no reconnect |
| Scene discontinuity at loop point | ✓ PTS jump detected → tracker/motion/OCR reset |
| Mixed H.264/H.265 + resolutions | ✓ per-camera; no fixed-shape assumption |
| Catalogue-driven (`cameras.json`) | **DONE (6 Sep)** — `onboard_grid.py` reads the catalogue and registers all 30 cameras; `/api/ingest` does not exist on this deployment (404), `cameras.json` is the real catalogue |

**The onboarding seam — RESOLVED (6 Sep), tested live against a real DB.**
Both ends are now connected:
- `app/onboard_grid.py` (run: `python -m app.onboard_grid`) reads the grid
  catalogue (live `cameras.json`, or the bundled snapshot when the login wall
  or a blocked network prevents a live fetch), merges the local location
  overlay, and registered all **30 cameras across 10 real Gujarat districts**
  (Ahmedabad 9, Navsari 6, Junagadh 5, Rajkot 2, Gandhinagar 2, Aravalli 2,
  Patan/Kutch/Gir Somnath/Banaskantha 1 each) — verified in the DB with correct
  coordinates and per-camera HLS URLs.
- `GET /api/analytics/assignments` (new, in `routers/analytics.py`) is the
  endpoint the worker's `load_cameras_from_registry` already called but that
  never existed — it returns the 30 grid cameras and builds each camera's
  authenticated RTSP URL from `GRID_EMAIL`/`GRID_PASSWORD` at request time
  (`@`→`%40`, verified), or leaves it empty (worker runs stubs) when creds are
  unset. Confirmed live: 30 cameras returned, `streams_authenticated` toggles
  correctly with the env.

The cameras now appear on the console map for a statewide admin, and the worker
can be assigned to them — the "onboard the government feed and monitor it
centrally" story (graded for onboarding efficiency, FAQ 28) works end to end.
Credentials are never stored in the DB — only the credential-free HLS URL and
the `vms_camera_id` the RTSP URL is built from.

**Operational caveat discovered — the demo network must open the grid ports.**
RTSP/WebRTC are served on the grid's raw public IP `103.250.160.189`
(8554/TCP, 8889/TCP, 8189/UDP), which hostel/public/college networks routinely
block — the symptom is the Live Grid hanging on "connecting…". HLS works
anywhere (CDN/443). Run the live pipeline and the demo from a mobile hotspot,
home connection, or VPN where those ports are open; confirm the venue network
before the day, or plan to demo over a hotspot. The pipeline degrades safely on
a blocked network (empty RTSP URL → stub frames) rather than crashing.

**Submission documents are now precisely specified** (FAQ 29–32), and note the
hard rule: **demonstrations must show real working software — mock-ups,
animations and concept videos are explicitly rejected.** Required:
- Solution Presentation (PPT/PDF): model chosen *with justification*, overview, key features.
- Technical Proposal / HLD: architecture diagrams; heterogeneous camera/VMS integration (IP, analog, multi-vendor, varied protocols); dispersed-location handling (bandwidth, edge vs. centralized); analytics approach (ANPR, cross-camera tracking); ~80,000-camera scalability; department-level technical detail for integration feasibility.
- Two demo videos: (1) own feed, 2–3 min screen recording — onboarding, live/recorded viewing, vehicle detection/ANPR; (2) live on the government feed — onboarding, viewing, analytics output (ANPR, vehicle/person/intrusion/object detection).



## 0. The three things to focus on (from sentinel.gujarat.gov.in/problems)

The hackathon site's own step-by-step guide names exactly three deliverable
categories left before submission. Every build/test task below exists to
satisfy one of these — this table is the traceability between "why are we
doing this" and the work in Sections 3–4.

**Step 5 — Prepare & Submit (deliverables)**

| Deliverable | What it means for us | Where it stands |
|---|---|---|
| Solution Presentation | A PPT/PDF deck | Not started |
| High-Level Design Document | An architecture doc/diagram, separate from the README's summary table | Not started |
| Own-Feed Demonstration | Run the ONVIF/RTSP direct-connect adapter against a real or file-looped camera feed you supply, end to end into the console | Adapter code exists (`federation/onvif_rtsp.py`), never demonstrated against a real feed |
| Government-Feed Demonstration | Connect to whatever feed/VMS format the organisers provide at the event | **Unknown** — need to check the Resources/FAQ pages or ask the organisers what format this is (RTSP URL? A specific VMS API? Provided on-site?). This determines which federation adapter to rehearse. |
| Video & Output Report | A recorded demo video, plus a written report of what the analytics actually detected on test footage | Not started |
| Submission Links | A working GitHub repo link (and any deployed URL) | **Blocked**: your `git remote` currently points at `https://github.com/kartikeyyyyyaa` with no repository name — this needs fixing before anything can be submitted as a link |

**Step 6 — Plan for Scale (scalability requirement: ~80,000 cameras across Gujarat)**

| Requirement | What "testing/verifying" it means here | Where it stands |
|---|---|---|
| Hardware & Software Requirements | A per-camera and per-worker-box sizing table | Not written, but the numbers to compute it already exist (see Section 4) |
| Network & Bandwidth Planning | A concrete metadata-bandwidth-per-camera figure, extrapolated to 80,000 | Not written; the architecture's "metadata only, no video" claim is real but not yet quantified |
| Storage & Retention Strategy | A stated retention/partitioning policy for `app.audit_log`, `app.watchlist_match`, `app.alert` at fleet scale | Not written |
| AI Processing Capacity | Cameras-per-worker-box, computed from real measured cascade timings | Not written, but `--self-test`'s output (Section 2) is exactly the input this needs |
| Disaster Recovery Strategy | Backup/replica/failover statement for the registry DB | Not written |
| Statewide Rollout Plan | A phased rollout mapped onto the jurisdiction hierarchy already modelled in `app.jurisdiction` | Not written, but the data model already supports it — this is a documentation task, not a build task |

**Step 7 — Evaluation & Recognition (what judges score)**

| Criterion | What it takes to pass it | Where it stands |
|---|---|---|
| Successful Test Case | A scripted, repeatable demo that works cold on a clean environment | Covered by Section 4's end-to-end demo script — needs the clean-environment dry run in Section 3 |
| PPT/PDF Presentation | Same deck as Step 5 | Not started |
| Solution Architecture | A diagram, likely the same HLD as Step 5 | Not started |
| Working Demonstration | The live console + API + analytics pipeline running | Built; needs the dry run |
| Analytics Quality | Evidence the matching/detection is accurate, not just present — e.g. a short table of test plates/faces with expected vs. actual match results and confidence scores | Not assembled, though the pieces (self-test's speedup numbers, live watchlist-match verification from this week) already exist |
| Scalability & PoC Readiness | The Step 6 numbers plus a PoC that actually runs | Depends on Step 6 |
| Bonus Consideration | **Unknown** — the site doesn't say what earns this; worth checking the FAQ or Resources pages | Unresolved |

Two things on that list need information neither of us has yet: exactly what
"Government-Feed" access looks like logistically, and what "Bonus
Consideration" rewards. Worth checking the Resources/FAQ pages on the
hackathon site (or asking the organisers directly) before planning around a
guess — it's a five-minute check that could otherwise cost a day of building
the wrong thing.

## 1. How the three reference models map to this repo

| Layer in this repo | Reference model | What it owns |
|---|---|---|
| `services/registry` | **Model 1** (mandatory) | System of record: camera metadata, jurisdiction/RBAC, audit trail, the HTTP API, the watchlist/alert database. |
| `services/registry/app/federation` | **Model 3** | Adapter framework — ONVIF/RTSP direct-connect, and VMS-federated (Genetec, Milestone, generic "Sentinel Grid") adapters over one interface. |
| `services/analytics` | **Model 2** | The edge pipeline: motion/detect/plate/OCR/face cascade, primitive events, rule engine, watchlist matching, alert/event sinks. |
| `web/console` | spans all three | The operator console — reads Model 1's registry, Model 2's live alerts/events, and is agnostic to which Model 3 adapter fed a given camera. |

## 2. Current state per model, verified today

### Model 1 — registry

- **198 unit tests, all passing, in 6.5 seconds, zero external dependencies**
  (`cd services/registry && python -m unittest discover -s tests -t . -v`).
  These are deliberately adversarial, not round-trip checks: JWT `alg:none`
  forgery, payload tampering, credential-transplant between cameras,
  account-enumeration timing, audit-row tamper detection, lockout state-machine
  edge cases. This is the strongest asset in the whole submission — lead with
  it, and re-run it before every submission-relevant commit.
- The HTTP API (auth, cameras, watchlist, alerts, SOS, SSE events) built this
  week has **no unit tests yet** — it has only been verified by hand, with curl,
  against a live database. That is real verification but not repeatable
  automatically, which matters if you touch this code again before the 7th.
- `docs/SECURITY.md` is referenced by the README and does not exist.
- RLS/audit parity has a dedicated script, `db/verify_parity.sql`, confirmed
  working this week (the audit hash chain reports `is_intact: true` after the
  `write_audit_row` fix).

### Model 3 — federation

- Adapters exist for ONVIF/RTSP direct-connect, Genetec, Milestone, and a
  generic "Sentinel Grid" VMS shape, behind one interface (`federation/base.py`).
- `tests/test_federation.py` (47KB — the largest test file in the repo) is part
  of the same 198-test run above and **passes**. This model is in the best
  tested state of the three and needs the least new work.

### Model 2 — analytics

- The pipeline (motion → detect → plate → OCR → face → tracker → primitives →
  rules → watchlist matching → alert/event sinks) is built and has a genuinely
  strong built-in quality gate: **`python -m services.analytics.main
  --self-test` runs end-to-end with only `numpy` installed — no GPU, no model
  weights — and it exits 0 on your machine**, printing real cascade
  timing/speedup numbers. This is your CI in miniature; it should run before
  every submission-relevant change to this service.
- **RESOLVED (5 Sep) — duplicate worker tree removed.** There were two parallel
  copies of the worker: `services/analytics/*.py` (current, with the face
  stage) and an older `services/analytics/worker/*.py` (no face stage,
  predating person-watchlist matching). The older `worker/` tree has been
  deleted; the self-test and the new suite both still pass, confirming nothing
  depended on it. `services/analytics/` now has one unambiguous worker.
- **RESOLVED (5 Sep) — the promised test suite now exists.** `requirements.txt`
  promised *"the tests are stdlib unittest, runnable with nothing installed
  beyond tier 1... `python -m unittest discover -s tests -t . -v`"* and the
  directory did not exist. It does now: **`services/analytics/tests/` — 42
  tests, passing on your machine with only `numpy` installed** (confirmed cv2,
  paddle and torch all absent). Coverage: plate normalisation's
  evidential-integrity rules (no OCR-confusion substitution), `WatchlistIndex`
  plate/face matching (including the no-fuzzy-match and photo-pending-skip
  properties), and the rule engine (alert-key determinism, risk→severity,
  cooldown suppress-then-refire, speed-uses-lower-bound, women's-safety
  encircled pattern). The exact command the requirements file promised now
  works verbatim.
- Not present in `docker-compose.yml` at all — the worker has no containerised
  path today; it is a manual `python -m services.analytics.main` invocation.
  Fine for a demo you drive yourself, but worth deciding on purpose rather than
  by omission.
- No top-level `tests/` directory (the analytics tests live under the service);
  the README's stated layout mentions a top-level `tests/` — worth either
  adding one or adjusting the README's layout section so they agree.

## 3. Build plan — remaining three days

**Today, 4 Sep**
1. **Fix the submission link now, before anything else.** `git remote -v` shows
   `origin` pointing at `https://github.com/kartikeyyyyyaa` with no repository
   name — that is not a usable submission link in its current state. Create the
   real GitHub repository and point the remote at it. Everything else in this
   plan is worthless if Step 5's "Submission Links" deliverable doesn't
   resolve.
2. Spend 15 minutes on the hackathon site's Resources/FAQ pages (or ask the
   organisers) to pin down two unknowns from Section 0: what "Government-Feed
   Demonstration" actually connects to, and what "Bonus Consideration" scores.
   Both change what gets built next; five minutes now beats a day of guessing.
3. ~~Resolve the `services/analytics/worker/` duplication~~ **DONE (5 Sep)** —
   the older `worker/` tree is deleted (staged in the working tree, not yet
   committed); nothing referenced `worker.main` except this doc. Self-test and
   the new suite both still pass.
4. ~~Create `services/analytics/tests/`~~ **DONE (5 Sep)** — 42 tests, pass with
   only numpy installed. (This was step 7 in the original plan; done early
   because it was the biggest gap and the user picked it first.)
5. Start `docs/SECURITY.md` — RLS model, the single `app.user_id` claim,
   audit-chain design, human-vs-machine auth. Mechanical: it's describing what
   the code already does correctly, not designing something new.

**5 Sep**
5. Finish `docs/SECURITY.md`.
6. Write `docs/SCALE.md` — this single document is what answers Step 6's whole
   "Plan for Scale" requirement:
   - *Hardware & Software Requirements* and *AI Processing Capacity*: use
     `--self-test`'s own measured per-stage timings (Section 2) to compute
     cameras-per-worker-box, then multiply out to ~80,000 cameras statewide.
     This is a calculated number, not a guess — that's the point.
   - *Network & Bandwidth Planning*: compute bytes-per-event × expected events/
     camera/day for the primitive-event and alert JSON payloads, extrapolated
     to the fleet. Backs up the "metadata only, no video" architecture claim
     with an actual figure.
   - *Storage & Retention Strategy*: a stated retention/partitioning policy for
     `app.audit_log`, `app.watchlist_match`, `app.alert` at fleet scale (e.g.
     time-range partitioning past N months).
   - *Disaster Recovery Strategy*: backup cadence and failover story for the
     registry Postgres instance.
   - *Statewide Rollout Plan*: a phased rollout mapped onto the jurisdiction
     hierarchy already modelled in `app.jurisdiction` — this part is mostly
     already true of the data model, so it's a description, not new design.
7. Create `services/analytics/tests/` and write real unit tests against the
   pure-Python pieces that need no camera/GPU: `rules.py`'s alert-key
   determinism and cooldown logic, `watchlist.py`'s `WatchlistIndex` plate/face
   matching, `stages/plate.py`'s normalisation. Target: the same "runs with
   nothing but tier 1 installed" bar the requirements file already promises.
8. Add unit tests for `services/registry/app/api/` — at minimum the upsert-on-
   `alert_key` behaviour, the SOS anonymous-insert path, and the ANPR/track-lock
   kinematics if you keep that console feature.
9. Assemble the "Analytics Quality" evidence Step 7 asks for: a short table of
   test plates/faces run through the pipeline this week, with expected vs.
   actual match result and confidence score. You already generated this data
   live (the two-camera trail test) — this is packaging it, not re-running it.

**6 Sep**
10. **Own-Feed Demonstration**: point the ONVIF/RTSP adapter (`federation/
    onvif_rtsp.py`) at a real camera or a looped video file and confirm
    primitive events reach the console end to end. This has never been
    demonstrated against an actual feed — code review is not the same as this
    working.
11. **Government-Feed Demonstration**: once Step 2's research answers what
    format this is, rehearse it the same way. If it's genuinely unknown until
    event day, say so explicitly in the presentation rather than leaving a gap.
12. Full dry run on a clean `docker compose down -v` bring-up, exactly as a
    judge would do it: migrate → seed → console → login → trigger a watchlist
    match → view the trail → send an SOS. Fix whatever a truly clean
    environment surfaces.
13. Decide and document how the analytics worker is actually started for the
    demo (manual process vs. adding it to `docker-compose.yml`).
14. Build the Solution Presentation deck and the High-Level Design Document
    (Step 5 + Step 7's "Solution Architecture") — the content for both already
    exists across `README.md`, `SUBMISSION_PLAN.md`, and this file; this is
    assembly, not new writing.
15. Record the demo video and write the short output report Step 5 asks for.

**7 Sep — deadline**
16. Final packaging: repo link (from step 1), deck, video, output report, all
    cross-checked against Section 0's tables before submitting.

## 4. Test plan per model

### Model 1 — registry
- **Keep green**: `cd services/registry && python -m unittest discover -s tests -t . -v` — 198 tests, ~6.5s, no network or DB needed. Run this after any change to `core/`, `db.py`, or the API routers.
- **RLS/audit parity** (needs a live DB): `db/verify_parity.sql` via psql, plus `app.write_audit_row`'s Python mirror — confirms the trigger-computed hash chain and the Python `verify_chain()` agree and that the chain is unbroken.
- **New coverage to add**: alert upsert-on-conflict, the anonymous SOS insert path, watchlist search/trail filtering, JWT-vs-API-key dual auth in `deps.py`.
- **Manual live-API smoke test** (already exercised this week, worth keeping as a script rather than ad hoc curl next time): login → list cameras → post two watchlist matches for one entry from two cameras → fetch the trail → confirm it shows two rows and one alert → post an SOS unauthenticated → confirm it appears in `/api/alerts` with no login.

### Model 3 — federation
- Already covered by the same 198-test run (`tests/test_federation.py`). No new automated work needed unless you add a new adapter.
- If time allows: one live smoke test per adapter type against a mock/sandbox endpoint, since the unit tests exercise adapter logic but not a real ONVIF/VMS handshake.

### Model 2 — analytics
- **Quality gate that already exists — use it as your CI**: `python -m services.analytics.main --self-test` — confirmed passing today, needs only `numpy`.
- **New coverage to add** (the real gap): a `tests/` directory with stdlib `unittest`, covering `rules.py` (alert-key determinism, per-entry cooldown), `watchlist.py` (`WatchlistIndex.match_plate` / `match_face` against known fixtures), `stages/plate.py` normalisation, and `stages/face.py`'s stub-backend cosine similarity path.
- **Integration check**: `--dry-run` end-to-end against a local registry (`services/registry` running) to confirm a synthetic watchlist match actually reaches `POST /api/analytics/alerts` and shows up live in the console — this closes the loop between Model 2 and Model 1 rather than testing either in isolation.

### Cross-cutting
- **End-to-end demo script**, run at least once on a from-scratch environment: sign in → watchlist hit from two cameras → trail with computed speed/heading → SOS → acknowledge/dispatch → sign out. This is also your actual judging demo script — testing it and rehearsing it are the same activity, and it's what satisfies Step 7's "Successful Test Case".
- **Security regression**: the 198-test suite already encodes the adversarial cases (forged tokens, credential transplant, audit tampering) — re-run it, don't re-derive it.
- **Scale** (Step 6, "Scalability & PoC Readiness"): two separate kinds of evidence, both cheap to produce:
  1. `EXPLAIN ANALYZE` on the two highest-traffic queries (`GET /api/cameras`, `GET /api/alerts`) against a seeded larger synthetic fleet (a few thousand rows is enough to show the query plan uses the indexed columns and doesn't scan the whole table — the plan shape doesn't change again between 5,000 and 80,000 rows).
  2. The cameras-per-worker-box number from `--self-test`'s own measured stage timings, multiplied out to ~80,000 — this is `docs/SCALE.md`'s central number and it's a calculation, not an estimate.
- **Own-feed / government-feed demonstrations** (Step 5): these are rehearsed, not unit-tested — the test *is* running the adapter against a real feed once before the actual demo, so the first time it's tried isn't in front of judges.

## 5. Commands cheat-sheet

```powershell
# Fix the submission link (do this first — see Section 3, step 1)
git remote set-url origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main

# Model 1 — full security-core suite (no DB needed)
cd services\registry
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v

# Model 1 — RLS / audit-chain parity (needs the DB up)
docker compose exec -T db psql -U postgres -d sentinel -v ON_ERROR_STOP=1 -f /srv/db/verify_parity.sql

# Model 2 — built-in self-test (no GPU, no model weights, no DB)
cd services\analytics
python -m services.analytics.main --self-test

# Model 2 — dry run against synthetic frames
python -m services.analytics.main --dry-run

# Migration state check
docker compose run --rm api python -m app.migrate status
```
