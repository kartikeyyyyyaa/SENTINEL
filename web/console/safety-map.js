/* safety-map.js — the Women's Safety Map dashboard.
 *
 * Reads GET /api/safety-zones through window.SentinelAPI.fetchSafetyZones()
 * (api.js), which falls back to a small, explicitly-labelled illustrative
 * set when signed out or the API is unreachable — never a claim about real
 * risk. Signed-in users with safety_zone.write (state/district admins) can
 * add or remove zones; anyone else sees the map and list read-only.
 *
 * Map tiles: same Esri "World Imagery" satellite basemap as the main
 * console (app.js), for the same reason — free, keyless, HTTPS-only, and
 * already the one extra host nginx's CSP img-src allows.
 */
(function () {
  "use strict";

  const SAT = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
  const SAT_ATTR = "Imagery &copy; Esri, Maxar, Earthstar Geographics";
  const LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
  const CENTER = [22.6, 71.6], ZOOM = 7;

  const RISK_COLOR = { low: "#9db0ce", medium: "#f59e0b", high: "#fb923c", critical: "#ef4444" };

  let map, circles = [];
  let zones = [];

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function initMap() {
    map = L.map("safety-map", { zoomControl: true, minZoom: 5, maxZoom: 18 }).setView(CENTER, ZOOM);
    L.tileLayer(SAT, { attribution: SAT_ATTR, maxZoom: 18 }).addTo(map);
    L.tileLayer(LABELS, { maxZoom: 18, opacity: 0.9 }).addTo(map);
    map.on("click", (e) => {
      const latIn = document.getElementById("zf-lat");
      const lonIn = document.getElementById("zf-lon");
      if (latIn && lonIn && !document.getElementById("add-zone-panel").hidden) {
        latIn.value = e.latlng.lat.toFixed(5);
        lonIn.value = e.latlng.lng.toFixed(5);
      }
    });
  }

  function drawZones() {
    circles.forEach((c) => map.removeLayer(c));
    circles = zones.map((z) => {
      const circle = L.circle([z.lat, z.lon], {
        radius: z.radius_m,
        color: RISK_COLOR[z.risk_level] || RISK_COLOR.medium,
        weight: 2,
        fillOpacity: 0.18,
      }).addTo(map);
      circle.bindTooltip(`${escapeHtml(z.name)} &mdash; ${escapeHtml(z.risk_level)}`, { sticky: true });
      return circle;
    });
  }

  function renderList() {
    const list = document.getElementById("zone-list");
    const empty = document.getElementById("zone-empty");
    if (!zones.length) {
      list.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    const canWrite = !!(window.SentinelAPI.getSession());
    list.innerHTML = zones
      .map(
        (z) => `
      <li class="zone-item" data-id="${z.id}">
        <div class="zone-item-head">
          <span class="zone-item-name">${escapeHtml(z.name)}</span>
          <span class="risk-pill risk-${escapeHtml(z.risk_level)}">${escapeHtml(z.risk_level)}</span>
        </div>
        <div class="zone-item-sub">${escapeHtml(z.jurisdiction_path || "Unassigned")} &middot; ${Math.round(z.radius_m)} m radius &middot; ${escapeHtml(z.basis)}</div>
        ${z.note ? `<div class="zone-item-note">${escapeHtml(z.note)}</div>` : ""}
        ${canWrite && z.id > 0 ? `<div class="zone-item-actions"><button class="btn btn-danger zone-delete-btn" type="button" data-id="${z.id}">Remove</button></div>` : ""}
      </li>`
      )
      .join("");

    list.querySelectorAll(".zone-delete-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Remove this safety zone?")) return;
        btn.disabled = true;
        try {
          await window.SentinelAPI.deleteSafetyZone(btn.dataset.id);
          await load();
        } catch (e) {
          alert(e.message || "Could not remove zone.");
          btn.disabled = false;
        }
      });
    });
  }

  function renderStats() {
    document.getElementById("stat-zones").textContent = zones.length;
    document.getElementById("stat-high").textContent = zones.filter((z) => z.risk_level === "high" || z.risk_level === "critical").length;
    document.getElementById("stat-districts").textContent = new Set(zones.map((z) => z.jurisdiction_path).filter(Boolean)).size;
  }

  async function load() {
    const { rows, demo } = await window.SentinelAPI.fetchSafetyZones();
    zones = rows;
    const badge = document.getElementById("data-badge");
    badge.textContent = demo ? "Demo / illustrative" : "Curated";
    badge.className = "badge-note " + (demo ? "demo" : "curated");
    drawZones();
    renderList();
    renderStats();
  }

  function wireAddZoneForm() {
    const session = window.SentinelAPI.getSession();
    document.getElementById("add-zone-panel").hidden = !session;
    document.getElementById("signin-prompt").hidden = !!session;
    if (!session) return;

    document.getElementById("zone-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = document.getElementById("zf-error");
      errEl.textContent = "";
      const payload = {
        name: document.getElementById("zf-name").value.trim(),
        jurisdiction_id: parseInt(document.getElementById("zf-jurisdiction").value, 10),
        lat: parseFloat(document.getElementById("zf-lat").value),
        lon: parseFloat(document.getElementById("zf-lon").value),
        radius_m: parseFloat(document.getElementById("zf-radius").value),
        risk_level: document.getElementById("zf-risk").value,
        note: document.getElementById("zf-note").value.trim() || null,
      };
      if (!payload.name || isNaN(payload.jurisdiction_id) || isNaN(payload.lat) || isNaN(payload.lon) || isNaN(payload.radius_m)) {
        errEl.textContent = "Fill in name, location and jurisdiction before adding a zone.";
        return;
      }
      try {
        await window.SentinelAPI.createSafetyZone(payload);
        e.target.reset();
        document.getElementById("zf-risk").value = "medium";
        document.getElementById("zf-radius").value = "300";
        await load();
      } catch (err) {
        errEl.textContent = err.message || "Could not add zone. You may need state or district administrator access.";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initMap();
    wireAddZoneForm();
    load();
  });
})();
