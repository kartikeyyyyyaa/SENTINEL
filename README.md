# Sentinel — Integrated Video Management & Analytics Platform

Submission for the **Gujarat CCTV Hackathon 2026**.

This repository implements Models 1, 2 and 3 of the reference architecture as a
**single layered platform** rather than three separate products:

| Layer | Reference model | Role in this platform |
|---|---|---|
| `registry` | **Model 1** (mandatory) | System of record. Camera metadata, jurisdiction hierarchy, GIS mapping, health, gap analysis, RBAC, audit. |
| `federation` | **Model 3** | Adapter/plugin framework + metadata & event bus. The integration substrate. |
| `viewer` | **Model 2** | Unified viewing, ANPR metadata generation, event indexing, alerts. Consumes the bus. |

The adapter framework supports **both** ingestion modes by configuration:
direct-connect (RTSP/ONVIF straight to a camera or departmental VMS — Model 2) and
VMS-federated (via the middleware bus — Model 3). Model 1 is the spine both depend on.

## Security posture

Authorisation is enforced in the **database**, not just the application, using
PostgreSQL Row-Level Security. The application asserts exactly **one** claim per
transaction — `app.user_id`. Jurisdiction subtree, department, permissions and
statewide status are all *derived* inside the database by `SECURITY DEFINER`
functions over the identity tables. A compromised application therefore cannot widen
its own scope, because there is no wider claim available to assert. Policies fail
closed when the claim is absent, and the application connects as a dedicated
non-superuser role without `BYPASSRLS`.

The audit trail is append-only and hash-chained **in the database via trigger**, so even a
fully compromised application cannot write an unchained entry, and any later tampering is
detectable by walking the chain. See `docs/SECURITY.md`.

## Quick start

Two supported paths. Use Docker if you have it; use the native path if WSL 2 is
unavailable on your machine.

### With Docker

```powershell
.\scripts\new-env.ps1          # generates .env with cryptographically random secrets
docker compose up -d db
docker compose build api
docker compose run --rm api python -m app.migrate up
docker compose run --rm api python -m app.seed
docker compose up -d
```

- API + interactive docs: http://localhost:8000/docs
- Operator console: http://localhost:8080
- Postgres: `localhost:5433` (mapped off 5432 to avoid clashing with a local install)

### Without Docker (native Windows)

Docker Desktop needs WSL 2 or Hyper-V. On a managed machine `wsl --install` may be
blocked by policy, in which case Docker cannot start at all. This path needs neither:

```powershell
.\scripts\new-env.ps1 -Local   # also writes DATABASE_URL for localhost:5432
.\scripts\setup-local.ps1      # creates DB + PostGIS + app role, venv, migrations
```

`setup-local.ps1` does everything `docker-compose.yml` and `db/bootstrap/` would have
done, and verifies that the app role really is `NOSUPERUSER` / `NOBYPASSRLS` before
continuing — a superuser role would silently defeat every policy in `004_rls.sql`.

It will tell you what to install if PostgreSQL or PostGIS is missing. PostGIS is not
optional: camera geometry and coverage analysis are computed in the database.

Then:

```powershell
cd services\registry
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Note that native Postgres listens on **5432**, while the Docker path maps to **5433**.
That difference is deliberate, so both can coexist on one machine.

On Linux or macOS, generate the secrets by hand instead of running the script:

```bash
cp .env.example .env
python -c "import secrets;print(secrets.token_urlsafe(48))"                    # JWT_SECRET
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"  # CREDENTIAL_MASTER_KEY
```

Back up `CREDENTIAL_MASTER_KEY`. Losing it makes every stored camera credential
unrecoverable; rotating it without re-wrapping orphans them.

## Tests

The security core is pure Python with no database or framework dependency, so it
tests with nothing installed beyond the runtime:

```bash
cd services/registry
python -m unittest discover -s tests -t . -v
```

Most of these are negative tests — credential transplant between cameras, JWT
`alg: none` forgery, audit-row edits and deletions, account-enumeration timing.
A round-trip test shows the code works; only the attacks show it is safe.

Two checks need a live database:

```powershell
# audit hash: SQL and the Python mirror must agree, and the log must be append-only
# Docker:
docker compose exec -T db psql -U postgres -d sentinel -v ON_ERROR_STOP=1 -f /srv/db/verify_parity.sql
# Native:
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -d sentinel -v ON_ERROR_STOP=1 -f .\db\verify_parity.sql

# migration state
docker compose run --rm api python -m app.migrate status       # Docker
..\..\.venv\Scripts\python.exe -m app.migrate status           # Native, from services\registry
```

## Layout

```
db/bootstrap/      runs once on first container init (creates the app DB role)
db/migrations/     plain, ordered SQL — no ORM, no migration framework
services/registry/ FastAPI service (Model 1 + API surface for 2 and 3)
web/console/       zero-build Leaflet operator console
docs/              high-level design, security architecture, sizing
tests/             unit tests for the logic that must not be wrong
```

Migrations are plain SQL applied in filename order by `app/migrate.py`, which records
applied files in `app.schema_migration`. Migrations run as the Postgres superuser
(`DATABASE_ADMIN_URL`); the API runs as `sentinel_app` (`DATABASE_URL`).
