"""Onboard the government camera grid — the hackathon's live test case, wired.

The FAQ's live test is: onboard ~30 geographically-distributed government RTSP
feeds, monitor them centrally, and track a designated vehicle across them. This
tool is the "onboard" half — it turns the grid's catalogue into rows in
``app.camera`` so the cameras appear on the console map with real Gujarat
locations and the analytics worker can be assigned to them
(``/api/analytics/assignments``).

**Catalogue-driven, not hard-coded.** It reads the grid's own catalogue
(``GRID_CATALOGUE_URL``, default ``https://cctv.corp8.cloud/cameras.json``) so a
change to the camera set is picked up on the next run — the FAQ is explicit that
the catalogue is the contract and the id list can change. The catalogue sits
behind the grid's access login, so an unauthenticated or network-blocked fetch
falls back to the bundled snapshot in ``fixtures/gov_cameras.json``; either way
the run is identical downstream.

**Locations come from a local overlay, because the catalogue has none.** The
real ``cameras.json`` is ``{id, name}`` only — no coordinates — so
``fixtures/gov_camera_locations.json`` supplies lat/lon, district and
camera_type per id. A camera whose id has no location entry is skipped with a
warning rather than dropped onto (0,0), because ``app.camera.location`` is NOT
NULL and a wrong point is worse than a known gap.

**Credentials are never stored here.** The RTSP URL carries the participant's
registered email + access password, and that is assembled at request time by
``/api/analytics/assignments`` from ``GRID_EMAIL`` / ``GRID_PASSWORD`` in the
environment — this row only records the CDN HLS URL (which needs no inline
credential) and the ``vms_camera_id`` the assignment endpoint builds the RTSP
URL from.

    python -m app.onboard_grid            # live catalogue, else bundled snapshot
    python -m app.onboard_grid --offline  # force the bundled snapshot

Idempotent: re-running updates in place (``ON CONFLICT`` on natural keys), so it
is safe after a ``docker-compose down -v`` + migrate + seed, and safe to re-run
when the catalogue changes.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import get_settings
from .db import admin_transaction

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CATALOGUE_FALLBACK = _FIXTURES / "gov_cameras.json"
_LOCATIONS = _FIXTURES / "gov_camera_locations.json"

# District -> (jurisdiction code, display name). Ahmedabad reuses the code the
# seed already creates (GJ-AHM) so the two do not create duplicate districts.
_DISTRICTS = {
    "Ahmedabad": ("GJ-AHM", "Ahmedabad District"),
    "Gandhinagar": ("GJ-GNR", "Gandhinagar District"),
    "Junagadh": ("GJ-JND", "Junagadh District"),
    "Gir Somnath": ("GJ-GSM", "Gir Somnath District"),
    "Rajkot": ("GJ-RJT", "Rajkot District"),
    "Navsari": ("GJ-NVS", "Navsari District"),
    "Patan": ("GJ-PTN", "Patan District"),
    "Banaskantha": ("GJ-BK", "Banaskantha District"),
    "Aravalli": ("GJ-ARV", "Aravalli District"),
    "Kutch": ("GJ-KTC", "Kutch District"),
}


def _load_catalogue(offline: bool) -> list[dict]:
    """The grid's ``{id, name}`` list — live if reachable, else the snapshot."""
    settings = get_settings()
    if not offline and settings.grid_catalogue_url:
        try:
            req = urllib.request.Request(settings.grid_catalogue_url, method="GET")
            if settings.grid_email and settings.grid_password:
                token = base64.b64encode(
                    f"{settings.grid_email}:{settings.grid_password}".encode()
                ).decode()
                req.add_header("Authorization", f"Basic {token}")
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read().decode("utf-8")
            data = json.loads(body)
            rows = data.get("cameras", data) if isinstance(data, dict) else data
            if isinstance(rows, list) and rows:
                print(f"catalogue: fetched {len(rows)} cameras live from {settings.grid_catalogue_url}")
                return rows
            print("catalogue: live response was empty or unexpected; using bundled snapshot")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            print(f"catalogue: live fetch failed ({exc}); using bundled snapshot")
    rows = json.loads(_CATALOGUE_FALLBACK.read_text(encoding="utf-8"))
    print(f"catalogue: loaded {len(rows)} cameras from {_CATALOGUE_FALLBACK.name}")
    return rows


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


