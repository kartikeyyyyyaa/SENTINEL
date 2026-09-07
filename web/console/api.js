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
    get usingDemoData() { return usingDemoData; },
  };
})();
