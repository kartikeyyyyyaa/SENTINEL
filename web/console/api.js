/* api.js — the console's only data-access layer.
 *
 * Talks to the real registry API (services/registry/app/api) whenever a valid
 * session exists, and falls back to a bundled, clearly-labelled demo fixture
 * whenever it doesn't — no login, no reachable backend, or a request that
 * fails for any reason. That fallback is not a placeholder to delete later:
 * it is what lets this console be opened and evaluated with zero setup, and
 * the switch to LIVE happens automatically the moment someone signs in
 * against a running registry. window.SentinelAPI.usingDemoData reports which
 * mode is active so app.js can show the DEMO/LIVE badge honestly.
 *
 * Auth: a JWT access token (15 min) plus a refresh token, exactly as
 * api/routers/auth.py issues them. Both live in localStorage — this is a
 * console served from the operator's own machine/kiosk, not a shared
 * in-conversation preview, so persisting a session across a reload is a
 * feature, not a leak (nginx + the deployment's TLS terminate this origin;
 * see web/nginx.conf). authFetch() attaches the access token, and on a 401
 * makes exactly one attempt to refresh before giving up and surfacing the
 * caller's own demo fallback.
 *
 * The demo event generator draws its vocabulary directly from
 * services/common/events.py (OBJECT_CLASSES, PLATED_CLASSES, PRIMITIVE_KINDS)
 * so that what you see here is shaped exactly like what the real edge worker
 * (services/analytics) actually emits, not an invented format.
 */
