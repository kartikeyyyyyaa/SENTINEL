# Data Handling & Retention Notice

Sentinel processes personal data at scale — camera footage, number plates,
faces, and the movement histories those imply. This notice states plainly what
is collected, why, for how long, and who can see it. It exists because a
system built on `app.user_id`-scoped Row-Level Security and a hash-chained
audit trail (see `SECURITY.md`) is only as accountable as what it discloses
about itself — the access controls mean nothing to a citizen who doesn't know
they're covered by them.

This is an internal notice for operators, auditors, and reviewers, condensed
in the console itself under **Data & Privacy** in the footer. It is written to
be checked against the schema and code, not taken on faith.

## 1. What is collected

| Data | Source | Table |
|---|---|---|
| Camera identity, location, status | Grid onboarding / manual entry | `app.camera` |
| Vehicle number plates (ANPR reads) | Edge analytics (`services/analytics`) | emitted as events; matched plates become `app.watchlist_match` rows |
| Face embeddings, for watchlist matching only | Edge analytics, stub backend in this build | `app.watchlist_entry` (enrolled), matched via events — see §4 |
| Object tracks (person/vehicle bounding boxes, class, confidence) | Edge analytics | not persisted beyond the live event stream unless they trigger a rule |
| Alerts (watchlist match, women's-safety risk, SOS, camera health) | Rules engine / operator / public SOS | `app.alert` |
| Operator actions (acknowledge, close, dispatch, login) | Console | `app.audit_log` |

Sentinel does **not** store raw video. Edge analytics run at the camera or a
nearby edge node and emit metadata events (`services/common/events.py`); the
video itself is only ever proxied live, on demand, through the console
(`web/nginx.conf`'s `/grid/` route) and is never written to Sentinel's own
storage. This is a deliberate boundary, not an oversight: it is what keeps the
system's own data-protection surface to metadata rather than to a video
archive.

## 2. Why (purpose limitation)

Every audit row records a `purpose` and, where one exists, a `case_reference`
(migration `010`, `SECURITY.md` §6) — processing is tied to a stated reason,
not open-ended. The purposes this system is built for:

- **Traffic and public-safety monitoring** — camera health, ANPR at
  junctions, dwell/proximity primitives that feed the rules engine.
- **Watchlist enforcement** — stolen-vehicle and missing-person matching
  against entries created under a case reference (`FIR-...`, `MP-...` in
  `seed.py`), each traceable to the department that entered it.
- **Women's-safety response** — the isolated-zone + SOS features exist to
  raise and route a specific category of alert faster, not to profile
  individuals.

Processing outside these purposes — for example, tracking a person's movement
with no watchlist match, no alert, and no case reference — is not a supported
query path in this system; jurisdiction-scoped RLS and the audit trail exist
precisely so that any attempt would be both blocked and logged.

## 3. Retention

| Data | Retention | Basis |
|---|---|---|
| Raw video | Not stored by Sentinel | out of scope by design (§1) |
| Object tracks / non-matching events | Not persisted past the live stream | metadata-only, no standing store |
| ANPR reads that do **not** match a watchlist entry | Not persisted | avoids building a de facto movement log of uninvolved vehicles |
| Watchlist matches (`app.watchlist_match`) | Retained while the watchlist entry is active, then per department retention schedule | ties retention to an active case |
| Alerts | Retained per department retention schedule | investigative/audit value |
| Audit log | Retained indefinitely, never deleted or updated | append-only by design (`SECURITY.md` §6); this is the accountability record itself |

The one deliberate asymmetry — audit logs never expire while investigative
data follows a department schedule — is intentional: the record of *who
accessed what* is the safeguard, and weakening it to match shorter data
retention would remove the ability to detect misuse after the fact.

**Not yet implemented in this build:** an automated purge job enforcing the
department retention schedule above. Today that schedule is a policy
statement; a scheduled job (or `db/verify_parity.sql`-style script) that
actually deletes expired `watchlist_match`/`alert` rows to the department
schedule is the concrete next step, called out here rather than implied.

## 4. Who can see it

Access is enforced by PostgreSQL Row-Level Security, not by the application
(`SECURITY.md` §1–2): a user sees only their jurisdiction subtree, derived
from their own identity, never from a claim the API could get wrong. Two
practical consequences for this notice:

- A district operator cannot see another district's watchlist matches or
  alerts, regardless of what the console UI requests.
- A machine caller (the edge analytics worker) authenticates the same way a
  person does and is bound to the same policies (`SECURITY.md` §3) — there is
  no service-account back door with broader visibility.

## 5. AI/LLM processing disclosure

Sentinel's core detection, ANPR, and matching pipeline (`services/analytics`)
uses computer-vision models — object detection, OCR, face-embedding
comparison — not a large language model, and no user-facing chat or generative
AI feature is part of this system. Where any component uses an LLM in future
(for example, natural-language search over alerts), that use will be disclosed
here and in-product before it ships, per the DPDP Act 2023's expectation that
automated processing be identified, not assumed self-evident.

The nature of that computer-vision processing — what it decides, on what
input, and with what accuracy — is disclosed separately in
`AUTOMATED_PROCESSING_DISCLOSURE.md`, since it carries different obligations
(explainability of an automated match, not general data handling).

## 6. Cross-references

- `SECURITY.md` — how access control, encryption, and the audit trail are
  implemented and independently verifiable.
- `AUTOMATED_PROCESSING_DISCLOSURE.md` — what the automated pipeline decides
  and its known accuracy/limitations.
- `ANALYTICS_QUALITY.md` — measured detection/OCR accuracy and funnel
  efficiency this notice's accuracy claims are drawn from.