def _camera(cur, *, code, name, dept_id, juris_id, camera_type, lon, lat, address,
            vms_camera_id, hls_url) -> int:
    cur.execute(
        """
        INSERT INTO app.camera (
            code, name, department_id, jurisdiction_id, camera_type, location,
            address, status, vms_platform, vms_camera_id, external_id, hls_url, source
        )
        VALUES (
            %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            %s, 'active', 'sentinel_grid', %s, %s, %s, 'api'
        )
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            location = EXCLUDED.location,
            camera_type = EXCLUDED.camera_type,
            jurisdiction_id = EXCLUDED.jurisdiction_id,
            vms_camera_id = EXCLUDED.vms_camera_id,
            hls_url = EXCLUDED.hls_url,
            updated_at = now()
        RETURNING id
        """,
        (code, name, dept_id, juris_id, camera_type, lon, lat, address,
         vms_camera_id, vms_camera_id, hls_url),
    )
    return cur.fetchone()["id"]


def run(offline: bool = False) -> None:
    catalogue = _load_catalogue(offline)
    overlay = json.loads(_LOCATIONS.read_text(encoding="utf-8")).get("cameras", {})

    onboarded, skipped, low_conf = 0, [], []
    with admin_transaction() as cur:
        gj = _jurisdiction(cur, "GJ", "Gujarat", "state", None, "GJ")
        dept = _department(cur, "GJ-GRID", "Gujarat State CCTV Grid", "police")

        district_ids: dict[str, int] = {}
        for district, (code, disp) in _DISTRICTS.items():
            district_ids[district] = _jurisdiction(cur, code, disp, "district", "GJ", f"GJ.{code}")

        for entry in catalogue:
            cam_id = str(entry.get("id") or "").strip()
            if not cam_id:
                continue
            loc = overlay.get(cam_id)
            if not loc:
                skipped.append(cam_id)
                continue
            district = loc.get("district") or "Ahmedabad"
            juris_id = district_ids.get(district) or gj
            code = f"GRID-{cam_id.upper()}"
            hls_url = f"https://cctv.corp8.cloud/{cam_id}/index.m3u8"
            _camera(
                cur,
                code=code,
                name=str(entry.get("name") or cam_id),
                dept_id=dept,
                juris_id=juris_id,
                camera_type=loc.get("camera_type", "fixed"),
                lon=float(loc["lon"]),
                lat=float(loc["lat"]),
                address=f"{loc.get('city', '')}, {district}".strip(", "),
                vms_camera_id=cam_id,
                hls_url=hls_url,
            )
            onboarded += 1
            if loc.get("confidence") == "low":
                low_conf.append(f"{cam_id} ({loc.get('city') or '?'})")

    print(f"\nOnboarded {onboarded} government cameras into the registry.")
    if skipped:
        print(f"  Skipped {len(skipped)} with no location overlay: {', '.join(skipped)}")
    if low_conf:
        print(
            "  Low-confidence coordinates (verify against the real location):\n    "
            + "\n    ".join(low_conf)
        )
    print(
        "\nThey now appear on the console map and are assignable to the worker via "
        "GET /api/analytics/assignments.\nSet GRID_EMAIL / GRID_PASSWORD in the "
        "registry environment to have that endpoint hand out authenticated RTSP URLs; "
        "leave them unset and the worker runs these as stubs (safe on a blocked network)."
    )


if __name__ == "__main__":
    try:
        run(offline="--offline" in sys.argv)
    except Exception as exc:  # noqa: BLE001
        print(f"onboard_grid failed: {exc}", file=sys.stderr)
        sys.exit(1)
