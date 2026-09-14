# Scaling to ~80,000 Cameras

The hackathon's scale target is roughly 80,000 cameras across Gujarat. This
document is the sizing plan: hardware and AI-processing capacity, network and
bandwidth, storage and retention, disaster recovery, and a phased statewide
rollout. Where a number is **measured**, it is labelled as such; where it is an
**assumption** to validate on the real edge hardware, it says so. Sentinel is
not designed to be *made* to scale later — the architecture's central decision
exists precisely because of this number.

## 1. The decision that makes 80,000 cameras possible: metadata, not video

Streaming 80,000 cameras of raw video to a central site is infeasible and it is
not what Sentinel does. At a typical 2–4 Mbps per H.264/H.265 stream, 80,000
cameras is **160–320 Gbps** of continuous ingress — before a single frame is
analysed. No central facility a state can afford absorbs that, and it is pure
waste, because 99.9% of those frames contain nothing anyone will ever look at.

Sentinel therefore **analyses at the edge and moves only metadata.** Each edge
worker sits near a cluster of cameras, runs the detection/ANPR/tracking cascade
locally, and emits small JSON facts — a plate read, a watchlist match, a
camera-health beat. Continuous video never leaves the camera site. This is the
same split every layer of the system already enforces (small metadata at the
edge, detail behind an authenticated read), and it is what turns an impossible
bandwidth problem into a trivial one (see §3).

## 2. AI processing capacity — cameras per edge box

**The measured result (from `python -m services.analytics.main --self-test`):**
the cascade is a funnel that spends the expensive stages on almost nothing. In
one self-test run over 480 frames with 1,920 detected objects, the pipeline made
just **4 OCR calls — about 8 OCR calls per 1,000 frames** — because a geometric
plate gate rejects most objects before any crop, and a once-per-track dedupe
means a vehicle crossing frame for 40 frames is read once, not 40 times. OCR,
the single most expensive operation, is therefore *not* the scaling bottleneck;
detection frequency is, and that is bounded by an adaptive sampler that widens a
camera's frame stride under load rather than falling behind real time.

> Note: the self-test runs with stub models to stay GPU-free, so its absolute
> millisecond figures measure pipeline overhead, not real inference. The *funnel
> ratios above are real and model-independent* — they are what the gates and
> dedupe do — and they are the reason a modest box serves many cameras. The
> per-camera counts below combine those ratios with realistic inference costs
> that must be confirmed on the chosen edge hardware.

**Sizing method and a worked range (assumptions to validate):**

- Detection: YOLO-class detector exported to INT8 ONNX, ~5–8 ms/frame on an
  edge GPU (e.g. NVIDIA Jetson Orin NX class), analysing a sampled subset of
  frames per camera (motion-gated, adaptive), not every frame.
- Plate OCR: ~30–50 ms/crop, but invoked ~8× per 1,000 frames (measured), so
  its amortised per-camera cost is small.
- One worker process shares one model instance across several camera threads
  (inference serialised behind a lock — safe and cache-friendly), so model
  weights are resident once per box, not once per camera.

On these assumptions a single mid-tier edge GPU box serves on the order of
**25–50 cameras**. That gives a statewide fleet of roughly **1,600–3,200 edge
boxes** for 80,000 cameras — distributed by district, each box owning a local
camera cluster. The exact ratio is a hardware-procurement decision; the point is
that it is linear, bounded, and per-district, with no central inference tier to
become a bottleneck.

**Hardware & software per tier:**

| Tier | Runs | Rough spec |
|---|---|---|
| Edge worker box | detection/ANPR/tracking cascade for a camera cluster | 1 edge GPU (Jetson Orin NX / small T4 node), 8–16 GB RAM, INT8 models; stateless |
| District aggregation (optional) | regional bus/relay if a district prefers not to reach centre directly | commodity VM |
| Central registry | Model 1 API, RLS, audit, watchlist, alert store | PostgreSQL 16 + PostGIS, primary + read replica(s); the API is stateless and horizontally scalable behind a load balancer |
| Console | operator UI | static files behind nginx; stateless |

