# Sentinel — Integrated CCTV Command & Response Platform

**Submission for the Gujarat Police CCTV Integration Hackathon 2026.**

Sentinel turns a passive wall of CCTV feeds into an active command-and-response system. It
implements the hackathon's three reference models — **Model 1 (Registry & GIS)**, **Model 2
(Federation / ingestion)** and **Model 3 (Viewer + Analytics)** — as one layered platform, and
adds a response layer on top: when a camera recognises something, Sentinel doesn't just raise an
alert, it **routes that incident to the nearest responder** (PCR van, hospital, or fire station)
and reconstructs the **movement of a vehicle across the whole camera grid**.

> One line: *See every camera. Recognise the vehicle or incident. Trace its route across cameras.
> Send it to the nearest responder. All jurisdiction-scoped and fully audited.*

---

## Table of contents

1. [The problem we are solving](#1-the-problem-we-are-solving)
2. [System architecture](#2-system-architecture)
3. [Feature catalogue — what each does & how](#3-feature-catalogue)
   - [3.1 Live Ops console](#31-live-ops-console)
   - [3.2 Cross-camera vehicle tracking (the route)](#32-cross-camera-vehicle-tracking--the-route)
   - [3.3 Auto response-routing — PCR / hospital / fire](#33-auto-response-routing--pcr--hospital--fire)
   - [3.4 Women's safety — red zones, dark areas, SOS](#34-womens-safety--red-zones-dark-areas-sos)
   - [3.5 Camera Health monitoring](#35-camera-health-monitoring)
   - [3.6 Incident Reports (area-wise, weekly)](#36-incident-reports-area-wise-weekly)
   - [3.7 Analytics / AI pipeline](#37-analytics--ai-pipeline)
   - [3.8 Federation & ingestion](#38-federation--ingestion)
   - [3.9 Security, privacy & audit](#39-security-privacy--audit)
4. [How the AI works — plates are hard, so we track by features](#4-how-the-ai-works)
5. [Data model & key APIs](#5-data-model--key-apis)
6. [How to run](#6-how-to-run)
7. [Completion status](#7-completion-status)
8. [Roadmap / what's next](#8-roadmap)
9. [Honesty notes for evaluators](#9-honesty-notes-for-evaluators)

---

## 1. The problem we are solving

A city CCTV grid produces far more video than humans can watch. Two questions matter most to
police: **"where did this vehicle go?"** (trace a suspect across cameras) and **"something is
happening — who do we send, and how fast?"** Sentinel answers both automatically, on the live
grid, and keeps a defensible record of every action for accountability.

The designated live test: onboard ~50 RTSP camera feeds; given a vehicle, output its complete
route across cameras with timestamped, location-wise movement history — on real feeds, no mockups.

---

## 2. System architecture

```
                 ┌─────────────────────────────────────────────────────────┐
                 │                    OPERATOR CONSOLE (Model 3 viewer)      │
                 │  Live Ops · Camera Health · Incident Reports · Safety Map │
                 └───────────────▲───────────────────────▲──────────────────┘
                                 │ REST / SSE             │ HLS video (proxied)
                 ┌───────────────┴───────────────────────┴──────────────────┐
                 │              REGISTRY  (Model 1: FastAPI + PostgreSQL/PostGIS)  │
                 │  auth · RLS scoping · cameras · watchlist · alerts · audit │
                 │  camera-health · reports · safety-zones · responders      │
                 └───────────────▲───────────────────────▲──────────────────┘
             assignments / events│                       │ camera metadata
                 ┌───────────────┴──────────┐   ┌─────────┴──────────────────┐
                 │  ANALYTICS (Model 3 AI)   │   │  FEDERATION (Model 2)      │
                 │  motion→detect→plate/OCR  │   │  ONVIF/RTSP · Genetec ·    │
                 │  →face→tracker→re-ID→rules│   │  Milestone · sentinel_grid │
                 └───────────────▲──────────┘   └─────────┬──────────────────┘
                                 │ RTSP/HLS frames         │
                        ┌────────┴─────────────────────────┴────────┐
                        │        GUJARAT CCTV GRID (cam01…cam30/50)  │
                        └────────────────────────────────────────────┘
```

- **Model 1 — Registry & GIS** (`services/registry`): the system of record. JWT auth + refresh,
  **row-level security** so every query is automatically scoped to the officer's jurisdiction and
  role, camera registry with PostGIS locations, watchlist, alerts, an append-only **hash-chained
  audit trail**, 11 SQL migrations, and government-grid onboarding.
- **Model 2 — Federation / ingestion** (`services/registry/app/federation`, `services/analytics/source.py`):
  pluggable adapters (ONVIF/RTSP, Genetec, Milestone, sentinel_grid) and a read-only RTSP capture
  hardened to the grid's real failure modes.
- **Model 3 — Viewer + Analytics** (`web/console`, `services/analytics`): the operator console and
  the edge AI pipeline.

---

## 3. Feature catalogue

Each feature below lists **what it does** and **how it works**.

### 3.1 Live Ops console
**What:** the operator's main screen — every camera on a live satellite/dark map with status
pins, a searchable camera list, a real-time alert feed (watchlist / women's-safety / other),
live HLS video per camera and a video wall, plus a "Track a vehicle" tab.
**How:** `web/console/index.html` + `app.js` render a Leaflet map (free, keyless Esri basemaps —
satellite or dark, toggleable). Cameras and alerts come from the registry over REST + a
Server-Sent-Events stream; when there's no backend the console falls back to a clearly-labelled
DEMO dataset so it always demonstrates. Video is proxied same-origin through nginx so the grid
password never reaches the browser.

### 3.2 Cross-camera vehicle tracking — the route
**What:** the headline, scored capability. Given a vehicle, produce its **timestamped route
across cameras** ("seen at cam01 → cam04 → cam17, with times").
**How:** two complementary mechanisms —
- *ANPR path*: the analytics pipeline reads number plates; a watchlist entry accumulates
  `watchlist_match` rows (camera + timestamp), and the console's "Track a vehicle" tab draws that
  trail on the map with speed/heading read-outs.
- *Appearance path* (`scripts/track_vehicle.py`): where the plate is unreadable, the same vehicle
  is followed by a **CNN appearance embedding** (shape/structure, not colour). `enroll` builds a
  descriptor of the target; `scan` finds it across feeds and emits `route.json` + a montage.
  Verified: same vehicle matched at **0.98–0.99** across three different camera looks.

### 3.3 Auto response-routing — PCR / hospital / fire
**What:** the differentiator. A recognised incident is relayed to the **nearest** appropriate unit
first, then escalated to the state control room. The incident type decides the responder:

| Incident | Routed to (nearest) |
|---|---|
| Crime / women's-safety / SOS | **PCR van** |
| Road accident | **Hospital** |
| Fire | **Fire station** |

**How:** responder locations (demo: 11 PCRs, 11 hospitals, 11 fire stations + the Gujarat State
Control Room) live in `api.js`; `app.js` computes the nearest unit with the Haversine formula,
draws the incident → responder → control-room relay on the map, and animates a three-step
"Auto-response relay" panel (tinted per responder type) showing distance and ETA. It fires
automatically on serious alerts, and a **"Simulate incident"** control lets you trigger the
crime / accident / fire scenarios on demand for a demo. On selection, the demo rosters are
replaced by the department's real PCR/hospital/fire lists via a `/api/responders` route.

### 3.4 Women's safety — red zones, dark areas, SOS
**What:** a dedicated women's-safety layer: a curated map of **high-risk / "shady" areas** (the
red zones / dark areas), a one-touch **SOS**, and a **Women's-Safety mode** that prioritises
safety alerts and overlays the risk zones on the operator's map.
**How:**
- *Risk zones* — `db/migrations/011_safety_zone.sql` defines an `app.safety_zone` table
  (name, PostGIS centre, radius, `risk_level` low/medium/high/critical, `basis='curated'`),
  RLS-scoped, with `GET/POST/DELETE /api/safety-zones`. `web/console/safety-map.html` renders them
  as Leaflet circles coloured by risk; state/district admins can add or remove zones. They also
  appear on the Live Ops map when Women's-Safety mode is on. Zones are **curated police judgement**,
  explicitly *not* algorithmically-inferred crime data — the schema already supports a future
  `basis='alert_derived'` without change.
- *SOS* — `POST /api/sos` (deliberately unauthenticated, exactly as a public kiosk or mobile app
  would send it) opens a **critical** alert immediately and triggers the nearest-PCR relay.

### 3.5 Camera Health monitoring
**What:** which cameras are live, faulty, or unreachable right now, and when each was last checked.
**How:** `GET /api/camera-health` reads `camera_health_current` (RLS-scoped). A camera with no
recorded probe shows **"never checked"** — never a fabricated uptime number. `camera-health.html`
shows live/faulty/unknown counts + a searchable, filterable table.

### 3.6 Incident Reports (area-wise, weekly)
**What:** area-wise, week-by-week counts of what the platform has detected — watchlist matches,
women's-safety flags, SOS calls, etc. — grouped by district for planning and review.
**How:** `GET /api/reports/incidents?weeks=N` aggregates the platform's own `alert` history by
jurisdiction and week (RLS does the scoping). `incident-reports.html` shows totals, a by-district
bar chart and the full weeks/district/kind/severity table. This is Sentinel's own detection
history — **not** a claim of official crime statistics.

### 3.7 Analytics / AI pipeline
**What:** the edge worker that turns pixels into metadata: detect objects → localise & read plates
→ (optional) face → track → apply rules → emit events/alerts.
**How:** `services/analytics` runs a cheapest-first cascade — motion gate → **YOLOv8** detection →
geometric plate localisation → **PaddleOCR** read → tracker (per-vehicle dedupe) → rule engine.
One process, one thread per camera, a shared model — designed for a ~50-feed live run where "at
least one camera will be broken." Metadata-only by default; raw video is proxied live, never stored.

### 3.8 Federation & ingestion
**What:** connect to cameras and VMS regardless of vendor.
**How:** adapters for **ONVIF/RTSP** (direct cameras), **Genetec** and **Milestone** (departmental
VMS), and the **sentinel_grid** (the hackathon gateway). `source.py` is a read-only RTSP capture
written to the grid's documented failure modes: force TCP, drive timing from PTS (not arrival),
tolerate inter-frame gaps, exponential-backoff reconnect, ignore join-time decoder warnings,
recover from the loop-point scene cut.

### 3.9 Security, privacy & audit
**What:** access is limited to what an officer's jurisdiction and role permit, and every action is
recorded tamper-evidently.
**How:** authorization is enforced by **PostgreSQL row-level security** — the app only asserts the
user id; jurisdiction scope, permissions and statewide status are derived in SQL. Every access is
written to an **append-only, hash-chained audit trail** (`app.audit_log`). Two DPDP-Act-2023-grounded
disclosure docs (`docs/DATA_HANDLING_AND_RETENTION.md`, `docs/AUTOMATED_PROCESSING_DISCLOSURE.md`)
are linked from the console's Data & Privacy panel.

---

## 4. How the AI works

**Plates on wide street cameras are small and blurred — so we don't rely on OCR alone.** Our
approach is **"ANPR anchors, appearance re-identification follows":** read the plate when the crop
is large and sharp enough (to lock onto the given vehicle number); everywhere else, follow the
*same vehicle* across cameras by a CNN appearance embedding that captures shape and structure
(not colour, which changes with lighting). This still produces the required cross-camera route
when OCR fails, and it's the honest answer to the "plate reading is unreliable" objection.

- Detection: YOLOv8 (COCO vehicle + person classes).
- Appearance embedding: torchvision ResNet18 feature, with a fallback to the detector's own CNN
  embedding so it runs even offline.
- Matching: cosine similarity, gated by coarse vehicle type; the nearest match per camera above a
  threshold becomes a sighting; sightings ordered by time = the route.

---

## 5. Data model & key APIs

Registry routers (`services/registry/app/api/routers`): `auth`, `cameras`, `watchlist`, `alerts`,
`events` (SSE), `analytics` (worker assignments/events), `health`, `reports`, `safety_zones`.

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/login` `/refresh` `/logout` | JWT session |
| `GET /api/cameras` | grid, jurisdiction-scoped |
| `GET /api/camera-health` | live/faulty/unknown per camera |
| `GET /api/watchlist` · `/{id}/trail` | watchlist + a vehicle's cross-camera route |
| `GET/POST /api/alerts` · `POST /api/sos` | alert lifecycle + panic |
| `GET /api/reports/incidents?weeks=N` | area-wise weekly aggregate |
| `GET/POST/DELETE /api/safety-zones` | curated women's-safety risk zones |
| `GET /api/events/stream` | live SSE feed to the console |

11 migrations under `db/migrations` (core, camera, audit, RLS, reference data, token rotation,
watchlist, api-key user, SOS insert, audit insert, safety_zone).

---

## 6. How to run

```bash
# Backend (Model 1 + 2)
docker compose up -d
docker compose run --rm api python -m app.migrate up
docker compose run --rm api python -m app.seed

# Console (Model 3 viewer): served by nginx via compose; or open web/console/landing.html

# Analytics worker on a live feed (RTSP / HLS / file all supported)
python -m services.analytics.main --cameras "1=rtsp://<email>:<pass>@103.250.160.189:8554/stream/cam04"

# Standalone model tests (no backend, no grid needed)
python scripts/test_anpr.py <video-or-URL> --out demo.mp4                 # detection + plate reads
python scripts/track_vehicle.py enroll <clip> --out query.npz             # pick the target vehicle
python scripts/track_vehicle.py scan query.npz cam01=<a> cam02=<b> cam03=<c> --out-dir route_out
```

Analytics deps: `pip install -r services/analytics/requirements.txt` (ultralytics, torch/
torchvision, paddleocr, opencv). CPU works; first run downloads model weights.

---

## 7. Completion status

| Area | Done |
|---|---|
| Model 1 — Registry & GIS | **85%** |
| Model 2 — Federation / ingestion | **75%** |
| Model 3 — Console / viewer | **90%** |
| Model 3 — Analytics / AI | **70%** |
| Core scored task — vehicle route across cameras | **75%** (proven on generated footage) |
| Response routing (PCR / hospital / fire) | **80%** |
| Women's safety (zones / SOS / map) | **80%** |
| Security, privacy & audit | **85%** |
| Docs & deck | **85%** |
| Automated tests (9 suites) | **60%** |
| Submission logistics (upload, demo video, live-grid run) | **30%** ⚠️ |

**Engineering build ≈ 80% · Submission-readiness ≈ 65%.**

---

## 8. Roadmap

1. Run the pipeline on the live government grid and capture the route on real footage.
2. Record the demo video; fix the GitHub upload and submit.
3. ANPR reliability pass (plate gates + fuzzy matching) on real reads.
4. Dedicated **fire** and **accident** detectors to auto-trigger those routes (today they route via
   real alerts + the Simulate control).
5. Replace demo PCR/hospital/fire and safety-zone data with real departmental rosters.
6. Data-driven safety zones (`basis='alert_derived'`) once enough history accumulates.
7. Confirm ~50-feed throughput on the demo hardware (see `docs/SCALE.md`).

---

## 9. Honesty notes for evaluators

Everything synthetic is labelled **DEMO** in the UI. Incident Reports aggregate Sentinel's own
alert history (not official crime statistics); Camera Health shows the last real probe (never a
fabricated uptime); women's-safety zones are curated police judgement (not measured crime data);
responder rosters are demo placeholders until real ones are provided. Every read is jurisdiction-
scoped at the database level and written to a tamper-evident audit trail. We would rather show a
smaller number that is true than a larger one that is not.
