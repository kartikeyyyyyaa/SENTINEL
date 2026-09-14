<div align="center">

# 🛡️ Sentinel

### Integrated CCTV Command & Response Platform

*See every camera. Recognise the vehicle or incident. Trace its route across cameras.*
*Send it to the nearest responder — jurisdiction-scoped and fully audited.*

![Model](https://img.shields.io/badge/reference-Model%201%20%2B%202%20%2B%203-1f6feb)
![Backend](https://img.shields.io/badge/backend-FastAPI%20%2B%20PostgreSQL%2FPostGIS-3776ab)
![AI](https://img.shields.io/badge/AI-YOLOv8%20%2B%20PaddleOCR%20%2B%20ReID-00b4d8)
![Frontend](https://img.shields.io/badge/console-Leaflet%20%2B%20HLS-f26522)
![Security](https://img.shields.io/badge/security-RLS%20%2B%20hash--chained%20audit-2ea043)
![Hackathon](https://img.shields.io/badge/Gujarat%20Police-CCTV%20Hackathon%202026-0b2138)

</div>

---

Sentinel turns a passive wall of CCTV feeds into an **active command-and-response system**. It
implements the Gujarat Police CCTV Hackathon's three reference models — **Registry (1)**,
**Federation (2)**, **Viewer + Analytics (3)** — as one layered platform, and adds a response
layer: when a camera recognises a vehicle or an incident, Sentinel reconstructs that vehicle's
**route across the whole camera grid** and **routes the alert to the nearest responder** (PCR van,
hospital, or fire station), then escalates to the state control room.

> **The core task:** onboard the live camera grid; given a vehicle, output its complete,
> timestamped route across cameras — on real feeds, no mockups. Sentinel does this with an
> **ANPR-anchor + appearance re-identification** approach that keeps working when plates are
> unreadable.

---

## ✨ Highlights

| | Capability | What it gives an operator |
|---|---|---|
| 🚗 | **Cross-camera vehicle tracking** | A vehicle's timestamped route across cameras (ANPR + appearance re-ID). *Verified: same vehicle matched at 0.98–0.99 across 3 camera views.* |
| 🚨 | **Auto response-routing** | Incident → **nearest** PCR / hospital / fire unit → control room, drawn live on the map. |
| 🛰️ | **Live Ops console** | Every camera on a live map, real-time alerts, live video + video wall, cross-camera trails. |
| 🩺 | **Camera Health** | Live / faulty / never-checked per camera — honest, never a fabricated uptime. |
| 📊 | **Incident Reports** | Area-wise, week-by-week counts of what the platform itself detected. |
| 👩‍🦰 | **Women's-Safety layer** | Curated high-risk "red zones", one-touch SOS, women's-safety alert prioritisation. |
| 🔒 | **Security by construction** | Row-level-security jurisdiction scoping + append-only hash-chained audit trail. |
| 🌐 | **Federation** | ONVIF/RTSP, Genetec, Milestone and the government grid behind one interface. |
| 🗣️ | **Tri-lingual, dark/light** | English · ગુજરાતી · हिंदी, in the official site's Montserrat / navy-orange theme. |

---

## 🎥 Demo (proven output)

Run on a controlled 3-camera scenario — the model followed the **same vehicle across all three
cameras** despite different scale, lighting and tint, and ignored pedestrians:

```json
{ "target_type": "bus",
  "sightings": [
    { "camera": "cam01", "role": "enrolled" },
    { "camera": "cam02", "t_sec": 0.17, "score": 0.978 },
    { "camera": "cam03", "t_sec": 0.17, "score": 0.987 } ] }
```

That `route.json` — *which camera, at what time* — is exactly the scored deliverable, produced by
appearance features (shape/structure), not plate text and not colour.

---

## 🏗️ Architecture

```
        ┌──────────────────────────────────────────────────────────────┐
        │        OPERATOR CONSOLE  ·  Model 3 (viewer)                   │
        │  Live Ops · Camera Health · Incident Reports · Safety Map      │
        └───────────────▲───────────────────────────▲──────────────────┘
                        │ REST / SSE                 │ HLS video (proxied)
        ┌───────────────┴───────────────────────────┴──────────────────┐
        │        REGISTRY  ·  Model 1  (FastAPI + PostgreSQL/PostGIS)    │
        │  auth · RLS scoping · cameras · watchlist · alerts · audit     │
        │  camera-health · reports · safety-zones · responders          │
        └───────────────▲───────────────────────────▲──────────────────┘
          assignments / │ events                     │ camera metadata
        ┌───────────────┴──────────┐      ┌──────────┴──────────────────┐
        │  ANALYTICS · Model 3 (AI) │      │  FEDERATION · Model 2       │
        │  motion→detect→plate/OCR  │      │  ONVIF/RTSP · Genetec ·     │
        │  →face→tracker→re-ID→rules│      │  Milestone · sentinel_grid  │
        └───────────────▲──────────┘      └──────────┬──────────────────┘
                        │  RTSP / HLS frames          │
              ┌─────────┴─────────────────────────────┴───────┐
              │      GUJARAT CCTV GRID  (cam01 … cam30/50)     │
              └───────────────────────────────────────────────┘
```

- **Model 1 — Registry & GIS** (`services/registry`): system of record. JWT + refresh auth,
  **row-level security** (every query auto-scoped to the officer's jurisdiction & role), camera
  registry with PostGIS, watchlist, alerts, **append-only hash-chained audit**, 11 migrations.
- **Model 2 — Federation / ingestion** (`services/registry/app/federation`, `services/analytics/source.py`):
  vendor adapters + a read-only RTSP capture hardened to the grid's real failure modes (force TCP,
  PTS timing, backoff, scene-cut recovery).
- **Model 3 — Viewer + Analytics** (`web/console`, `services/analytics`): the operator console and
  the edge AI cascade.

---

## 🧠 How the AI works — *plates are hard, so we track by features*

**ANPR anchors, appearance re-identification follows.** Read the plate when the crop is large and
sharp enough (to lock onto the given vehicle number); everywhere else, follow the **same vehicle**
across cameras with a **CNN appearance embedding** that captures shape/structure — not colour,
which changes with lighting. This produces the required cross-camera route even when OCR fails.

```
detect (YOLOv8)  →  plate localise + read (PaddleOCR)   ─┐  anchor to the given number
                 →  appearance embedding (ResNet / YOLO) ─┘  follow across cameras
                 →  tracker + rules  →  route.json + alerts
```

---

## 🚀 Quickstart

```bash
# Backend (Model 1 + 2)
docker compose up -d
docker compose run --rm api python -m app.migrate up
docker compose run --rm api python -m app.seed

# Console (Model 3 viewer): served by nginx via compose, or open web/console/landing.html

# Analytics worker on a live feed (RTSP / HLS / file)
python -m services.analytics.main --cameras "1=rtsp://<email>:<pass>@103.250.160.189:8554/stream/cam04"
```

**Try the AI in seconds — no backend, no grid:**

```bash
pip install -r services/analytics/requirements.txt        # ultralytics, torch, paddleocr, opencv

# detection + plate reads on any clip
python scripts/test_anpr.py <video-or-URL> --out demo.mp4

# follow one vehicle across cameras → route.json + montage
python scripts/track_vehicle.py enroll <clip> --out query.npz
python scripts/track_vehicle.py scan query.npz cam01=<a> cam02=<b> cam03=<c> --out-dir route_out
```

---

## 🗂️ Project structure

```
sentinel/
├── services/
│   ├── registry/            # Model 1 — FastAPI API, RLS, audit, 9 routers
│   │   ├── app/api/routers/ # auth · cameras · watchlist · alerts · events
│   │   │                     #  · camera-health · reports · safety-zones · analytics
│   │   └── app/federation/  # Model 2 — ONVIF/RTSP · Genetec · Milestone · sentinel_grid
│   └── analytics/           # Model 3 AI — motion·detect·plate·face·tracker·rules·source
├── web/console/             # Model 3 viewer — landing · login · Live Ops · Camera Health
│                             #  · Incident Reports · Safety Map  (+ theme, i18n, nav)
├── db/migrations/           # 11 SQL migrations (core · camera · audit · RLS · watchlist · …)
├── scripts/                 # test_anpr.py · track_vehicle.py  (standalone demos)
├── docs/                    # HLD · SECURITY · SCALE · data-handling & automated-processing (DPDP)
└── docker-compose.yml
```

---

## 🔌 Key APIs

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/login` · `/refresh` · `/logout` | JWT session |
| `GET /api/cameras` | grid, jurisdiction-scoped |
| `GET /api/camera-health` | live / faulty / unknown per camera |
| `GET /api/watchlist` · `/{id}/trail` | watchlist + a vehicle's cross-camera route |
| `GET/POST /api/alerts` · `POST /api/sos` | alert lifecycle + panic |
| `GET /api/reports/incidents?weeks=N` | area-wise weekly aggregate |
| `GET/POST/DELETE /api/safety-zones` | curated women's-safety risk zones |
| `GET /api/events/stream` | live SSE feed to the console |

---

## 🔒 Security, privacy & honesty

- **Authorization is enforced in the database** via PostgreSQL row-level security — the app only
  asserts the user id; jurisdiction scope and permissions are derived in SQL.
- **Every access** is written to an append-only, **hash-chained audit trail**.
- **No fabricated data.** Incident Reports aggregate the platform's own alert history (not official
  crime statistics); Camera Health shows the last real probe (never a made-up uptime); safety zones
  are curated police judgement (not measured crime data). Anything synthetic is labelled **DEMO**.
- Two DPDP-Act-2023-grounded disclosures live in `docs/` and are linked from the console.

---

## 📊 Status

| Area | Done |
|---|---|
| Model 1 — Registry & GIS | ▓▓▓▓▓▓▓▓░ 85% |
| Model 2 — Federation / ingestion | ▓▓▓▓▓▓▓░░ 75% |
| Model 3 — Console / viewer | ▓▓▓▓▓▓▓▓▓ 90% |
| Model 3 — Analytics / AI | ▓▓▓▓▓▓▓░░ 70% |
| Vehicle route across cameras | ▓▓▓▓▓▓▓░░ 75% (proven on generated footage) |
| Response routing (PCR/hospital/fire) | ▓▓▓▓▓▓▓▓░ 80% |
| Women's safety (zones / SOS / map) | ▓▓▓▓▓▓▓▓░ 80% |
| Security, privacy & audit | ▓▓▓▓▓▓▓▓░ 85% |

---

## 🗺️ Roadmap

- Live-grid run + captured route on real government footage
- Dedicated **fire** and **accident** detectors to auto-trigger those response routes
- ANPR reliability pass (plate gates + fuzzy matching)
- Real PCR / hospital / fire rosters; data-driven safety zones once history accumulates
- Confirm ~50-feed throughput on demo hardware (see `docs/SCALE.md`)

---

## 👥 Team & 📄 License

Built for the **Gujarat Police CCTV Integration Hackathon 2026** · i-Hub Gujarat.
Team: _add your names here_ · License: _add (e.g. MIT)_.

<div align="center"><sub>Sentinel — we'd rather show a smaller number that's true than a bigger one that isn't.</sub></div>
