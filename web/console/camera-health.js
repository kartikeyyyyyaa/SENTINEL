/* camera-health.js — the Camera Health dashboard.
 *
 * Pulls GET /api/camera-health (services/registry/app/api/routers/health.py)
 * through window.SentinelAPI.fetchCameraHealth(), which already carries its
 * own honest demo fallback (api.js). This file only renders what it gets:
 * it never computes an uptime percentage or invents a status, because the
 * API itself refuses to (a camera with no probe stays null, on purpose).
 */
(function () {
  "use strict";

  let rows = [];
  let activeFilter = "all";
  let searchTerm = "";

  function classify(row) {
    if (row.last_checked_at === null) return "unknown";
    if (row.last_is_live === false) return "faulty";
    return "live";
  }

  function statusChip(row) {
    const kind = classify(row);
    const label = kind === "unknown" ? "Never checked" : kind === "faulty" ? "Faulty" : "Live";
    const cls = kind === "unknown" ? "status-inactive" : kind === "faulty" ? "status-faulty" : "status-active";
    return `<span class="status-chip ${cls}"><span class="dot"></span>${label}</span>`;
  }

  function fmtTime(iso) {
    if (!iso) return "&mdash;";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "&mdash;";
    return escapeHtml(d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }));
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function render() {
    const tbody = document.getElementById("ch-tbody");
    const empty = document.getElementById("ch-empty");
    const term = searchTerm.trim().toLowerCase();

    const filtered = rows.filter((r) => {
      if (activeFilter !== "all" && classify(r) !== activeFilter) return false;
      if (!term) return true;
      return (r.name + " " + r.code + " " + (r.jurisdiction_path || "")).toLowerCase().includes(term);
    });

    document.getElementById("stat-total").textContent = rows.length || "0";
    document.getElementById("stat-live").textContent = rows.filter((r) => classify(r) === "live").length;
    document.getElementById("stat-faulty").textContent = rows.filter((r) => classify(r) === "faulty").length;
    document.getElementById("stat-unknown").textContent = rows.filter((r) => classify(r) === "unknown").length;

    if (!filtered.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    tbody.innerHTML = filtered
      .map(
        (r) => `
      <tr>
        <td>${escapeHtml(r.code)}</td>
        <td>${escapeHtml(r.name)}</td>
        <td>${r.jurisdiction_path ? escapeHtml(r.jurisdiction_path) : "&mdash;"}</td>
        <td>${escapeHtml(r.camera_type)}</td>
        <td>${statusChip(r)}</td>
        <td>${r.last_probe ? escapeHtml(r.last_probe) : "&mdash;"}</td>
        <td>${r.last_latency_ms != null ? r.last_latency_ms + " ms" : "&mdash;"}</td>
        <td>${fmtTime(r.last_checked_at)}</td>
      </tr>`
      )
      .join("");
  }

  async function load() {
    const { rows: data, demo } = await window.SentinelAPI.fetchCameraHealth();
    rows = data;
    const badge = document.getElementById("data-badge");
    badge.textContent = demo ? "Demo data" : "Live";
    badge.className = "badge-note " + (demo ? "demo" : "live");
    render();
  }

  function wireFilters() {
    document.getElementById("ch-filters").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-filter]");
      if (!btn) return;
      activeFilter = btn.dataset.filter;
      document.querySelectorAll("#ch-filters button").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("btn-active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      render();
    });
    document.getElementById("ch-search").addEventListener("input", (e) => {
      searchTerm = e.target.value;
      render();
    });
    document.getElementById("refresh-btn").addEventListener("click", load);
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireFilters();
    load();
  });
})();
