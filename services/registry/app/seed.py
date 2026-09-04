"""Demo data: enough of a jurisdiction tree, cameras, users and watchlist
entries to log into the console and see something real, plus one machine API
key for an edge analytics worker.

Idempotent — every insert is `ON CONFLICT DO NOTHING` / `DO UPDATE`, so this
can be re-run after a `docker-compose down -v` without producing duplicates or
failing on the second run. Runs on the admin connection (bypasses RLS) because
seeding is exactly the "operator with god-mode setting up the world" case
`db.admin_transaction` exists for — no interactive user session has broad
enough scope to create jurisdictions and departments from nothing.

    python -m app.seed

Prints the demo admin password and the minted API key exactly once, the same
"shown once, then only the hash is kept" rule `core/tokens.py` documents for
every other secret this system issues.
"""
from __future__ import annotations

import secrets
import sys

from .core.passwords import hash_password
from .core.tokens import new_api_key
from .db import admin_transaction

DEMO_ADMIN_PASSWORD = "Sentinel-Demo-2026!"  # noqa: S105 - demo credential, printed on run


def _jurisdiction(cur, code: str, name: str, kind: str, parent_code: str | None, path: str) -> int:
    parent_id = None
    if parent_code:
        cur.execute("SELECT id FROM app.jurisdiction WHERE code = %s", (parent_code,))
        parent_id = cur.fetchone()["id"]
    cur.execute(
        """
        INSERT INTO app.jurisdiction (code, name, kind, parent_id, path)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (code, name, kind, parent_id, path),
    )
    return cur.fetchone()["id"]


def _department(cur, code: str, name: str, kind: str) -> int:
    cur.execute(
        """
        INSERT INTO app.department (code, name, kind)
        VALUES (%s, %s, %s)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (code, name, kind),
    )
    return cur.fetchone()["id"]


def _camera(cur, code, name, dept_id, juris_id, camera_type, lon, lat, address, isolated=False) -> int:
    cur.execute(
        """
        INSERT INTO app.camera (code, name, department_id, jurisdiction_id, camera_type,
                                 location, address, status)
        VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, 'active')
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (code, name, dept_id, juris_id, camera_type, lon, lat, address),
    )
    return cur.fetchone()["id"]


def _user(cur, username, full_name, password_hash, dept_id, juris_id, role_code) -> int:
    cur.execute(
        """
        INSERT INTO app.app_user (username, full_name, password_hash, department_id, jurisdiction_id, is_active)
        VALUES (%s, %s, %s, %s, %s, true)
        ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash, is_active = true
        RETURNING id
        """,
        (username, full_name, password_hash, dept_id, juris_id),
    )
    user_id = cur.fetchone()["id"]
    cur.execute("SELECT id FROM app.role WHERE code = %s", (role_code,))
    role_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO app.user_role (user_id, role_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, role_id),
    )
    return user_id


def run() -> None:
    with admin_transaction() as cur:
        gj = _jurisdiction(cur, "GJ", "Gujarat", "state", None, "GJ")
        ahm = _jurisdiction(cur, "GJ-AHM", "Ahmedabad District", "district", "GJ", "GJ.GJ-AHM")
        z1 = _jurisdiction(cur, "GJ-AHM-Z1", "Ahmedabad Zone 1", "zone", "GJ-AHM", "GJ.GJ-AHM.GJ-AHM-Z1")

        police = _department(cur, "AHM-POL", "Ahmedabad City Police", "police")
        traffic = _department(cur, "AHM-TRF", "Ahmedabad Traffic Police", "traffic")

        # A handful of real Ahmedabad landmarks so the map looks like a city,
        # not a grid of test points. One camera near an isolated/park zone for
        # the women's-safety demo (services/analytics/rules.py's followed
        # pattern activates on isolated zones + low track count).
        cameras = [
            ("AHM-NAV-0001", "Navrangpura Cross Rd ANPR", traffic, z1, "anpr", 72.5605, 23.0365, "Navrangpura", False),
            ("AHM-CGR-0002", "CG Road Junction", traffic, z1, "fixed", 72.5650, 23.0280, "CG Road", False),
            ("AHM-LGR-0003", "Law Garden Perimeter", police, z1, "dome", 72.5580, 23.0225, "Law Garden", True),
            ("AHM-RIV-0004", "Riverfront Walkway West", police, z1, "ptz", 72.5715, 23.0335, "Sabarmati Riverfront", True),
            ("AHM-SGH-0005", "SG Highway Overbridge ANPR", traffic, z1, "anpr", 72.5145, 23.0395, "SG Highway", False),
        ]
        camera_ids = {}
        for code, name, dept_id, juris_id, ctype, lon, lat, addr, isolated in cameras:
            camera_ids[code] = _camera(cur, code, name, dept_id, juris_id, ctype, lon, lat, addr, isolated)

        admin_hash = hash_password(DEMO_ADMIN_PASSWORD)
        _user(cur, "admin", "State Control Room Admin", admin_hash, None, gj, "state_admin")
        _user(cur, "operator.ahm", "Ahmedabad Control Room Operator", admin_hash, police, ahm, "district_operator")

        # Two demo watchlist entries: one vehicle, one person-by-embedding
        # (a deterministic stub vector — see services/analytics/stages/face.py
        # — not a real enrolled face, so this is safe to ship in a public repo).
        cur.execute(
            """
            INSERT INTO app.watchlist_entry (entry_type, risk_level, plate_number, label,
                                              case_reference, jurisdiction_id, source)
            VALUES ('stolen_vehicle', 'critical', 'GJ01AB1234',
                    '2019 white Maruti Swift, reported stolen', 'FIR-AHM-2026-00417', %s, 'manual')
            ON CONFLICT DO NOTHING
            """,
            (ahm,),
        )
        cur.execute(
            """
            INSERT INTO app.watchlist_entry (entry_type, risk_level, label, case_reference,
                                              jurisdiction_id, source)
            VALUES ('missing_person', 'high', 'Demo missing-person entry (no photo stored)',
                    'MP-AHM-2026-00092', %s, 'manual')
            ON CONFLICT DO NOTHING
            """,
            (ahm,),
        )

        # One machine account + API key for an edge analytics worker, scoped
        # to the state root so a single demo key can front cameras anywhere in
        # the tree (see 008_api_key_user.sql on why 'edge_worker' exists).
        worker_user_id = _user(
            cur, "svc-edge-worker-01", "Edge Worker (machine)", hash_password(secrets.token_urlsafe(32)),
            None, gj, "edge_worker",
        )
        minted = new_api_key()
        cur.execute("SELECT id FROM app.role WHERE code = 'edge_worker'")
        role_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO app.api_key (label, key_prefix, key_hash, jurisdiction_id, role_id, user_id)
            VALUES ('demo edge worker', %s, %s, %s, %s, %s)
            ON CONFLICT (key_prefix) DO NOTHING
            """,
            (minted.prefix, minted.key_hash, gj, role_id, worker_user_id),
        )

    print("Seed complete.\n")
    print(f"  Admin login:      admin / {DEMO_ADMIN_PASSWORD}")
    print(f"  Operator login:   operator.ahm / {DEMO_ADMIN_PASSWORD}")
    print(f"  Edge worker key:  {minted.full_key}")
    print("\nThe API key is shown once — store it in the worker's environment as ANALYTICS_API_KEY.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        print(f"seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
