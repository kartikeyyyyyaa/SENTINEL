# Sentinel — Path to Submission (Gujarat CCTV Hackathon 2026)

Written 3 Sep 2026. Deadline: 7 Sep 2026. Four working days remain.

## 1. Where the platform stands right now

The repo already implements all three reference models as one layered system, not
three disconnected demos:

| Layer | Reference model | Status |
|---|---|---|
| `registry` | Model 1 (mandatory) | Core is solid: jurisdiction hierarchy, camera metadata + GIS, RBAC via Postgres RLS, hash-chained audit trail, health/gap tracking. Newly added this pass: full HTTP API surface, JWT + API-key auth, SSE live feed. |
| `federation` | Model 3 | Adapter framework already existed (ONVIF/RTSP, Genetec, Milestone, generic Sentinel Grid adapters) — untouched this pass, was already ahead of the other two layers. |
| `viewer` / analytics | Model 2 | Newly added this pass: watchlist matching, rule-based alerting, and the operator console that ties it all together. |

The single biggest gap going into this pass was **Step 3 of the problem
statement** — a searchable watchlist (stolen vehicles, wanted/missing persons,
blacklisted vehicles, suspects) with continuous AI matching against live feeds and
automated real-time alerting on a match. That is now implemented end-to-end and
verified live against a real database, not just written:

- `app.watchlist_entry` / `app.watchlist_match` / `app.alert` schema (RLS-protected).
- Edge/analytics workers authenticate with a scoped API key (`edge_worker` role:
  read watchlist, write camera health, read/write alerts — nothing else) and post
  sightings to `POST /api/watchlist/match`.
- A match against an open watchlist entry opens **one** alert per entry (upsert on
  `alert_key`), while every individual sighting still lands in the append-only
  `watchlist_match` log — verified live with two sightings from two different
  cameras collapsing into one alert but two trail entries.
- `GET /api/watchlist/{id}/trail` returns that trail in order — this is the
  literal "start mapping the criminal" feature: a wanted plate or face that
  appears at Camera A at 09:14 and Camera B at 09:41 now produces a timestamped,
  camera-by-camera movement trail an operator can pull up, not just a single
  ping.
- Alerts and camera-health events push to the console over Server-Sent Events, so
  an operator watching the console sees a match the moment the edge worker posts
  it — no polling, no refresh.

Women's-safety feature added this pass, per your explicit ask:

- `POST /api/sos` — unauthenticated on purpose (a kiosk or a phone in someone's
  hand cannot be expected to be logged in) — opens a **critical** severity alert
  immediately and logs to the audit trail even though the caller has no identity.
- Console-side "Women's Safety Mode": recolors the theme, reorders the alert feed
  so safety-flagged items always sort first, and highlights cameras in
  isolated/high-risk zones on the map.
- This is deliberately additive to the watchlist/alert pipeline rather than a
  separate system — an SOS is just another `alert` row with `kind='sos'`, so it
  gets the same audit trail, the same live push, the same operator workflow.

Console visual overhaul (your "looking dead" complaint): rebuilt as a dark
glass "control room" aesthetic — glow/pulse states for live alerts, a radar
sweep overlay, severity-colored feed items, a real login screen, and a working
watchlist search/trail panel. Verified with actual screenshots against the real
API, not just in isolation — login, dashboard with live alerts, watchlist trail
expansion, safety-mode toggle, and the SOS confirmation flow all render and
behave correctly.

## 2. Bugs found and fixed this pass (worth knowing about)

Two of these are the kind of thing a judge's live demo would hit immediately;
one is a genuinely serious pre-existing defect.

1. **Audit trail was silently failing to record anonymous/denied actions.**
   `record_out_of_band()` — the path used for failed logins, permission denials,
   token-reuse detection, and now SOS creation — wraps its insert in a
   try/except that was meant to tolerate the database being briefly unavailable.
   It was actually swallowing a Postgres RLS error: `INSERT ... RETURNING`
   requires the inserted row to pass the table's **SELECT** policy, and an
   anonymous or just-failed-auth caller can never pass it. In practice this
   means this whole category of audit rows — including failed login attempts
   and (once added) SOS reports — was probably never being written since the
   audit system was first built. Given the audit trail exists specifically for
   court-admissible, DPDP Act 2023–compliant evidence, this is significant.
   Fixed with a `SECURITY DEFINER` helper (`app.write_audit_row`, migration
   010) that performs the insert and read-back itself, bypassing the caller's
   RLS view only for that read-back. Verified fixed: failed-login and SOS rows
   now appear in `app.audit_log`, and the hash chain still verifies intact.

2. **The same RLS/RETURNING interaction broke the watchlist-match endpoint on a
   retry.** The first version used `SELECT ... FOR UPDATE` to check whether an
   alert already existed before deciding insert-vs-update. Postgres enforces
   the table's **UPDATE** policy on `FOR UPDATE`, not just its SELECT policy —
   and an edge worker correctly has no `alert.acknowledge` permission (that's
   an operator-only action), so the check always came back empty and a repeat
   sighting crashed with a unique-constraint violation instead of updating
   anything. Fixed by inserting first inside a savepoint, catching the
   conflict, and reading (never mutating) the existing alert — which also
   means a machine principal never needs write access to alert content, matching
   the intent that only a human operator acknowledges or closes an alert.

