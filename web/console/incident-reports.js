/* incident-reports.js — the Incident Reports dashboard.
 *
 * Pulls GET /api/reports/incidents (services/registry/app/api/routers/
 * reports.py) through window.SentinelAPI.fetchIncidentReports(), which
 * already carries its own clearly-labelled demo fallback (api.js). Every
 * number here is a count of alerts Sentinel itself opened — see the page
 * copy and reports.py's module docstring for why this is deliberately never
 * called a crime report.
 */
(function () {
  "use strict";

  let allRows = [];
  let districtFilter = "";

  const KIND_LABELS = {
    watchlist_match_vehicle: "Watchlist (vehicle)",
    watchlist_match_person: "Watchlist (person)",
    women_safety_risk: "Women's safety",
    abandoned_object: "Abandoned object",
    crowd_surge: "Crowd surge",
    speed_violation: "Speed violation",
    sos: "SOS",
    stream_gap_prolonged: "Camera stream gap",
  };

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function severityPill(sev) {
    const cls = sev === "critical" ? "risk-critical" : sev === "urgent" ? "risk-high" : sev === "advisory" ? "risk-medium" : "risk-low";
    return `<span class="risk-pill ${cls}">${escapeHtml(sev)}</span>`;
  }

  function fmtWeek(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "&mdash;";
    return escapeHtml(d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }));
  }

  function render() {
    const term = districtFilter.trim().toLowerCase();
    const rows = allRows.filter((r) => !term || r.jurisdiction_name.toLowerCase().includes(term));

    // Stats over the full (unfiltered) window.
    const total = allRows.reduce((sum, r) => sum + r.count, 0);
    const severe = allRows.filter((r) => r.severity === "critical" || r.severity === "urgent")
      .reduce((sum, r) => sum + r.count, 0);
    const byDistrict = new Map();
    allRows.forEach((r) => {
      byDistrict.set(r.jurisdiction_name, (byDistrict.get(r.jurisdiction_name) || 0) + r.count);
    });
    const sorted = Array.from(byDistrict.entries()).sort((a, b) => b[1] - a[1]);

    document.getElementById("stat-total").textContent = total;
    document.getElementById("stat-severe").textContent = severe;
    document.getElementById("stat-districts").textContent = byDistrict.size;
    document.getElementById("stat-top-district").textContent = sorted.length ? sorted[0][0] : "&mdash;";

    const barsWrap = document.getElementById("ir-district-bars");
    const barsEmpty = document.getElementById("ir-district-empty");
    if (!sorted.length) {
      barsWrap.innerHTML = "";
      barsEmpty.hidden = false;
    } else {
      barsEmpty.hidden = true;
      const max = sorted[0][1] || 1;
      barsWrap.innerHTML = sorted
        .map(
          ([name, count]) => `
        <div class="ir-bar-row">
          <span class="ir-bar-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
          <span class="ir-bar-track"><span class="ir-bar-fill" style="width:${Math.max(4, Math.round((count / max) * 100))}%"></span></span>
          <span class="ir-bar-count">${count}</span>
        </div>`
        )
        .join("");
    }

    const tbody = document.getElementById("ir-tbody");
    const empty = document.getElementById("ir-empty");
    if (!rows.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    tbody.innerHTML = rows
      .map(
        (r) => `
      <tr>
        <td>${fmtWeek(r.week_start)}</td>
        <td>${escapeHtml(r.jurisdiction_name)}</td>
        <td><span class="kind-pill">${escapeHtml(KIND_LABELS[r.kind] || r.kind)}</span></td>
        <td>${severityPill(r.severity)}</td>
        <td>${r.count}</td>
      </tr>`
      )
      .join("");
  }

  async function load() {
    const weeks = parseInt(document.getElementById("ir-weeks").value, 10) || 8;
    const { rows, demo } = await window.SentinelAPI.fetchIncidentReports(weeks);
    allRows = rows;
    const badge = document.getElementById("data-badge");
    badge.textContent = demo ? "Demo data" : "Live";
    badge.className = "badge-note " + (demo ? "demo" : "live");
    render();
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("ir-weeks").addEventListener("change", load);
    document.getElementById("ir-search").addEventListener("input", (e) => {
      districtFilter = e.target.value;
      render();
    });
    load();
  });
})();