Edge boxes hold **no durable state** — they pull their camera assignments and
the active watchlist from the registry on start and on a refresh interval — so a
failed box is replaced and re-pulls, with no data migration.

## 3. Network & bandwidth

Because only metadata moves centrally, upstream bandwidth per camera is **KB/s,
not Mb/s**. A concrete envelope:

- Primitive events (plate reads, track updates) are **broadcast locally, not
  persisted centrally** — a high-volume observed fact is not the operational
  record the central store exists to keep. Only *judgements* (alerts) and
  watchlist-match rows travel to and persist at the centre.
- An alert is ~1–2 KB of JSON. Even at a deliberately generous 100 alerts per
  camera per day, 80,000 cameras produce on the order of **16 GB/day** of
  central ingress statewide — a few Mbps averaged, trivially handled, and four
  to five orders of magnitude below the 160–320 Gbps that raw video would cost.
- Camera-health beats are a few bytes per camera per interval — negligible.

The comparison is the entire architecture argument in one line: **video-central
is ~200 Gbps and impossible; metadata-central is a few Mbps and boring.**

## 4. Storage & retention

Central storage is metadata only; there is no video store.

- **Static/near-static:** camera registry, jurisdictions, departments,
  watchlist entries — megabytes to low gigabytes, essentially flat.
- **Append-only growth:** `app.audit_log`, `app.watchlist_match`, `app.alert`.
  These are the tables to plan retention around. Strategy: **monthly range
  partitioning** on the timestamp column, with a hot window (e.g. 90 days) kept
  online and older partitions detached to cheaper archival storage or a
  data-warehouse export. Partitioning keeps query plans and index sizes bounded
  as history accumulates, and detaching a partition is a metadata operation, not
  a bulk delete.
- The **audit log is never pruned in place** — it is append-only and
  hash-chained by design (see `SECURITY.md`); archival moves whole sealed
  partitions rather than deleting rows, so the chain remains verifiable.

## 5. Disaster recovery

- **PostgreSQL:** streaming replication to a hot standby in a second facility,
  continuous WAL archiving for point-in-time recovery, and scheduled base
  backups. RPO measured in seconds (replication), RTO in minutes (promote the
  standby).
- **Stateless tiers:** the API, console, and edge workers hold no unique state,
  so recovery is redeploy-and-reconnect. Edge workers re-pull assignments and
  watchlist from the registry automatically.
- **Watchlist availability:** each edge worker keeps its own in-memory copy of
  the active watchlist and keeps matching against the last good copy if the
  registry is briefly unreachable, so a central outage degrades to
  "few-minutes-stale watchlist," never "cameras go blind."

## 6. Scalability of the security model

A common failure mode is that per-row authorization gets more expensive as data
grows. Sentinel's does not: every RLS policy filters on **indexed** columns —
`camera.department_id`, `camera.jurisdiction_id` (both indexed), and the
jurisdiction path — so a scoped query is an index scan whose plan shape does not
change between 5,000 and 80,000 cameras. The single asserted claim
(`app.user_id`) and the `SECURITY DEFINER` derivation functions add a constant
per-transaction cost, not a per-row one. (Verify with `EXPLAIN ANALYZE` on
`GET /api/cameras` against a seeded larger fleet — the plan uses the indexes and
does not table-scan.)

## 7. Phased statewide rollout

The data model is already organised as a jurisdiction tree (state → district →
zone), which is also the natural rollout unit:

1. **Pilot** — one district (e.g. Ahmedabad), a few hundred cameras, one or two
   edge boxes. Prove onboarding, monitoring, ANPR, and cross-camera tracking
   end to end on real feeds.
2. **Region** — extend to a cluster of districts, validating the per-district
   edge-box ratio and the central store under real alert volume.
3. **State** — roll out district by district. Onboarding is catalogue-driven
   (the `onboard_grid` pattern already reads a camera catalogue and registers a
   whole district's cameras with their locations in one run), so each new
   district is a data operation, not a code change. RBAC and RLS already scope
   every district's operators to their own jurisdiction from day one.

Each phase adds edge boxes horizontally and, if needed, Postgres read replicas;
no phase requires re-architecting, because the metadata-not-video decision that
makes 80,000 cameras affordable is in place from the first camera.