3. **`GET /api/ready` returned 500 with "permission denied for table
   schema_migration."** The migration runner created that bookkeeping table but
   never granted the app role `SELECT` on it. One-line fix in `migrate.py`'s
   bootstrap block.

4. **Migration `006_token_rotation.sql` defined an index with the same name as
   one already created in `001_core.sql`**, so migrations failed on a clean
   database. Renamed.

None of these were visible from reading the code — all four were only found by
standing up a real Postgres instance and exercising the endpoints live, which
is exactly why the plan below keeps a live-verification step for anything new
between now and the 7th.

## 3. What's still missing for Model 1 (mandatory) compliance

- **`docs/SECURITY.md`** — referenced by the README, doesn't exist yet. This is
  low-effort relative to its payoff: judges will read the README, see the
  reference, and go looking for it.
- **A `tests/` directory for the new code.** `services/registry/tests/` covers
  crypto, tokens, passwords, audit, and the federation adapters well, but
  nothing yet exercises the new watchlist/alert/broadcast/API-router code.
- **An explicit scale narrative for ~80,000 cameras.** The RLS + jurisdiction-tree
  design already scales in principle (no policy scans the whole camera table;
  everything is scoped by department/jurisdiction path), but nothing currently
  states this as a design decision a reviewer can check off.
- **A presentation deck.** Not started.

## 4. Plan to the 7th

**Today, 3 Sep (remaining hours)**
- Write `docs/SECURITY.md` (RLS model, audit chain, credential handling,
  auth model for humans vs. machines — most of this can be lifted from what's
  already true of the code, it just isn't written down).
- Add unit/integration tests for: upsert-on-alert_key behavior, the SOS
  anonymous-insert path, and `write_audit_row`'s parity with the trigger-based
  chain (mirroring the existing `verify_parity.sql` pattern).

**4 Sep**
- Write the scale/sizing note (`docs/SCALE.md` or fold into `SECURITY.md`):
  camera count assumptions, index strategy on `camera(department_id)` and the
  jurisdiction path, connection-pooling plan for the analytics workers, and
  why RLS overhead stays flat as camera count grows (policies filter on
  indexed columns, not the identity tables).
- Start the presentation deck outline: problem → architecture → the three
  models mapped to one platform → live demo script (login → watchlist hit →
  trail → SOS) → security posture → scale story.

**5 Sep**
- Full dry run of the demo script end-to-end on a clean `docker-compose down -v`
  bring-up, exactly as a judge would do it. Fix whatever that surfaces —
  clean-slate bugs are the ones most likely to appear during actual judging.
- Finish the deck.

**6 Sep**
- Buffer day for whatever the dry run found. Record a fallback demo video in
  case live networking/projector issues eat time during judging.

**7 Sep — deadline**
- Final packaging, submission.

## 5. What would make this stand out

- **The trail, demoed live, not described.** Most teams will show "camera sees
  plate → alert fires." Few will show the same plate at two cameras collapsing
  into one alert with a two-point movement trail — that's the actual
  "mapping the criminal" capability the problem statement is pointing at, and
  it's already working. Lead the demo with it.
- **Say the audit-trail bug out loud.** Counterintuitive, but finding and fixing
  a defect in your own tamper-evident evidence trail — and being able to show
  the hash chain verifying intact before and after — is a stronger security
  story than claiming nothing was ever wrong. It demonstrates the chain is
  actually being tested, not just built.
- **RLS-enforced-in-the-database, not the app, as a stated design decision.**
  Most hackathon submissions put authorization in application code. Being able
  to say "a compromised API server cannot widen its own access because there is
  no broader claim to assert — Postgres itself is the enforcement point" is a
  differentiated, verifiable claim, and `verify_parity.sql` gives judges a way
  to check it themselves rather than take your word for it.
- **One machine-identity model for humans and edge workers.** Rather than a
  side-channel for service accounts, edge workers are just app_users with a
  scoped role — so every permission, every audit row, every RLS policy applies
  identically regardless of whether a human or a camera pipeline is the caller.
  That's a smaller, more auditable system than most teams will build.
- **The women's-safety feature reuses the alert pipeline instead of bolting on
  a parallel system.** Worth stating explicitly: an SOS is not a special case
  in the architecture, which is why it got the audit trail and live-push
  "for free."

## 6. Setup note

Run `docker compose down -v` before bringing the stack up again locally — you're
picking up five new/changed migrations and a changed `nginx.conf`, and a stale
container/volume from before this pass can leave the migration table or the
old nginx config in a half-upgraded state. Then:

```powershell
docker compose up -d db
docker compose build api
docker compose run --rm api python -m app.migrate up
docker compose run --rm api python -m app.seed
docker compose up -d
```

`app.seed` prints a username/password for a state-admin and a district-operator
account, plus one edge-worker API key, once — copy them from that output. Don't
reuse the sandbox credentials from this conversation; they were minted against
a throwaway database and won't exist on your machine.
