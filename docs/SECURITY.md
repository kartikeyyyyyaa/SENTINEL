# Security Architecture

Sentinel is a police system handling camera locations, watchlists, and
movement histories of named individuals. Its security posture is built on one
principle: **authorization is enforced by the database, not by the
application.** A bug or compromise in the API cannot widen access, because the
API is never trusted to decide what a user may see — PostgreSQL is.

This document describes how that works and how to verify it. It is written to
be checked, not taken on faith: every claim below has a corresponding test or a
SQL script a reviewer can run.

## 1. One claim, derived authority

Every database transaction asserts exactly **one** fact about the caller:

```sql
SELECT set_config('app.user_id', '<the authenticated user id>', true);
```

That is the *entire* trust surface. The application never tells the database
"this user is a state admin" or "this user may see district GJ-AHM." It says
only *who* the user is. Everything else — which jurisdiction subtree they can
see, which department they belong to, which permissions they hold, whether they
are statewide — is **derived inside the database** by `SECURITY DEFINER`
functions that read the identity tables:

- `app.current_user_id()` — reads the asserted claim.
- `app.current_jurisdiction_path()` — the user's jurisdiction subtree.
- `app.current_department_id()` — the user's department.
- `app.has_permission(perm)` — whether the user's role grants `perm`.
- `app.is_statewide()` — whether the user sees the whole state.
- `app.path_in_scope(target_path)` — whether a row's jurisdiction is within the
  user's subtree.

Because the only claim the application can assert is an identity, **a compromised
application cannot escalate its own privileges** — there is no broader claim
available to set. This is the difference between "the app checks permissions"
(which fails when the app is buggy) and "the app cannot express a permission it
does not have" (which holds even when the app is fully compromised).

Row-Level Security policies on every table call these functions. A `SELECT` on
`app.camera` returns only cameras in the caller's jurisdiction subtree (or all,
for a statewide user); a write is checked the same way. Policies **fail closed**:
when `app.user_id` is absent, the derivation functions return no scope and the
policies deny everything.

## 2. The application role cannot bypass RLS

The API connects as a dedicated role (`sentinel_app`) created
`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS`.
Two of those matter most:

- **`NOSUPERUSER`** — superusers bypass RLS entirely, so the app role must not
  be one.
- **`NOBYPASSRLS`** — the explicit "ignore row security" privilege, withheld.

Migrations run under a *separate* superuser connection (`DATABASE_ADMIN_URL`);
the serving path uses only `sentinel_app` (`DATABASE_URL`). `setup-local.ps1`
verifies at setup time that the app role really is `NOSUPERUSER`/`NOBYPASSRLS`
before proceeding — a misconfigured superuser app role would silently defeat
every policy, so it is checked rather than assumed.

## 3. Machines authenticate the same way as people

An edge analytics worker is not a special case with a side channel. An API key
maps to a machine `app_user` row (migration `008_api_key_user.sql`) with a
scoped role (`edge_worker`: `watchlist.read`, `camera.health_write`,
`camera.read_cross_department`, `alert.read`). When a worker calls the API, the
same single claim — `app.user_id` = the machine user's id — is asserted, and the
same RLS policies and audit trail apply. There is exactly one authorization code
path for humans and machines, which is far smaller to reason about and audit
than two.

Human sessions use JWT bearer tokens; machine callers use `X-API-Key` (or
`Authorization: ApiKey …`). Both resolve to an `app_user` id and nothing more.

## 4. Tokens

Access tokens are JWTs with defence against the standard attacks, each covered
by a negative test in `services/registry/tests/test_tokens.py`:

- **Algorithm is pinned.** An `alg: none` token, or a token signed with a
  different algorithm, is rejected — the classic JWT forgery.
- **`kid` header** identifies the signing key, so keys can rotate.
- **Issuer and audience** are verified; a token minted for another purpose or
  service is not replayable.
- **Expiry is mandatory** — a token with no `exp` is not treated as valid
  forever; expired and not-yet-valid tokens are rejected.
