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
  // Demo camera fixture
  // ---------------------------------------------------------------------
  // Shaped after app.camera in db/migrations/002_camera.sql: code, name,
  // camera_type, status, location, bearing/fov/range wedge, department and
  // jurisdiction. Coordinates are real Ahmedabad locations; the cameras
  // themselves are fictional.

  const DEMO_CAMERAS = [
    cam(1, "AHM-NAV-0142", "Navrangpura Cross, Approach Rd", "anpr", "active",
      23.0339, 72.5622, 40, 90, 120, "Ahmedabad Traffic Police", "GJ.AHM.NAVRANGPURA", false),
    cam(2, "AHM-NAV-0143", "Navrangpura Cross, Exit Rd", "anpr", "active",
      23.0341, 72.5628, 220, 90, 120, "Ahmedabad Traffic Police", "GJ.AHM.NAVRANGPURA", false),
    cam(3, "AHM-ELS-0021", "Ellis Bridge, River Front", "ptz", "active",
      23.0258, 72.5714, 0, 360, 80, "Ahmedabad Municipal Corporation", "GJ.AHM.KHANPUR", true),
    cam(4, "AHM-CGR-0087", "CG Road, Panchvati Junction", "fixed", "faulty",
      23.0195, 72.5561, 300, 100, 90, "Ahmedabad Traffic Police", "GJ.AHM.NAVRANGPURA", false),
    cam(5, "AHM-MAN-0009", "Manek Chowk, East Gate", "dome", "active",
      23.0246, 72.5891, 150, 360, 60, "Ahmedabad Municipal Corporation", "GJ.AHM.KALUPUR", false),
    cam(6, "AHM-SGH-0055", "SG Highway, Iskcon Overbridge", "anpr", "active",
      23.0284, 72.5069, 45, 80, 150, "Ahmedabad Traffic Police", "GJ.AHM.BODAKDEV", false),
    cam(7, "AHM-SGH-0056", "SG Highway, Iskcon Underpass", "bullet", "maintenance",
      23.0281, 72.5075, 225, 80, 150, "Ahmedabad Traffic Police", "GJ.AHM.BODAKDEV", false),
    cam(8, "AHM-VAS-0033", "Vastrapur Lake, North Path", "fixed", "active",
      23.0368, 72.5290, 90, 120, 70, "Ahmedabad Municipal Corporation", "GJ.AHM.VASTRAPUR", true),
    cam(9, "AHM-RLY-0004", "Kalupur Railway Station, Gate 2", "thermal", "inactive",
      23.0272, 72.6013, 0, 360, 50, "Western Railway", "GJ.AHM.KALUPUR", false),
    cam(10, "AHM-JUH-0018", "Juhapura Ring Road", "fixed", "active",
      22.9878, 72.5432, 10, 100, 100, "Ahmedabad Traffic Police", "GJ.AHM.JUHAPURA", false),
  ];

  function cam(id, code, name, camera_type, status, lat, lon, bearing_deg, fov_deg, range_m, department, jurisdiction_path, isolated) {
    return {
      camera_id: id, code, name, camera_type, status,
      location: { lat, lon },
      bearing_deg, fov_deg, range_m,
      department, jurisdiction_path, isolated,
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
