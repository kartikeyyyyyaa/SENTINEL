/* nav.js — shared platform nav strip, rendered into <div id="platform-nav">
 * on every page (landing, login, console, and the three dashboards). One
 * script, one source of truth for the link list and the signed-in/out state,
 * so adding a fifth page later is a one-line change here instead of five
 * hand-edited headers slowly drifting apart.
 *
 * Reads session state the same way api.js does (the sentinel_user key in
 * localStorage) but doesn't depend on api.js being loaded — the console's
 * own login overlay, this nav, and the dashboard pages all need to know
 * "signed in or not" independently and cheaply.
 */
(function () {
  "use strict";

  const NAV_ITEMS = [
    { href: "index.html", label: "Live Ops" },
    { href: "camera-health.html", label: "Camera Health" },
    { href: "incident-reports.html", label: "Incident Reports" },
    { href: "safety-map.html", label: "Safety Map" },
  ];

  const THEME_KEY = "sentinel_theme";

  // A page whose <body> carries data-theme-fixed (landing, login) is always
  // its declared theme and gets no toggle. Every other page (the console and
  // dashboards) defaults to the LIGHT gov-style theme — same look as the
  // landing/sign-in pages, so the whole site reads as one design — and lets
  // the operator switch the control room to dark; the choice is remembered
  // across pages and reloads.
  function themeFixed() { return document.body.hasAttribute("data-theme-fixed"); }
  function savedTheme() {
    try { return window.localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }
  function applySavedTheme() {
    if (themeFixed()) return;
    const saved = savedTheme();          // "light" | "dark" | null (never chosen)
    const light = saved ? saved === "light" : true;   // default: light
    document.body.classList.toggle("theme-light", light);
  }
  function toggleTheme() {
    const nowLight = !document.body.classList.contains("theme-light");
    document.body.classList.toggle("theme-light", nowLight);
    try { window.localStorage.setItem(THEME_KEY, nowLight ? "light" : "dark"); } catch (e) {}
    const btn = document.getElementById("theme-toggle");
    if (btn) setThemeBtn(btn, nowLight);
  }
  function setThemeBtn(btn, isLight) {
    // Label names the mode you'd switch TO, like most theme switchers.
    btn.textContent = isLight ? "Dark mode" : "Light mode";
    btn.setAttribute("aria-label", isLight ? "Switch to dark mode" : "Switch to light mode");
    btn.setAttribute("aria-pressed", isLight ? "true" : "false");
  }

  function currentUser() {
    try {
      const raw = window.localStorage.getItem("sentinel_user");
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function currentPage() {
    const path = window.location.pathname.split("/").pop() || "index.html";
    return path === "" ? "index.html" : path;
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  function render(mount, opts) {
    opts = opts || {};
    const page = currentPage();
    const user = currentUser();

    const nav = el("nav", { class: "pnav", "aria-label": "Sentinel platform navigation" });

    const brand = el("a", { class: "pnav-brand", href: "index.html" }, [
      el("img", { class: "pnav-logo", src: "assets/gujarat-police-logo.svg", alt: "", "aria-hidden": "true" }),
      el("span", { class: "pnav-title" }, [
        document.createTextNode("SENTINEL "),
        el("span", { class: "pnav-slash", text: "//" }),
        document.createTextNode(" "),
        el("span", { class: "pnav-sub", text: "Gujarat Police" }),
      ]),
    ]);
    nav.appendChild(brand);

    const links = el("div", { class: "pnav-links", role: "list" });
    NAV_ITEMS.forEach((item) => {
      const active = item.href === page;
      const a = el("a", {
        class: "pnav-link" + (active ? " pnav-active" : ""),
        href: item.href,
        text: item.label,
      });
      if (active) a.setAttribute("aria-current", "page");
      links.appendChild(a);
    });
    nav.appendChild(links);

    const right = el("div", { class: "pnav-right" });
    if (!themeFixed()) {
      const t = el("button", { class: "pnav-theme", id: "theme-toggle", type: "button" });
      setThemeBtn(t, document.body.classList.contains("theme-light"));
      t.addEventListener("click", toggleTheme);
      right.appendChild(t);
    }
    if (user && user.username) {
      right.appendChild(el("span", { class: "pnav-user", text: "Signed in as " + user.username }));
    } else if (page !== "login.html") {
      right.appendChild(el("a", { class: "pnav-signin", href: "login.html", text: "Sign in" }));
    }
    nav.appendChild(right);

    mount.innerHTML = "";
    mount.appendChild(nav);

    if (opts.tricolor) {
      const strip = el("div", { class: "tricolor-strip", "aria-hidden": "true" }, [
        el("span", {}), el("span", {}), el("span", {}),
      ]);
      mount.parentNode.insertBefore(strip, mount.nextSibling);
    }
  }

  function mount(opts) {
    applySavedTheme();
    const target = document.getElementById("platform-nav");
    if (!target) return;
    opts = opts || { tricolor: target.dataset.tricolor === "true" };
    render(target, opts);
  }

  window.SentinelNav = { mount, NAV_ITEMS };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mount());
  } else {
    mount();
  }
})();