- Tokens carry **no authorization claims** — only a subject. A token is an
  identity, not a capability (see §1); tampering with the subject to escalate
  fails the signature check.

Refresh tokens are stored only as hashes, are single-use, and reuse is detected
(a replayed refresh token is treated as a compromise signal and audited).
Password handling uses bcrypt, enforces a policy, and has constant-time
account-lookup behaviour so a wrong username costs the same as a wrong password
(no account-enumeration oracle) — all in `test_passwords.py`.

## 5. Camera credentials are encrypted at rest

Camera passwords are the crown jewels of a CCTV platform — they grant live video
access. They are never stored in plaintext. Each credential is encrypted with a
per-credential data-encryption key (DEK), which is itself wrapped by a master
key (`CREDENTIAL_MASTER_KEY`, 32 bytes, held only in the environment, never in
the database). `services/registry/app/core/crypto.py` implements the envelope
scheme; `test_crypto.py` includes a **credential-transplant** negative test —
proof that a credential blob copied from one camera row cannot be decrypted in
the context of another, so a database-only compromise does not yield usable
passwords.

For the government camera grid specifically, no credential is stored at all: the
participant's single grid email/password lives in the environment and is
assembled into the RTSP URL at request time by `/api/analytics/assignments`
(see the onboarding docs) — the camera row holds only the credential-free HLS
URL and the stream id.

## 6. The audit trail is tamper-evident, in the database

Every consequential action writes a row to `app.audit_log`. The trail is:

- **Append-only** — no `UPDATE` or `DELETE` policy exists for any caller;
  `test_audit.py` includes edit- and delete-attempt negative tests.
- **Hash-chained in the database, by trigger** — each row's hash covers its
  own content plus the previous row's hash. The chaining happens in a database
  trigger, so **even a fully compromised application cannot write an unchained
  entry**, and any later tampering breaks the chain and is detectable by walking
  it.
- **Independently re-verifiable** — a Python implementation (`verify_chain()`)
  recomputes the chain from scratch and must agree with the database, and
  `db/verify_parity.sql` asserts the SQL and Python hash definitions match. This
  is what makes the trail court-admissible: the integrity check does not depend
  on the same code that wrote the rows.

Actions taken by anonymous or just-denied callers — failed logins, permission
denials, token-reuse detection, and women's-safety SOS reports — are the most
security-relevant rows of all, and they are written through a `SECURITY DEFINER`
helper (`app.write_audit_row`, migration `010`) so that an actor with no
`app.user_id` (or no read permission on the row) still produces an audit
entry. This closed a real defect found during development, where these rows were
silently failing to persist because `INSERT ... RETURNING` requires the row to
satisfy the table's SELECT policy — which an anonymous caller can never do. The
fix is verified: the chain still validates intact with those rows present.

Each audit row records `purpose` and `case_reference`, tying processing to a
stated purpose per access — the accountability the DPDP Act 2023 expects of a
system that processes personal data at this scale.

## 7. Defence in depth at the edge

The operator console ships with a strict Content-Security-Policy
(`web/nginx.conf`): `script-src 'self'` with no inline script and no `eval`,
`connect-src 'self'`, `object-src 'none'`, `base-uri 'none'`,
`frame-ancestors 'none'`, plus `X-Content-Type-Options`, `X-Frame-Options:
DENY`, and a `no-referrer` policy. Everything the console needs is vendored, so
no third-party origin is ever contacted. The console handles metadata only — no
video stream ever crosses it.

## 8. What a reviewer can run

```bash
# The security core — 198 adversarial tests, no DB or network needed:
cd services/registry && python -m unittest discover -s tests -t . -v

# Audit hash parity + append-only, against a live DB:
psql -U postgres -d sentinel -v ON_ERROR_STOP=1 -f db/verify_parity.sql
```

Most of those 198 tests are negative — credential transplant, `alg:none`
forgery, audit-row tampering, account-enumeration timing. A round-trip test
shows the code works; only the attacks show it is safe.