(function () {
  "use strict";

  const OBJECT_CLASSES = ["person", "bicycle", "motorcycle", "car", "auto_rickshaw", "bus", "truck", "tractor"];
  const PLATED_CLASSES = new Set(["motorcycle", "car", "auto_rickshaw", "bus", "truck", "tractor"]);

  const LS_ACCESS = "sentinel_access_token";
  const LS_REFRESH = "sentinel_refresh_token";
  const LS_USER = "sentinel_user";

  // ---------------------------------------------------------------------
  // Auth
  // ---------------------------------------------------------------------

  function storage() {
    try { return window.localStorage; } catch (e) { return null; }
  }

  function getSession() {
    const s = storage();
    if (!s) return null;
    const access = s.getItem(LS_ACCESS);
    const refresh = s.getItem(LS_REFRESH);
    if (!access || !refresh) return null;
    let user = null;
    try { user = JSON.parse(s.getItem(LS_USER) || "null"); } catch (e) { /* ignore */ }
    return { access, refresh, user };
  }

  function saveSession(tokenResponse) {
    const s = storage();
    if (!s) return;
    s.setItem(LS_ACCESS, tokenResponse.access_token);
    s.setItem(LS_REFRESH, tokenResponse.refresh_token);
    s.setItem(LS_USER, JSON.stringify(tokenResponse.user));
  }

  function clearSession() {
    const s = storage();
    if (!s) return;
    s.removeItem(LS_ACCESS);
    s.removeItem(LS_REFRESH);
    s.removeItem(LS_USER);
  }

  async function login(username, password) {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || "sign-in failed (HTTP " + res.status + ")");
    }
    const data = await res.json();
    saveSession(data);
    return data.user;
  }

  async function logout() {
    const session = getSession();
    clearSession();
    if (!session) return;
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + session.access },
        body: JSON.stringify({ refresh_token: session.refresh }),
      });
    } catch (e) { /* best-effort: the session is cleared locally regardless */ }
  }

  async function tryRefresh() {
    const session = getSession();
    if (!session) return false;
    try {
      const res = await fetch("/api/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: session.refresh }),
      });
      if (!res.ok) { clearSession(); return false; }
      saveSession(await res.json());
      return true;
    } catch (e) {
      return false;
    }
  }

  /** Attaches the bearer token; refreshes once on 401; never throws for auth
   * reasons — callers see a rejected response (ok:false) they can fall back on. */
  async function authFetch(path, opts) {
    opts = opts || {};
    const session = getSession();
    if (!session) return { ok: false, status: 401 };

    async function attempt(token) {
      const headers = Object.assign({}, opts.headers, { Authorization: "Bearer " + token });
      return fetch(path, Object.assign({}, opts, { headers }));
    }

    let res = await attempt(session.access);
    if (res.status === 401) {
      const refreshed = await tryRefresh();
      if (!refreshed) return res;
      const fresh = getSession();
      res = await attempt(fresh.access);
    }
    return res;
  }

  // ---------------------------------------------------------------------
  // Demo camera fixture — the REAL government grid (cctv.corp8.cloud)
  // ---------------------------------------------------------------------
  // These are the 30 live cameras from the hackathon grid's cameras.json,
  // with the same locations the registry onboarding uses
  // (services/registry/app/fixtures/gov_camera_locations.json). Using them as
  // the demo fixture means the Gujarat satellite map is populated with the real
  // grid even before an operator signs in; once signed in, the identical set is
  // served live from the registry. Coordinates marked "verify" in the overlay
  // are best-estimate and can be corrected there.

  const DEMO_CAMERAS = [
    cam(1, "GRID-CAM01", "01 Chiman bhai Bridge", "fixed", "active", 23.0060, 72.5730, "Ahmedabad"),
    cam(2, "GRID-CAM02", "02 Janpath", "fixed", "active", 23.0370, 72.5670, "Ahmedabad"),
    cam(3, "GRID-CAM03", "03 O.N.G.C. Office", "fixed", "active", 23.1010, 72.5810, "Ahmedabad"),
    cam(4, "GRID-CAM04", "04 Paldi Circle", "anpr", "active", 23.0100, 72.5670, "Ahmedabad"),
    cam(5, "GRID-CAM05", "05 Visat teen Rasta", "anpr", "active", 23.1080, 72.5900, "Ahmedabad"),
    cam(6, "GRID-CAM06", "06 Timbavadi gate-Junagadh", "fixed", "active", 21.5050, 70.4680, "Junagadh"),
    cam(7, "GRID-CAM07", "07 hero-showroom-gir-somnath", "fixed", "active", 20.9000, 70.3700, "Gir Somnath"),
    cam(8, "GRID-CAM08", "08 majewadi-gate-junagadh", "fixed", "active", 21.5170, 70.4570, "Junagadh"),
    cam(9, "GRID-CAM09", "09 new-bypass-near-by-circle-junagadh-2", "fixed", "active", 21.5200, 70.4400, "Junagadh"),
    cam(10, "GRID-CAM10", "10 char-chowk-road-2-junagadh", "fixed", "active", 21.5200, 70.4600, "Junagadh"),
    cam(11, "GRID-CAM11", "11 dolatpara-junagadh", "fixed", "active", 21.4800, 70.4400, "Junagadh"),
    cam(12, "GRID-CAM12", "12 Tri Mandir Adalaj Tollnaka", "anpr", "active", 23.1660, 72.5810, "Gandhinagar"),
    cam(13, "GRID-CAM13", "13 CN Vidhyalaya", "fixed", "active", 23.0230, 72.5560, "Ahmedabad"),
    cam(14, "GRID-CAM14", "14 Delight RLVD", "anpr", "active", 23.0300, 72.5800, "Ahmedabad"),
    cam(15, "GRID-CAM15", "15 Suvidha park", "fixed", "active", 23.0400, 72.5300, "Ahmedabad"),
    cam(16, "GRID-CAM16", "16 Visat P2", "anpr", "active", 23.1090, 72.5910, "Ahmedabad"),
    cam(17, "GRID-CAM17", "17 Rajkot Bus Port CCTV", "fixed", "active", 22.3020, 70.7950, "Rajkot"),
    cam(18, "GRID-CAM18", "18 Rajkot CCTV", "fixed", "active", 22.3030, 70.8020, "Rajkot"),
    cam(19, "GRID-CAM19", "19 Khaparia Gram Panchayat, Gandevi", "fixed", "active", 20.8000, 72.9800, "Navsari"),
    cam(20, "GRID-CAM20", "20 Mohanpura", "fixed", "active", 20.8500, 72.9200, "Navsari"),
    cam(21, "GRID-CAM21", "23 Patan Dethali Char Rasta", "fixed", "active", 23.7500, 71.7500, "Patan"),
    cam(22, "GRID-CAM22", "28 BK Mervada tran Rasta", "fixed", "active", 24.1000, 72.4000, "Banaskantha"),
    cam(23, "GRID-CAM23", "30 kheram", "fixed", "active", 23.6000, 72.9000, "Aravalli"),
    cam(24, "GRID-CAM24", "33 dehgam", "fixed", "active", 23.1700, 72.8200, "Gandhinagar"),
    cam(25, "GRID-CAM25", "34 dhanori", "fixed", "active", 23.4000, 73.0000, "Aravalli"),
    cam(26, "GRID-CAM26", "35 TANKAL", "fixed", "active", 20.9500, 72.9000, "Navsari"),
    cam(27, "GRID-CAM27", "36 bilimora", "fixed", "active", 20.7680, 72.9600, "Navsari"),
    cam(28, "GRID-CAM28", "37 bilimora", "fixed", "active", 20.7700, 72.9620, "Navsari"),
    cam(29, "GRID-CAM29", "38 bilimora", "fixed", "active", 20.7660, 72.9580, "Navsari"),
    cam(30, "GRID-CAM30", "Gandhidham Rambaugh p2", "fixed", "active", 23.0750, 70.1330, "Kutch"),
  ];

  function cam(id, code, name, camera_type, status, lat, lon, district) {
    return {
      camera_id: id, code, name, camera_type, status,
      location: { lat, lon },
      bearing_deg: null, fov_deg: null, range_m: null,
      department: "Gujarat State CCTV Grid", jurisdiction_path: district, isolated: false,
    };
  }

  // ---------------------------------------------------------------------
  // Cameras
  // ---------------------------------------------------------------------

  let usingDemoData = true;

  function normaliseCamera(c) {
    // The registry's GET /api/cameras (api/routers/cameras.py) returns
    // {id, lat, lon, ...} flat; the console's own shape (kept from before the
    // API existed) is {camera_id, location:{lat,lon}, ...}. Adapt here, once,
    // rather than threading two shapes through every render function.
    if (c.camera_id !== undefined) return c;
    return {
      camera_id: c.id,
      code: c.code,
      name: c.name,
      camera_type: c.camera_type,
      status: c.status,
      location: { lat: c.lat, lon: c.lon },
      bearing_deg: null,
      fov_deg: null,
      range_m: null,
      department: c.department_id ? "Dept #" + c.department_id : "",
      jurisdiction_path: null,
      isolated: false,
    };
  }

  async function fetchCameras() {
    if (getSession()) {
      try {
        const res = await authFetch("/api/cameras", { headers: { Accept: "application/json" } });
        if (res.ok) {
          const data = await res.json();
          const cameras = (Array.isArray(data) ? data : data.cameras || []).map(normaliseCamera);
          if (cameras.length) { usingDemoData = false; return cameras; }
        }
      } catch (err) { /* fall through to demo */ }
    }
    usingDemoData = true;
    return DEMO_CAMERAS;
  }

  // ---------------------------------------------------------------------
  // Alerts
  // ---------------------------------------------------------------------

  async function fetchAlerts() {
    if (!getSession()) return null;
    try {
      const res = await authFetch("/api/alerts");
      if (!res.ok) return null;
      return await res.json();
    } catch (e) { return null; }
  }

  async function acknowledgeAlert(id) {
    const res = await authFetch("/api/alerts/" + id + "/acknowledge", { method: "POST" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  async function closeAlert(id, reason, falsePositive) {
    const res = await authFetch("/api/alerts/" + id + "/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason || "resolved from console", false_positive: !!falsePositive }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  async function reportSos(payload) {
    // Deliberately plain fetch, not authFetch — POST /api/sos takes no
    // credential by design (see api/routers/alerts.py's module docstring).
    const res = await fetch("/api/sos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // ---------------------------------------------------------------------
  // Watchlist
  // ---------------------------------------------------------------------

  async function searchWatchlist(query) {
    if (!getSession()) return null;
    try {
      const qs = query ? "?q=" + encodeURIComponent(query) : "";
      const res = await authFetch("/api/watchlist" + qs);
      if (!res.ok) return null;
      return await res.json();
    } catch (e) { return null; }
  }

  async function fetchTrail(entryId) {
    const res = await authFetch("/api/watchlist/" + entryId + "/trail");
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // ---------------------------------------------------------------------
  // Events / alerts stream — real SSE if signed in, else synthetic
  // ---------------------------------------------------------------------
  //
  // Real transport is Server-Sent Events on /api/events/stream (api/routers/
  // events.py). Every message is a JSON envelope: {type:"event", event:{...}}
  // for a raw analytics primitive, or {type:"alert.created"|"alert.updated",
  // alert:{...}} for the alert lifecycle. EventSource can't set an
  // Authorization header, so the access token travels as a query parameter —
  // see events.py's module docstring for why that's an acceptable exposure
  // for a token this short-lived.

  function connectEvents(cameras, onMessage) {
    const session = getSession();
    if (session && typeof EventSource !== "undefined") {
      try {
        const source = new EventSource("/api/events/stream?token=" + encodeURIComponent(session.access));
        let receivedReady = false;
        source.addEventListener("ready", () => {
          receivedReady = true;
          usingDemoData = false;
        });
        source.onmessage = (msg) => {
          try { onMessage(JSON.parse(msg.data)); } catch (e) { /* malformed line, skip */ }
        };
        source.onerror = () => {
          if (!receivedReady) {
            source.close();
            startSyntheticFeed(cameras, onMessage);
          }
          // If it had connected and later drops, EventSource retries on its
          // own — that's the whole point of choosing SSE (see module docstring).
        };
        return;
      } catch (err) {
        // fall through to synthetic
      }
    }
    startSyntheticFeed(cameras, onMessage);
  }

  function startSyntheticFeed(cameras, onMessage) {
    usingDemoData = true;
    const sim = new EventSimulator(cameras, (evt) => onMessage({ type: "event", event: evt }));
    sim.start();
  }

  /** Drives a plausible stream of PrimitiveEvent-shaped objects per camera. */
  class EventSimulator {
    constructor(cameras, onEvent) {
      this.cameras = cameras.filter((c) => c.status === "active");
      this.onEvent = onEvent;
      this.tracksByCamera = new Map();
      this.seq = 1;
    }

    start() {
      if (!this.cameras.length) return;
      this.timer = setInterval(() => this.tick(), 1100);
      // A little immediate activity so the panel isn't empty on load.
      for (let i = 0; i < 4; i++) this.tick();
    }

    stop() {
      clearInterval(this.timer);
    }

    tick() {
      for (const camera of this.cameras) {
        let tracks = this.tracksByCamera.get(camera.camera_id);
        if (!tracks) {
          tracks = [];
          this.tracksByCamera.set(camera.camera_id, tracks);
        }

        // End tracks whose lifetime has expired.
        for (let i = tracks.length - 1; i >= 0; i--) {
          const t = tracks[i];
          if (Date.now() > t.endsAt) {
            this.emit(camera, "track_end", t.trackId, {
              class_label: t.classLabel,
              reason: "lost",
              duration_seconds: Math.round((Date.now() - t.startedAt) / 100) / 10,
            });
            tracks.splice(i, 1);
          }
        }

        // Occasionally start a new track.
        if (tracks.length < 3 && Math.random() < 0.35) {
          const classLabel = OBJECT_CLASSES[Math.floor(Math.random() * OBJECT_CLASSES.length)];
          const trackId = "c" + camera.camera_id + "-sim-" + this.seq++;
          const track = {
            trackId,
            classLabel,
            startedAt: Date.now(),
            endsAt: Date.now() + 4000 + Math.random() * 12000,
            platedRead: false,
            bbox: randomBBox(),
          };
          tracks.push(track);
          this.emit(camera, "track_start", trackId, {
            class_label: classLabel,
            confidence: round(0.55 + Math.random() * 0.4),
            bbox: track.bbox,
          });
        }

        // Heartbeat + occasional ANPR / dwell / proximity for live tracks.
        for (const t of tracks) {
          if (Math.random() < 0.6) {
            this.emit(camera, "track_update", t.trackId, {
              class_label: t.classLabel,
              confidence: round(0.5 + Math.random() * 0.45),
              bbox: (t.bbox = jitterBBox(t.bbox)),
              age_seconds: round((Date.now() - t.startedAt) / 1000),
            });
          }
          if (!t.platedRead && PLATED_CLASSES.has(t.classLabel) && Math.random() < 0.18) {
            t.platedRead = true;
            this.emit(camera, "anpr", t.trackId, {
              plate_text: randomPlate(),
              confidence: round(0.7 + Math.random() * 0.28),
              format_valid: Math.random() > 0.12,
              engine: "stub",
            });
          }
          if (Math.random() < 0.02) {
            this.emit(camera, "dwell", t.trackId, {
              zone_id: "zone-demo",
              class_label: t.classLabel,
              dwell_seconds: round(10 + Math.random() * 40),
              still: Math.random() > 0.5,
            });
          }
        }

        if (tracks.length >= 2 && Math.random() < 0.015) {
          const [a, b] = tracks;
          this.emit(camera, "proximity", a.trackId, {
            track_ids: [a.trackId, b.trackId],
            class_labels: [a.classLabel, b.classLabel],
            seconds: round(6 + Math.random() * 10),
            distance_unit: "normalised",
          });
        }

        if (Math.random() < 0.003) {
          this.emit(camera, "stream_gap", null, {
            reason: "stalled",
            gap_seconds: round(5 + Math.random() * 20),
          });
        }
      }
    }

    emit(camera, kind, trackId, payload) {
      this.onEvent({
        kind,
        camera_id: camera.camera_id,
        camera_code: camera.code,
        ts: new Date().toISOString(),
        track_id: trackId,
        payload,
      });
    }
  }

  function randomBBox() {
    const x1 = round(Math.random() * 0.7);
    const y1 = round(0.3 + Math.random() * 0.4);
    return [x1, y1, round(x1 + 0.1 + Math.random() * 0.15), round(y1 + 0.08 + Math.random() * 0.1)];
  }
  function jitterBBox(b) {
    const dx = (Math.random() - 0.5) * 0.03;
    return [round(clamp01(b[0] + dx)), b[1], round(clamp01(b[2] + dx)), b[3]];
  }
  function clamp01(v) { return Math.max(0, Math.min(1, v)); }
  function round(v) { return Math.round(v * 1000) / 1000; }

  const PLATE_LETTERS = ["AB", "CD", "GH", "JK", "MN", "PQ", "XY"];
  function randomPlate() {
    const district = String(1 + Math.floor(Math.random() * 38)).padStart(2, "0");
    const letters = PLATE_LETTERS[Math.floor(Math.random() * PLATE_LETTERS.length)];
    const number = String(Math.floor(Math.random() * 10000)).padStart(4, "0");
    return `GJ${district}${letters}${number}`;
  }

  // ---------------------------------------------------------------------
  // Camera health — GET /api/camera-health (health.py). Demo fallback is
  // derived straight from DEMO_CAMERAS so the two stay honest about the same
  // grid; a handful of rows are deliberately left with no probe at all
  // (last_checked_at: null) rather than inventing a 100% uptime figure —
  // matching the real endpoint's stance in docs/DATA_HANDLING_AND_RETENTION.md.
  // ---------------------------------------------------------------------

  function demoCameraHealth() {
    return DEMO_CAMERAS.map((c, i) => {
      const neverProbed = i % 11 === 4; // a few honestly "unknown" rows
      if (neverProbed) {
        return {
          camera_id: c.camera_id, code: c.code, name: c.name, department_id: 0,
          jurisdiction_id: 0, jurisdiction_path: c.jurisdiction_path, camera_type: c.camera_type,
          status: c.status, last_checked_at: null, last_probe: null, last_is_live: null,
          last_latency_ms: null, last_error_code: null,
        };
      }
      const faulty = i % 13 === 6;
      const ageMinutes = 1 + (i % 6);
      return {
        camera_id: c.camera_id, code: c.code, name: c.name, department_id: 0,
        jurisdiction_id: 0, jurisdiction_path: c.jurisdiction_path, camera_type: c.camera_type,
        status: faulty ? "faulty" : c.status,
        last_checked_at: new Date(Date.now() - ageMinutes * 60000).toISOString(),
        last_probe: "rtsp_connect",
        last_is_live: !faulty,
        last_latency_ms: faulty ? null : 90 + (i * 17) % 260,
        last_error_code: faulty ? "connect_timeout" : null,
      };
    });
  }

  async function fetchCameraHealth() {
    if (getSession()) {
      try {
        const res = await authFetch("/api/camera-health", { headers: { Accept: "application/json" } });
        if (res.ok) { usingDemoData = false; return { rows: await res.json(), demo: false }; }
      } catch (e) { /* fall through to demo */ }
    }
    return { rows: demoCameraHealth(), demo: true };
  }

  // ---------------------------------------------------------------------
  // Incident reports — GET /api/reports/incidents (reports.py). This is
  // Sentinel's own alert history grouped by jurisdiction/week, never a claim
  // about official crime statistics — see reports.py's module docstring. The
  // demo fallback is synthetic counts over the same districts DEMO_CAMERAS
  // already covers, clearly flagged via the `demo` field so a caller can never
  // mistake it for the real aggregate.
  // ---------------------------------------------------------------------

  const ALERT_KINDS = [
    "watchlist_match_vehicle", "watchlist_match_person", "women_safety_risk",
    "abandoned_object", "crowd_surge", "speed_violation", "sos", "stream_gap_prolonged",
  ];
  const KIND_SEVERITY = {
    watchlist_match_vehicle: "urgent", watchlist_match_person: "urgent",
    women_safety_risk: "critical", abandoned_object: "advisory", crowd_surge: "urgent",
    speed_violation: "info", sos: "critical", stream_gap_prolonged: "advisory",
  };

  function demoIncidentReport(weeks) {
    const districts = Array.from(new Set(DEMO_CAMERAS.map((c) => c.jurisdiction_path))).sort();
    const rows = [];
    let seed = 17;
    function rand() { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; }
    const now = new Date();
    for (let w = 0; w < weeks; w++) {
      const weekStart = new Date(now);
      weekStart.setUTCDate(weekStart.getUTCDate() - weekStart.getUTCDay() - w * 7);
      weekStart.setUTCHours(0, 0, 0, 0);
      districts.forEach((district, di) => {
        const kindsThisWeek = 1 + Math.floor(rand() * 3);
        for (let k = 0; k < kindsThisWeek; k++) {
          const kind = ALERT_KINDS[Math.floor(rand() * ALERT_KINDS.length)];
          const count = 1 + Math.floor(rand() * (3 + (di % 3)));
          rows.push({
            jurisdiction_id: di + 1, jurisdiction_path: district, jurisdiction_name: district,
            week_start: weekStart.toISOString(), kind, severity: KIND_SEVERITY[kind] || "info", count,
          });
        }
      });
    }
    return rows;
  }

  async function fetchIncidentReports(weeks) {
    weeks = weeks || 8;
    if (getSession()) {
      try {
        const res = await authFetch("/api/reports/incidents?weeks=" + encodeURIComponent(weeks), {
          headers: { Accept: "application/json" },
        });
        if (res.ok) {
          usingDemoData = false;
          const data = await res.json();
          return { generated_at: data.generated_at, weeks: data.weeks, rows: data.rows, demo: false };
        }
      } catch (e) { /* fall through to demo */ }
    }
    return { generated_at: new Date().toISOString(), weeks, rows: demoIncidentReport(weeks), demo: true };
  }

  // ---------------------------------------------------------------------
  // Safety zones — GET/POST /api/safety-zones, DELETE /api/safety-zones/{id}
  // (safety_zones.py). Curated by a human, never algorithmically derived —
  // see db/migrations/011_safety_zone.sql. The demo fallback is a small,
  // explicitly-labelled illustrative set so the Safety Map page has something
  // to show before any department has curated real zones; it is NOT a claim
  // about where risk actually is.
  // ---------------------------------------------------------------------

  const DEMO_SAFETY_ZONES = [
    { id: -1, name: "Kalupur Railway Station approach (illustrative)", jurisdiction_id: 0, jurisdiction_path: "Ahmedabad",
      lat: 23.0280, lon: 72.6010, radius_m: 400, risk_level: "high", basis: "curated",
      note: "Demo only — replace with a real curated entry.", is_active: true,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    { id: -2, name: "Rajkot bus port surrounds (illustrative)", jurisdiction_id: 0, jurisdiction_path: "Rajkot",
      lat: 22.3020, lon: 70.7950, radius_m: 350, risk_level: "medium", basis: "curated",
      note: "Demo only — replace with a real curated entry.", is_active: true,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    { id: -3, name: "Junagadh old-city lanes (illustrative)", jurisdiction_id: 0, jurisdiction_path: "Junagadh",
      lat: 21.5170, lon: 70.4570, radius_m: 500, risk_level: "medium", basis: "curated",
      note: "Demo only — replace with a real curated entry.", is_active: true,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
  ];

  async function fetchSafetyZones() {
    if (getSession()) {
      try {
        const res = await authFetch("/api/safety-zones", { headers: { Accept: "application/json" } });
        if (res.ok) { usingDemoData = false; return { rows: await res.json(), demo: false }; }
      } catch (e) { /* fall through to demo */ }
    }
    return { rows: DEMO_SAFETY_ZONES, demo: true };
  }

  async function createSafetyZone(payload) {
    const res = await authFetch("/api/safety-zones", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || "could not create zone (HTTP " + res.status + ")");
    }
    return res.json();
  }

  async function deleteSafetyZone(zoneId) {
    const res = await authFetch("/api/safety-zones/" + encodeURIComponent(zoneId), { method: "DELETE" });
    if (!res.ok && res.status !== 204) throw new Error("HTTP " + res.status);
  }

  // ---------------------------------------------------------------------
  // PCRs (Police Control Room units / patrol response points) + the state
  // control room. DEMO DATA ONLY for now — there is no /api/pcrs route yet.
  // The point this demonstrates is the response *routing*: when a camera
  // recognises an incident, the location is relayed to the NEAREST PCR first
  // (fastest boots on the ground) and then escalated to the state control
  // room — not the other way round. If the project is selected, these become
  // a real, RLS-scoped table fed from the department's actual PCR roster, and
  // fetchPcrs() flips to LIVE exactly like fetchCameras() does. The
  // coordinates below are approximate district-level placements, deliberately
  // near the demo camera clusters so the routing is visible on the map.
  // ---------------------------------------------------------------------

  const DEMO_PCRS = [
    { id: "PCR-AHM-01", name: "Ahmedabad City PCR (Shahibaug)", district: "Ahmedabad", lat: 23.0430, lon: 72.6010, phone: "100" },
    { id: "PCR-AHM-02", name: "Ahmedabad West PCR (Paldi)", district: "Ahmedabad", lat: 23.0110, lon: 72.5580, phone: "100" },
    { id: "PCR-GNR-01", name: "Gandhinagar PCR (Sector 7)", district: "Gandhinagar", lat: 23.2230, lon: 72.6500, phone: "100" },
    { id: "PCR-RJK-01", name: "Rajkot City PCR", district: "Rajkot", lat: 22.3010, lon: 70.8020, phone: "100" },
    { id: "PCR-JND-01", name: "Junagadh PCR", district: "Junagadh", lat: 21.5170, lon: 70.4610, phone: "100" },
    { id: "PCR-GIR-01", name: "Gir Somnath PCR (Veraval)", district: "Gir Somnath", lat: 20.9070, lon: 70.3670, phone: "100" },
    { id: "PCR-NVS-01", name: "Navsari PCR", district: "Navsari", lat: 20.9500, lon: 72.9200, phone: "100" },
    { id: "PCR-PTN-01", name: "Patan PCR", district: "Patan", lat: 23.8490, lon: 72.1260, phone: "100" },
    { id: "PCR-BNK-01", name: "Banaskantha PCR (Palanpur)", district: "Banaskantha", lat: 24.1720, lon: 72.4380, phone: "100" },
    { id: "PCR-KUT-01", name: "Kutch PCR (Bhuj)", district: "Kutch", lat: 23.2530, lon: 69.6690, phone: "100" },
    { id: "PCR-ARV-01", name: "Aravalli PCR (Modasa)", district: "Aravalli", lat: 23.4620, lon: 73.2990, phone: "100" },
  ];

  // Hospitals (accident / medical response) and fire stations (fire response)
  // — the same demo-only status as the PCRs above. Incident type decides which
  // set an incident is routed to: crime/women's-safety → PCR, accident →
  // hospital, fire → fire station. All three become real rosters on selection.
  const DEMO_HOSPITALS = [
    { id: "HOSP-AHM-01", name: "Civil Hospital, Asarwa (Ahmedabad)", district: "Ahmedabad", lat: 23.0550, lon: 72.6060, phone: "108" },
    { id: "HOSP-AHM-02", name: "SVP Hospital, Ellisbridge (Ahmedabad)", district: "Ahmedabad", lat: 23.0280, lon: 72.5560, phone: "108" },
    { id: "HOSP-GNR-01", name: "Civil Hospital, Sector 12 (Gandhinagar)", district: "Gandhinagar", lat: 23.2230, lon: 72.6490, phone: "108" },
    { id: "HOSP-RJK-01", name: "Rajkot Civil Hospital", district: "Rajkot", lat: 22.2930, lon: 70.7930, phone: "108" },
    { id: "HOSP-JND-01", name: "Junagadh Civil Hospital", district: "Junagadh", lat: 21.5200, lon: 70.4570, phone: "108" },
    { id: "HOSP-GIR-01", name: "Gir Somnath Hospital (Veraval)", district: "Gir Somnath", lat: 20.9060, lon: 70.3650, phone: "108" },
    { id: "HOSP-NVS-01", name: "Navsari Civil Hospital", district: "Navsari", lat: 20.9510, lon: 72.9250, phone: "108" },
    { id: "HOSP-PTN-01", name: "Patan Civil Hospital", district: "Patan", lat: 23.8450, lon: 72.1300, phone: "108" },
    { id: "HOSP-BNK-01", name: "Palanpur Civil Hospital (Banaskantha)", district: "Banaskantha", lat: 24.1720, lon: 72.4350, phone: "108" },
    { id: "HOSP-KUT-01", name: "G.K. General Hospital, Bhuj (Kutch)", district: "Kutch", lat: 23.2420, lon: 69.6670, phone: "108" },
    { id: "HOSP-ARV-01", name: "Modasa Hospital (Aravalli)", district: "Aravalli", lat: 23.4620, lon: 73.2980, phone: "108" },
  ];

  const DEMO_FIRE_STATIONS = [
    { id: "FIRE-AHM-01", name: "Danapith Fire Station (Ahmedabad)", district: "Ahmedabad", lat: 23.0240, lon: 72.5880, phone: "101" },
    { id: "FIRE-AHM-02", name: "Naranpura Fire Station (Ahmedabad)", district: "Ahmedabad", lat: 23.0550, lon: 72.5600, phone: "101" },
    { id: "FIRE-GNR-01", name: "Gandhinagar Fire Station", district: "Gandhinagar", lat: 23.2260, lon: 72.6470, phone: "101" },
    { id: "FIRE-RJK-01", name: "Rajkot Fire Station", district: "Rajkot", lat: 22.3000, lon: 70.7900, phone: "101" },
    { id: "FIRE-JND-01", name: "Junagadh Fire Station", district: "Junagadh", lat: 21.5180, lon: 70.4580, phone: "101" },
    { id: "FIRE-GIR-01", name: "Veraval Fire Station (Gir Somnath)", district: "Gir Somnath", lat: 20.9050, lon: 70.3680, phone: "101" },
    { id: "FIRE-NVS-01", name: "Navsari Fire Station", district: "Navsari", lat: 20.9480, lon: 72.9220, phone: "101" },
    { id: "FIRE-PTN-01", name: "Patan Fire Station", district: "Patan", lat: 23.8470, lon: 72.1280, phone: "101" },
    { id: "FIRE-BNK-01", name: "Palanpur Fire Station (Banaskantha)", district: "Banaskantha", lat: 24.1700, lon: 72.4400, phone: "101" },
    { id: "FIRE-KUT-01", name: "Bhuj Fire Station (Kutch)", district: "Kutch", lat: 23.2480, lon: 69.6700, phone: "101" },
    { id: "FIRE-ARV-01", name: "Modasa Fire Station (Aravalli)", district: "Aravalli", lat: 23.4600, lon: 73.3000, phone: "101" },
  ];

  const CONTROL_ROOM = {
    id: "GSCR", name: "Gujarat State Police Control Room", city: "Gandhinagar",
    lat: 23.2156, lon: 72.6369, phone: "100 / 112",
  };

  async function fetchPcrs() {
    // Future: authFetch("/api/responders") when a session exists. Demo for now.
    return {
      pcrs: DEMO_PCRS, hospitals: DEMO_HOSPITALS, fireStations: DEMO_FIRE_STATIONS,
      controlRoom: CONTROL_ROOM, demo: true,
    };
  }

  // ---------------------------------------------------------------------

  window.SentinelAPI = {
    login,
    logout,
    getSession,
    fetchCameras,
    fetchAlerts,
    acknowledgeAlert,
    closeAlert,
    reportSos,
    searchWatchlist,
    fetchTrail,
    connectEvents,
    fetchCameraHealth,
    fetchIncidentReports,
    fetchSafetyZones,
    createSafetyZone,
    deleteSafetyZone,
    fetchPcrs,
    get usingDemoData() { return usingDemoData; },
  };
})();
