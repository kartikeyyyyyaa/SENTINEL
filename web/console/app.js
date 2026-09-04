/* app.js — UI wiring: map, camera list, live feed, alerts, watchlist, auth.
 *
 * All data comes from window.SentinelAPI (api.js). This file knows nothing about
 * whether that data is real or demo — it just renders whatever arrives, and
 * shows the DEMO/LIVE badge that api.js reports.
 *
 * No inline event handlers anywhere (the console's CSP is script-src 'self'
 * with no 'unsafe-inline', so an onclick="" attribute would silently do
 * nothing) — everything is wired with addEventListener.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // A flat-earth projection for a small area, so the map works with no
  // basemap tiles at all (the console's CSP is img-src 'self', which rules
  // out fetching tiles from any real map provider — see web/nginx.conf).
  // Leaflet's CRS.Simple treats coordinates as a plain (y, x) plane, so this
  // just needs *a* consistent, roughly-metric mapping from lat/lon to it.
  // ---------------------------------------------------------------------

  const REF_LAT = 23.02;
  const REF_LON = 72.55;
  const UNITS_PER_DEGREE = 100000;
  const METRES_PER_DEGREE_LAT = 111320;
  const UNITS_PER_METRE = UNITS_PER_DEGREE / METRES_PER_DEGREE_LAT;
  const LON_SCALE = Math.cos((REF_LAT * Math.PI) / 180);

  function toPlane(lat, lon) {
    const y = (lat - REF_LAT) * UNITS_PER_DEGREE;
    const x = (lon - REF_LON) * UNITS_PER_DEGREE * LON_SCALE;
    return L.latLng(y, x);
  }

  function sectorPolygon(centre, bearingDeg, fovDeg, rangeM) {
    if (bearingDeg == null || fovDeg == null || !rangeM) return null;
    const r = rangeM * UNITS_PER_METRE;
    const points = [centre];
    const start = bearingDeg - fovDeg / 2;
    const steps = Math.max(2, Math.round(fovDeg / 8));
    for (let i = 0; i <= steps; i++) {
      const angle = ((start + (fovDeg * i) / steps) * Math.PI) / 180;
      points.push(L.latLng(centre.lat + r * Math.cos(angle), centre.lng + r * Math.sin(angle)));
    }
    points.push(centre);
    return points;
  }

  // ---------------------------------------------------------------------
  // Status / severity → colour
  // ---------------------------------------------------------------------

  const STATUS_COLOR = {
    active: "#2fe0c4",
    faulty: "#ff5470",
    maintenance: "#ffb443",
    inactive: "#4a5568",
    decommissioned: "#4a5568",
    planned: "#4a5568",
  };
  function statusColor(status) { return STATUS_COLOR[status] || STATUS_COLOR.inactive; }

  // Demo/synthetic primitive events (no registry Alert behind them) are
  // classified locally into the same three-tier feel the console has always
  // had. Real alerts carry their own severity (services/common/alerts.py's
  // SEVERITIES) and skip this entirely — see renderAlertItem.
  const DEMO_ALERT_KINDS = new Set(["dwell", "abandoned", "proximity", "stream_gap"]);
  const DEMO_INFO_KINDS = new Set(["anpr", "scene_change"]);
  function demoSeverityClass(kind) {
    if (DEMO_ALERT_KINDS.has(kind)) return "sev-urgent";
    if (DEMO_INFO_KINDS.has(kind)) return "sev-info";
    return "sev-routine";
  }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------

  let cameras = [];
  let markers = new Map(); // camera_id -> {marker, wedge}
  let selectedCameraId = null;
  let statusFilter = "all";
  let searchTerm = "";
  const liveTracks = new Set();
  const alertsById = new Map(); // real alerts, keyed by id — lets SSE updates replace a card in place
  let openAlertCount = 0;
  let safetyMode = false;

  let map;

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", async () => {
    // Each wiring step is independent of the others by design: a login form
    // that can't be used because the map failed to load is a worse failure
    // than a missing map, so no single step here is allowed to take the rest
    // of the console down with it. safeStep() logs and moves on instead of
    // letting one uncaught exception abort every listener queued after it —
    // which is exactly what happened during development when vendor/leaflet
    // was briefly absent from a checkout: initMap() threw, and nothing below
    // it — including the login form — ever got wired.
    safeStep("map", initMap);
    safeStep("clock", initClock);
    safeStep("sidebar controls", wireSidebarControls);
    safeStep("tabs", wireTabs);
    safeStep("drawer", wireDrawer);
    safeStep("auth UI", wireAuthUI);
    safeStep("safety mode", wireSafetyMode);
    safeStep("SOS", wireSos);
    safeStep("watchlist", wireWatchlist);

    await boot();
  });

  function safeStep(label, fn) {
    try {
      fn();
    } catch (err) {
      console.error("console init step failed:", label, err);
    }
  }

  async function boot() {
    const session = window.SentinelAPI.getSession();
    showLogin(!session);
    updateUserChip(session ? session.user : null);

    cameras = await window.SentinelAPI.fetchCameras();
    renderCameraList();
    plotCameras();
    updateDataModeBadge();
    updateStats();

    if (session) await loadInitialAlerts();

    window.SentinelAPI.connectEvents(cameras, handleMessage);
    setTimeout(updateDataModeBadge, 2700);
  }

  function initMap() {
    map = L.map("map", {
      crs: L.CRS.Simple,
      zoomControl: true,
      attributionControl: false,
      minZoom: -3,
      maxZoom: 6,
    });
    map.setView([0, 0], 2);
  }

  function initClock() {
    const el = document.getElementById("clock");
    const tick = () => { el.textContent = new Date().toLocaleTimeString("en-GB"); };
    tick();
    setInterval(tick, 1000);
  }

  function updateDataModeBadge() {
    const badge = document.getElementById("data-mode");
    if (window.SentinelAPI.usingDemoData) {
      badge.innerHTML = '<span class="badge-dot"></span>DEMO DATA';
      badge.className = "badge badge-demo";
      badge.title = "No live session or registry endpoint reachable — showing bundled demo cameras and a synthetic event feed shaped like the real one.";
    } else {
      badge.innerHTML = '<span class="badge-dot"></span>LIVE';
      badge.className = "badge badge-live";
      badge.title = "Connected to the registry API.";
    }
  }

  // ---------------------------------------------------------------------
  // Auth: login overlay, user chip, sign out
  // ---------------------------------------------------------------------

  function wireAuthUI() {
    const form = document.getElementById("login-form");
    const errorEl = document.getElementById("login-error");
    const submitBtn = document.getElementById("login-submit");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      errorEl.textContent = "";
      submitBtn.disabled = true;
      submitBtn.textContent = "Signing in…";
      try {
        const user = await window.SentinelAPI.login(
          document.getElementById("login-username").value.trim(),
          document.getElementById("login-password").value
        );
        updateUserChip(user);
        showLogin(false);
        await loadInitialAlerts();
        cameras = await window.SentinelAPI.fetchCameras();
        renderCameraList();
        plotCameras();
        updateDataModeBadge();
        updateStats();
        window.SentinelAPI.connectEvents(cameras, handleMessage);
      } catch (err) {
        errorEl.textContent = err.message || "sign-in failed";
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Sign in";
      }
    });

    document.getElementById("login-demo").addEventListener("click", () => showLogin(false));

    document.getElementById("user-chip").addEventListener("click", async () => {
      if (!confirm("Sign out of the console?")) return;
      await window.SentinelAPI.logout();
      updateUserChip(null);
      location.reload();
    });
  }

  function showLogin(visible) {
    document.getElementById("login-overlay").hidden = !visible;
  }

  function updateUserChip(user) {
    const chip = document.getElementById("user-chip");
    if (!user) { chip.hidden = true; return; }
    chip.hidden = false;
    document.getElementById("user-name").textContent = user.username;
    document.getElementById("user-avatar").textContent = user.username.slice(0, 2).toUpperCase();
    chip.title = (user.is_statewide ? "Statewide" : user.jurisdiction_path || "") + " · " + user.permissions.length + " permissions — click to sign out";
  }

  // ---------------------------------------------------------------------
  // Women's Safety Mode
  // ---------------------------------------------------------------------

  function wireSafetyMode() {
    document.getElementById("safety-toggle").addEventListener("click", () => {
      safetyMode = !safetyMode;
      document.getElementById("safety-toggle").classList.toggle("on", safetyMode);
      document.body.classList.toggle("safety-mode", safetyMode);
      plotCameras();
      if (safetyMode) {
        toast("Women’s Safety Mode: isolated zones highlighted, SOS + safety alerts prioritised.");
        document.querySelector('.tab[data-tab="alerts"]').click();
      }
    });
  }

  function toast(message) {
    const el = document.getElementById("map-toast");
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 4200);
  }

  // ---------------------------------------------------------------------
  // SOS
  // ---------------------------------------------------------------------

  function wireSos() {
    const modal = document.getElementById("sos-modal");
    document.getElementById("sos-btn").addEventListener("click", () => { modal.hidden = false; });
    document.getElementById("sos-cancel").addEventListener("click", () => { modal.hidden = true; });
    document.getElementById("sos-confirm").addEventListener("click", async () => {
      const btn = document.getElementById("sos-confirm");
      btn.disabled = true;
      btn.textContent = "Sending…";
      try {
        const camera = cameras.find((c) => c.camera_id === selectedCameraId) || cameras[0];
        await window.SentinelAPI.reportSos({
          channel: "operator",
          camera_id: camera ? camera.camera_id : null,
          lat: camera ? camera.location.lat : null,
          lon: camera ? camera.location.lon : null,
          notes: "Raised from the operator console (demo).",
        });
        toast("SOS sent — a critical alert has been opened.");
        modal.hidden = true;
        await loadInitialAlerts();
      } catch (err) {
        toast("SOS failed to send: " + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = "Send SOS";
      }
    });
  }

  // ---------------------------------------------------------------------
  // Camera list + filters
  // ---------------------------------------------------------------------

  function wireSidebarControls() {
    document.getElementById("camera-search").addEventListener("input", (e) => {
      searchTerm = e.target.value.trim().toLowerCase();
      renderCameraList();
    });

    const chips = document.querySelectorAll("#status-filters .chip");
    chips.forEach((chip) => {
      chip.addEventListener("click", () => {
        chips.forEach((c) => c.classList.remove("chip-active"));
        chip.classList.add("chip-active");
        statusFilter = chip.dataset.status;
        renderCameraList();
        plotCameras();
      });
    });
  }

  function visibleCameras() {
    return cameras.filter((c) => {
      if (statusFilter !== "all" && c.status !== statusFilter) return false;
      if (!searchTerm) return true;
      const hay = (c.code + " " + c.name + " " + (c.jurisdiction_path || "")).toLowerCase();
      return hay.includes(searchTerm);
    });
  }

  function renderCameraList() {
    const list = document.getElementById("camera-list");
    const rows = visibleCameras();
    list.innerHTML = "";
    if (!rows.length) {
      list.innerHTML = '<li class="empty-note">No cameras match.</li>';
      return;
    }
    for (const c of rows) {
      const li = document.createElement("li");
      li.className = "camera-row" + (c.camera_id === selectedCameraId ? " selected" : "");
      li.innerHTML =
        '<span class="dot" style="background:' + statusColor(c.status) + '"></span>' +
        '<span class="meta"><span class="code"></span><span class="name"></span></span>' +
        (c.isolated ? '<span class="isolated-flag" title="Isolated zone">&#9888;</span>' : "");
      li.querySelector(".code").textContent = c.code;
      li.querySelector(".name").textContent = c.name;
      li.addEventListener("click", () => selectCamera(c.camera_id, true));
      list.appendChild(li);
    }
  }

  // ---------------------------------------------------------------------
  // Map markers
  // ---------------------------------------------------------------------

  function plotCameras() {
    if (!map) return; // Leaflet failed to load — see safeStep() in the boot handler.
    for (const { marker, wedge } of markers.values()) {
      map.removeLayer(marker);
      if (wedge) map.removeLayer(wedge);
    }
    markers.clear();

    const rows = visibleCameras();
    const bounds = [];
    for (const c of rows) {
      const pos = toPlane(c.location.lat, c.location.lon);
      bounds.push(pos);

      const highlightIsolated = safetyMode && c.isolated;
      const ringColor = highlightIsolated ? "#ff5da2" : statusColor(c.status);

      let wedge = null;
      const sector = sectorPolygon(pos, c.bearing_deg, c.fov_deg, c.range_m);
      if (sector) {
        wedge = L.polygon(sector, {
          color: ringColor,
          weight: 1,
          fillColor: ringColor,
          fillOpacity: highlightIsolated ? 0.14 : 0.08,
          opacity: highlightIsolated ? 0.6 : 0.35,
          interactive: false,
        }).addTo(map);
      }

      const marker = L.circleMarker(pos, {
        radius: highlightIsolated ? 9 : 7,
        color: highlightIsolated ? "#ff5da2" : "#070a10",
        weight: highlightIsolated ? 2.5 : 1.5,
        fillColor: statusColor(c.status),
        fillOpacity: 0.95,
      }).addTo(map);
      marker.bindPopup(popupHtml(c));
      marker.on("click", () => selectCamera(c.camera_id, false));

      markers.set(c.camera_id, { marker, wedge });
    }

    if (bounds.length) {
      map.fitBounds(L.latLngBounds(bounds).pad(0.25));
    }
  }

  function popupHtml(c) {
    return (
      '<div><b>' + escapeHtml(c.name) + '</b><br>' +
      '<span class="popup-code">' + escapeHtml(c.code) + "</span>" +
      '<div class="popup-row">' + escapeHtml(c.camera_type) + " &middot; " + escapeHtml(c.status) + "</div>" +
      '<div class="popup-row">' + escapeHtml(c.department || "") + "</div>" +
      (c.isolated ? '<div class="popup-row" style="color:#ff5da2">&#9888; isolated zone</div>' : "") +
      "</div>"
    );
  }

  function selectCamera(cameraId, panMap) {
    selectedCameraId = cameraId;
    renderCameraList();
    const camera = cameras.find((c) => c.camera_id === cameraId);
    if (!camera) return;
    if (panMap) {
      const entry = markers.get(cameraId);
      if (entry) { map.panTo(entry.marker.getLatLng()); entry.marker.openPopup(); }
    }
    openDrawer(camera);
  }

  // ---------------------------------------------------------------------
  // Camera detail drawer
  // ---------------------------------------------------------------------

  function wireDrawer() {
    document.getElementById("cd-close").addEventListener("click", closeDrawer);
  }
  function openDrawer(c) {
    document.getElementById("cd-title").textContent = c.code;
    const body = document.getElementById("cd-body");
    body.innerHTML = "";
    const fields = [
      ["Name", c.name],
      ["Type", c.camera_type],
      ["Status", c.status],
      ["Department", c.department || "—"],
      ["Jurisdiction", c.jurisdiction_path || "—"],
      ["Location", c.location.lat.toFixed(5) + ", " + c.location.lon.toFixed(5)],
      ["Coverage", c.bearing_deg != null ? c.bearing_deg + "° bearing, " + c.fov_deg + "° FOV, " + c.range_m + "m range" : "—"],
      ["Zone", c.isolated ? "Isolated — flagged for women's-safety analytics" : "Standard"],
    ];
    for (const [label, value] of fields) {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      body.appendChild(dt); body.appendChild(dd);
    }
    document.getElementById("camera-detail").hidden = false;
  }
  function closeDrawer() { document.getElementById("camera-detail").hidden = true; }

  // ---------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------

  function wireTabs() {
    const tabs = document.querySelectorAll("#panel-tabs .tab");
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => t.classList.remove("tab-active"));
        tab.classList.add("tab-active");
        document.querySelectorAll(".tab-body").forEach((b) => b.classList.remove("tab-body-active"));
        document.getElementById("tab-" + tab.dataset.tab).classList.add("tab-body-active");
      });
    });
  }

  // ---------------------------------------------------------------------
  // Live event feed + alerts
  // ---------------------------------------------------------------------

  const MAX_FEED_ITEMS = 80;

  /** Every SSE / synthetic message arrives here as {type, event|alert}. */
  function handleMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "event") return handleEvent(msg.event);
    if (msg.type === "alert.created" || msg.type === "alert.updated") return handleAlert(msg.alert);
  }

  function handleEvent(evt) {
    if (evt.kind === "track_start" && evt.track_id) liveTracks.add(evt.track_id);
    if (evt.kind === "track_end" && evt.track_id) liveTracks.delete(evt.track_id);
    appendEventItem(evt);
    updateStats();
  }

  function appendEventItem(evt) {
    const list = document.getElementById("events-list");
    const empty = list.querySelector(".empty-note");
    if (empty) empty.remove();

    const camera = cameras.find((c) => c.camera_id === evt.camera_id);
    const li = document.createElement("li");
    li.className = "feed-item " + demoSeverityClass(evt.kind);
    const time = new Date(evt.ts);
    li.innerHTML =
      '<div class="feed-head"><span class="feed-kind"></span><span class="feed-time"></span></div>' +
      '<div class="feed-cam"></div><div class="feed-detail"></div>';
    li.querySelector(".feed-kind").textContent = evt.kind;
    li.querySelector(".feed-time").textContent = time.toLocaleTimeString("en-GB");
    li.querySelector(".feed-cam").textContent =
      (camera ? camera.code : "camera " + evt.camera_id) + (evt.track_id ? " · " + evt.track_id : "");
    li.querySelector(".feed-detail").textContent = summarisePayload(evt);

    list.insertBefore(li, list.firstChild);
    while (list.children.length > MAX_FEED_ITEMS) list.removeChild(list.lastChild);
  }

  function summarisePayload(evt) {
    const p = evt.payload || evt.detail || {};
    switch (evt.kind) {
      case "anpr":
      case "plate_read":
        return "plate " + (p.plate_text || "?") + " (conf " + fmtConf(p.confidence) + (p.format_valid === false ? ", format invalid" : "") + ")";
      case "track_start":
      case "track_update":
      case "track_end":
        return (p.class_label || "") + (p.confidence != null ? " conf " + fmtConf(p.confidence) : "") + (p.duration_seconds != null ? " · " + p.duration_seconds + "s" : "");
      case "dwell":
        return (p.class_label || "") + " dwelling " + (p.dwell_seconds || "?") + "s in " + (p.zone_id || "zone");
      case "proximity":
        return "sustained " + (p.seconds || "?") + "s at close range";
      case "stream_gap":
        return "camera unreachable for " + (p.gap_seconds || "?") + "s (" + (p.reason || "unknown") + ")";
      case "scene_change":
        return "scene discontinuity (" + (p.reason || "unknown") + ")";
      default:
        try { return JSON.stringify(p); } catch (e) { return ""; }
    }
  }
  function fmtConf(v) { return typeof v === "number" ? v.toFixed(2) : "?"; }

  // ---------------------------------------------------------------------
  // Real alerts (services/common/alerts.py's kind/severity/status taxonomy)
  // ---------------------------------------------------------------------

  async function loadInitialAlerts() {
    const alerts = await window.SentinelAPI.fetchAlerts();
    if (!alerts) return;
    alertsById.clear();
    for (const a of alerts) alertsById.set(a.id, a);
    renderAlertsList();
  }

  function handleAlert(alert) {
    if (!alert || alert.id == null) return;
    alertsById.set(alert.id, alert);
    renderAlertsList();
    if (alert.kind === "sos" || alert.kind === "women_safety_risk") {
      toast((alert.kind === "sos" ? "SOS: " : "Women’s safety alert: ") + alert.summary);
      document.querySelector('.tab[data-tab="alerts"]').click();
    }
  }

  const SEV_RANK = { critical: 0, urgent: 1, advisory: 2, info: 3 };

  function renderAlertsList() {
    const list = document.getElementById("alerts-list");
    list.innerHTML = "";
    let open = Array.from(alertsById.values()).filter((a) => a.status === "open" || a.status === "acknowledged");

    if (safetyMode) {
      // Surface SOS / women's-safety signals first without hiding anything
      // else — a control room can't afford a mode that makes other live
      // alerts disappear.
      open.sort((a, b) => {
        const aw = a.kind === "sos" || a.kind === "women_safety_risk" ? 0 : 1;
        const bw = b.kind === "sos" || b.kind === "women_safety_risk" ? 0 : 1;
        if (aw !== bw) return aw - bw;
        return (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9);
      });
    } else {
      open.sort((a, b) => (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9) || new Date(b.opened_at) - new Date(a.opened_at));
    }

    openAlertCount = open.length;
    document.getElementById("alerts-count").textContent = String(openAlertCount);
    updateStats();

    if (!open.length) {
      list.innerHTML = window.SentinelAPI.getSession()
        ? '<li class="empty-note">No open alerts.</li>'
        : '<li class="empty-note">Sign in to see live alerts — the Live Events tab still shows the demo feed.</li>';
      return;
    }
    for (const alert of open) list.appendChild(renderAlertItem(alert));
  }

  function renderAlertItem(alert) {
    const camera = cameras.find((c) => c.camera_id === alert.camera_id);
    const li = document.createElement("li");
    li.className = "feed-item sev-" + alert.severity + (alert.kind === "sos" ? " kind-sos" : "");
    li.innerHTML =
      '<div class="feed-head"><span class="feed-kind"></span><span class="status-pill st-' + alert.status + '"></span></div>' +
      '<div class="feed-cam"></div><div class="feed-detail"></div><div class="feed-actions"></div>';
    li.querySelector(".feed-kind").textContent = alert.kind.replace(/_/g, " ");
    li.querySelector(".status-pill").textContent = alert.status.replace(/_/g, " ");
    li.querySelector(".feed-cam").textContent =
      (camera ? camera.code : alert.camera_id ? "camera " + alert.camera_id : "no camera") +
      (alert.case_reference ? " · " + alert.case_reference : "");
    li.querySelector(".feed-detail").textContent = alert.summary;

    const actions = li.querySelector(".feed-actions");
    if (alert.status === "open") {
      const ackBtn = document.createElement("button");
      ackBtn.className = "btn-mini"; ackBtn.textContent = "Acknowledge";
      ackBtn.addEventListener("click", () => runAlertAction(ackBtn, () => window.SentinelAPI.acknowledgeAlert(alert.id)));
      actions.appendChild(ackBtn);
    }
    if (alert.status === "open" || alert.status === "acknowledged") {
      const closeBtn = document.createElement("button");
      closeBtn.className = "btn-mini danger"; closeBtn.textContent = "Close";
      closeBtn.addEventListener("click", () => runAlertAction(closeBtn, () => window.SentinelAPI.closeAlert(alert.id)));
      actions.appendChild(closeBtn);
    }
    if (!actions.children.length) actions.remove();
    return li;
  }

  async function runAlertAction(btn, action) {
    btn.disabled = true;
    try {
      const updated = await action();
      alertsById.set(updated.id, updated);
      renderAlertsList();
    } catch (err) {
      toast("Action failed: " + err.message);
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------------
  // Watchlist — the searchable stolen-vehicle / wanted / missing / suspect
  // database (Step 3), plus the "criminal mapping" trail per entry.
  // ---------------------------------------------------------------------

  function wireWatchlist() {
    const input = document.getElementById("wl-search-input");
    const run = () => runWatchlistSearch(input.value.trim());
    document.getElementById("wl-search-btn").addEventListener("click", run);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  }

  async function runWatchlistSearch(query) {
    const list = document.getElementById("wl-list");
    if (!window.SentinelAPI.getSession()) {
      list.innerHTML = '<li class="empty-note">Sign in to search the watchlist.</li>';
      return;
    }
    list.innerHTML = '<li class="empty-note">Searching…</li>';
    const entries = await window.SentinelAPI.searchWatchlist(query);
    list.innerHTML = "";
    if (!entries || !entries.length) {
      list.innerHTML = '<li class="empty-note">No matching watchlist entries.</li>';
      return;
    }
    for (const entry of entries) list.appendChild(renderWatchlistItem(entry));
  }

  function renderWatchlistItem(entry) {
    const li = document.createElement("li");
    li.className = "wl-item";
    li.innerHTML =
      '<div class="wl-head"><span></span><span class="wl-risk risk-' + entry.risk_level + '"></span></div>' +
      '<div class="wl-label"></div><div class="wl-case"></div><span class="wl-trail-link">View sighting trail →</span>';
    li.querySelector(".wl-head span").outerHTML = entry.plate_number
      ? '<span class="wl-plate">' + escapeHtml(entry.plate_number) + "</span>"
      : "<span>" + escapeHtml(entry.entry_type.replace(/_/g, " ")) + "</span>";
    li.querySelector(".wl-risk").textContent = entry.risk_level;
    li.querySelector(".wl-label").textContent = entry.label;
    li.querySelector(".wl-case").textContent = entry.case_reference ? "Ref: " + entry.case_reference : entry.status;

    li.querySelector(".wl-trail-link").addEventListener("click", async (e) => {
      const el = e.target;
      el.textContent = "Loading trail…";
      try {
        const trail = await window.SentinelAPI.fetchTrail(entry.id);
        renderTrailBelow(li, trail);
        el.remove();
      } catch (err) {
        el.textContent = "Trail unavailable";
      }
    });
    return li;
  }

  function renderTrailBelow(li, trail) {
    const box = document.createElement("div");
    box.style.marginTop = "8px";
    box.style.paddingTop = "8px";
    box.style.borderTop = "1px solid var(--line-solid)";
    if (!trail.length) {
      box.innerHTML = '<span style="color:var(--text-2)">No sightings recorded yet.</span>';
    } else {
      box.innerHTML = trail
        .map((m) => {
          const cam = cameras.find((c) => c.camera_id === m.camera_id);
          return (
            '<div style="margin-bottom:4px;color:var(--text-1)">' +
            new Date(m.matched_at).toLocaleString("en-GB") +
            " · " + (cam ? escapeHtml(cam.code) : "camera " + m.camera_id) +
            " · " + Math.round((m.confidence || 0) * 100) + "% " + m.basis +
            "</div>"
          );
        })
        .join("");
    }
    li.appendChild(box);
  }

  // ---------------------------------------------------------------------
  // Stats
  // ---------------------------------------------------------------------

  function updateStats() {
    document.getElementById("stat-cameras").textContent = String(cameras.length);
    document.getElementById("stat-online").textContent = String(cameras.filter((c) => c.status === "active").length);
    document.getElementById("stat-tracks").textContent = String(liveTracks.size);
    const alertsEl = document.getElementById("stat-alerts");
    alertsEl.textContent = String(openAlertCount);
    alertsEl.classList.toggle("stat-hot", openAlertCount > 0);
  }

  // ---------------------------------------------------------------------

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
})();
