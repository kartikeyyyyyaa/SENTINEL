"""Minimal migration runner.

Applies `db/migrations/*.sql` in filename order inside a single transaction each,
recording what it applied in `app.schema_migration` along with a checksum. If a
migration file changes after it has been applied, the runner refuses to continue
rather than silently diverging from the deployed schema.

    python -m app.migrate up
    python -m app.migrate status

Runs as the Postgres superuser via DATABASE_ADMIN_URL, because it creates
extensions, roles grants, and RLS policies.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

try:
    import psycopg
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise SystemExit(
        f"psycopg is not usable in this interpreter: {exc}\n"
        "\n"
        "On Windows you need the wheel that bundles libpq. Bare 'psycopg' does not\n"
        "include it, which is why the import fails with 'no pq wrapper available':\n"
        "\n"
        "    python -m pip install \"psycopg[binary]\"\n"
    ) from exc

from .config import get_settings

def _find_migrations_dir() -> Path:
    """Locate db/migrations, whether running in the container or natively.

    In the container the repo's ./db is bind-mounted at /srv/db, so it sits beside
    the app package. Run natively on a developer machine it is two levels up, at
    the repository root. Rather than encode one layout and break the other, walk up
    from this file and take the first hit.
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "db" / "migrations"
        if candidate.is_dir():
            return candidate
    # Nothing found. Return the container path so the error message names a
    # concrete location rather than describing a failed search.
    return here.parent.parent / "db" / "migrations"


MIGRATIONS_DIR = _find_migrations_dir()

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.schema_migration (
    filename    text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
-- GET /api/ready (main.py) reports the applied migration count over the same
-- least-privilege connection every other request uses, on the theory that a
-- readiness probe answered by a superuser connection proves nothing about
-- whether the connection real traffic uses actually works. That query needs
-- SELECT here, which nothing else grants — this table is created here, by the
-- runner, before any migration file exists to grant it. Wrapped in a DO block
-- because the app role may not exist yet the very first time this runs.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = coalesce(nullif(current_setting('sentinel.app_role', true), ''), 'sentinel_app')) THEN
        EXECUTE format(
            'GRANT SELECT ON app.schema_migration TO %I',
            coalesce(nullif(current_setting('sentinel.app_role', true), ''), 'sentinel_app')
        );
    END IF;
END $$;
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _migration_files() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"migrations directory not found: {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM app.schema_migration")
        return {row[0]: row[1] for row in cur.fetchall()}


def up() -> int:
    settings = get_settings()
    if not settings.database_admin_url:
        raise SystemExit("DATABASE_ADMIN_URL must be set to run migrations")

    with psycopg.connect(settings.database_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)

        applied = _applied(conn)
        pending = []

        for path in _migration_files():
            body = path.read_text(encoding="utf-8")
            digest = _checksum(body)
            if path.name in applied:
                if applied[path.name] != digest:
                    raise SystemExit(
                        f"migration {path.name} was modified after being applied.\n"
                        f"  applied checksum: {applied[path.name]}\n"
                        f"  current checksum: {digest}\n"
                        "Write a new migration instead of editing an applied one."
                    )
                continue
            pending.append((path, body, digest))

        if not pending:
            print("migrate: nothing to do, schema is current")
            return 0

        for path, body, digest in pending:
            print(f"migrate: applying {path.name} ... ", end="", flush=True)
            # autocommit is on, so wrap each migration explicitly. A migration
            # either lands whole or not at all.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(body)
                    cur.execute(
                        "INSERT INTO app.schema_migration (filename, checksum) VALUES (%s, %s)",
                        (path.name, digest),
                    )
            print("ok")

        print(f"migrate: applied {len(pending)} migration(s)")
        return 0


def status() -> int:
    settings = get_settings()
    with psycopg.connect(settings.database_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
        applied = _applied(conn)

    for path in _migration_files():
        mark = "applied" if path.name in applied else "PENDING"
        print(f"  [{mark:>7}] {path.name}")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "up"
    if command == "up":
        return up()
    if command == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
