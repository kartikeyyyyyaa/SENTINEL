/* app.js — Control Room UI.
 *
 * Satellite map (Esri, free/keyless) with clustered camera pins; live HLS video
 * in the camera panel and a multi-camera video wall; alerts with sound, type
 * filters and a dispatch action; cross-camera vehicle tracking drawn on the map;
 * women's-safety SOS + mode; and an English / Gujarati / Hindi interface.
 *
 * Live video is played through the console's own origin (/grid/<id>/index.m3u8,
 * proxied by nginx) so the browser stays same-origin and the grid password
 * stays server-side. Where that proxy isn't configured (or the grid network
 * isn't reachable), the player shows a clear message instead of erroring.
 *
 * Data comes from window.SentinelAPI. No inline handlers (CSP is script-src
 * 'self'); every boot step is wrapped so one failure can't take the UI down.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- i18n
  const I18N = {
    en: {
      cameras: "Cameras", searchCameras: "Search name, code or district…", all: "All", live: "Live",
      faulty: "Faulty", offline: "Offline", activeAlert: "active alert", fitAll: "Fit all cameras",
      alerts: "Alerts", trackVehicle: "Track a vehicle", activity: "Activity", watchlistF: "Watchlist",
      safetyF: "Women’s safety", otherF: "Other", search: "Search",
      wlHint: "Search the watchlist, then open a vehicle to see its route across cameras on the map.",
      openAlerts: "open alerts", metaNote: "Edge analytics are metadata-only · live video on demand.",
      videoWall: "Video wall", womenSafety: "Women’s Safety", signInSub: "Sign in to the operator console",
      username: "Username", password: "Password", signIn: "Sign in",
      continueDemo: "Continue without signing in — demo data only →", raiseSos: "Raise an SOS",
      sosBody: "This sends a real women’s-safety panic signal and opens a critical alert immediately — no login required, exactly as a public kiosk or the mobile app would send it.",
      cancel: "Cancel", sendSos: "Send SOS", searchWatchlist: "Plate, name or case reference…",
      showOnMap: "Show on map", trackRoute: "Track route", acknowledge: "Acknowledge", close: "Close",
      dispatch: "Dispatch", trackRouteMap: "Track route on map", openDetails: "Open details",
      liveView: "Live view", noStream: "No stream for this camera",
      videoUnavail: "Live view unavailable here — enable the grid video proxy (nginx.conf) and open on the grid network.",
      skipToMap: "Skip to map", dataPrivacy: "Data & Privacy",
      privacyTitle: "Data & Privacy", privacyH1: "What is collected, and why",
      privacyP1: "Camera metadata, number-plate reads and watchlist matches are logged against a stated purpose and case reference. Raw video is never stored by Sentinel — it is only ever proxied live, on demand.",
      privacyH2: "Automated matching",
      privacyP2: "Plate and face matching are automated; opening an alert is automated. Dispatching, closing or acting on an alert is always a human decision, recorded under the operator’s own identity.",
      privacyH3: "Access control & audit",
      privacyP3: "Every access is scoped by jurisdiction at the database level and written to an append-only, tamper-evident audit trail.",
      privacyRef: "Full detail:",
      usernameRequired: "Enter your username.", passwordRequired: "Enter your password.",
      cameraAria: "camera", statusAria: "status",
      mapDark: "Dark map", mapSatellite: "Satellite", fitAll: "Fit all cameras",
      simulate: "Simulate incident ▾", dispatchTitle: "Auto-response relay",
      dpStep1: "Incident recognised", dpStep2: "Nearest responder located", dpStep3: "Relayed to responder, escalated to control room",
    },
    gu: {
      cameras: "કૅમેરા", searchCameras: "નામ, કોડ કે જિલ્લો શોધો…", all: "બધા", live: "લાઇવ",
      faulty: "ખરાબ", offline: "ઑફલાઇન", activeAlert: "સક્રિય એલર્ટ", fitAll: "બધા કૅમેરા બતાવો",
      alerts: "એલર્ટ", trackVehicle: "વાહન ટ્રૅક કરો", activity: "પ્રવૃત્તિ", watchlistF: "વૉચલિસ્ટ",
      safetyF: "મહિલા સુરક્ષા", otherF: "અન્ય", search: "શોધો",
      wlHint: "વૉચલિસ્ટ શોધો, પછી નકશા પર વાહનનો માર્ગ જોવા તેને ખોલો.",
      openAlerts: "ખુલ્લા એલર્ટ", metaNote: "એજ એનાલિટિક્સ માત્ર મેટાડેટા · લાઇવ વિડિયો માંગ પ્રમાણે",
      videoWall: "વિડિયો વૉલ", womenSafety: "મહિલા સુરક્ષા", signInSub: "ઑપરેટર કન્સોલમાં સાઇન ઇન કરો",
      username: "વપરાશકર્તા નામ", password: "પાસવર્ડ", signIn: "સાઇન ઇન",
      continueDemo: "સાઇન ઇન વગર ચાલુ રાખો — માત્ર ડેમો ડેટા →", raiseSos: "SOS મોકલો",
      sosBody: "આ ખરેખરની મહિલા-સુરક્ષા પૅનિક સિગ્નલ મોકલે છે અને તરત જ ક્રિટિકલ એલર્ટ ખોલે છે — લૉગિન જરૂરી નથી.",
      cancel: "રદ કરો", sendSos: "SOS મોકલો", searchWatchlist: "પ્લેટ, નામ કે કેસ સંદર્ભ…",
      showOnMap: "નકશા પર બતાવો", trackRoute: "માર્ગ ટ્રૅક કરો", acknowledge: "સ્વીકારો", close: "બંધ કરો",
      dispatch: "ડિસ્પૅચ", trackRouteMap: "નકશા પર માર્ગ ટ્રૅક કરો", openDetails: "વિગત ખોલો",
      liveView: "લાઇવ દૃશ્ય", noStream: "આ કૅમેરા માટે કોઈ સ્ટ્રીમ નથી",
      videoUnavail: "અહીં લાઇવ દૃશ્ય ઉપલબ્ધ નથી — ગ્રિડ વિડિયો પ્રૉક્સી ચાલુ કરો (nginx.conf) અને ગ્રિડ નેટવર્ક પર ખોલો.",
      skipToMap: "નકશા પર જાઓ", dataPrivacy: "ડેટા અને ગોપનીયતા",
      privacyTitle: "ડેટા અને ગોપનીયતા", privacyH1: "શું એકત્રિત થાય છે, અને શા માટે",
      privacyP1: "કૅમેરા મેટાડેટા, નંબર પ્લેટ રીડિંગ્સ અને વૉચલિસ્ટ મેચ એક જણાવેલ હેતુ અને કેસ સંદર્ભ સામે લૉગ થાય છે. રો વિડિયો ક્યારેય સેન્ટિનલ દ્વારા સંગ્રહિત થતો નથી — તે ફક્ત માંગ પર લાઇવ પ્રોક્સી થાય છે.",
      privacyH2: "સ્વયંસંચાલિત મેચિંગ",
      privacyP2: "પ્લેટ અને ચહેરો મેચિંગ સ્વયંસંચાલિત છે; એલર્ટ ખોલવું સ્વયંસંચાલિત છે. ડિસ્પૅચ કરવું, બંધ કરવું અથવા એલર્ટ પર પગલાં લેવું હંમેશા માનવીય નિર્ણય છે, ઑપરેટરની પોતાની ઓળખ હેઠળ નોંધાયેલ.",
      privacyH3: "ઍક્સેસ નિયંત્રણ અને ઑડિટ",
      privacyP3: "દરેક ઍક્સેસ ડેટાબેઝ સ્તરે અધિકારક્ષેત્ર દ્વારા મર્યાદિત છે અને એક એપેન્ડ-ઓન્લી, ટેમ્પર-એવિડન્ટ ઑડિટ ટ્રેલમાં લખાય છે.",
      privacyRef: "વિગત માટે:",
      usernameRequired: "તમારું વપરાશકર્તા નામ દાખલ કરો.", passwordRequired: "તમારો પાસવર્ડ દાખલ કરો.",
      cameraAria: "કૅમેરા", statusAria: "સ્થિતિ",
    },
    hi: {
      cameras: "कैमरे", searchCameras: "नाम, कोड या ज़िला खोजें…", all: "सभी", live: "लाइव",
      faulty: "खराब", offline: "ऑफ़लाइन", activeAlert: "सक्रिय अलर्ट", fitAll: "सभी कैमरे दिखाएँ",
      alerts: "अलर्ट", trackVehicle: "वाहन ट्रैक करें", activity: "गतिविधि", watchlistF: "वॉचलिस्ट",
      safetyF: "महिला सुरक्षा", otherF: "अन्य", search: "खोजें",
      wlHint: "वॉचलिस्ट खोजें, फिर मानचित्र पर वाहन का मार्ग देखने के लिए उसे खोलें.",
      openAlerts: "खुले अलर्ट", metaNote: "एज एनालिटिक्स केवल मेटाडेटा · लाइव वीडियो माँग पर",
      videoWall: "वीडियो वॉल", womenSafety: "महिला सुरक्षा", signInSub: "ऑपरेटर कंसोल में साइन इन करें",
      username: "उपयोगकर्ता नाम", password: "पासवर्ड", signIn: "साइन इन",
      continueDemo: "बिना साइन इन जारी रखें — केवल डेमो डेटा →", raiseSos: "SOS भेजें",
      sosBody: "यह वास्तविक महिला-सुरक्षा पैनिक संकेत भेजता है और तुरंत एक क्रिटिकल अलर्ट खोलता है — लॉगिन आवश्यक नहीं.",
      cancel: "रद्द करें", sendSos: "SOS भेजें", searchWatchlist: "प्लेट, नाम या केस संदर्भ…",
      showOnMap: "मानचित्र पर दिखाएँ", trackRoute: "मार्ग ट्रैक करें", acknowledge: "स्वीकारें", close: "बंद करें",
      dispatch: "डिस्पैच", trackRouteMap: "मानचित्र पर मार्ग ट्रैक करें", openDetails: "विवरण खोलें",
      liveView: "लाइव दृश्य", noStream: "इस कैमरे के लिए कोई स्ट्रीम नहीं",
      videoUnavail: "यहाँ लाइव दृश्य उपलब्ध नहीं — ग्रिड वीडियो प्रॉक्सी सक्षम करें (nginx.conf) और ग्रिड नेटवर्क पर खोलें.",
      skipToMap: "मानचित्र पर जाएँ", dataPrivacy: "डेटा और गोपनीयता",
      privacyTitle: "डेटा और गोपनीयता", privacyH1: "क्या एकत्र किया जाता है, और क्यों",
      privacyP1: "कैमरा मेटाडेटा, नंबर प्लेट रीडिंग और वॉचलिस्ट मैच एक बताए गए उद्देश्य और केस संदर्भ के विरुद्ध लॉग किए जाते हैं। रॉ वीडियो कभी भी सेंटिनल द्वारा संग्रहीत नहीं किया जाता — यह केवल माँग पर लाइव प्रॉक्सी किया जाता है।",
      privacyH2: "स्वचालित मिलान",
      privacyP2: "प्लेट और चेहरा मिलान स्वचालित है; अलर्ट खोलना स्वचालित है। डिस्पैच करना, बंद करना या अलर्ट पर कार्रवाई करना हमेशा एक मानवीय निर्णय है, जो ऑपरेटर की अपनी पहचान के तहत दर्ज होता है।",
      privacyH3: "पहुँच नियंत्रण और ऑडिट",
      privacyP3: "हर पहुँच डेटाबेस स्तर पर क्षेत्राधिकार द्वारा सीमित है और एक एपेंड-ओनली, छेड़छाड़-प्रतिरोधी ऑडिट ट्रेल में लिखी जाती है।",
      privacyRef: "पूरी जानकारी:",
      usernameRequired: "अपना उपयोगकर्ता नाम दर्ज करें।", passwordRequired: "अपना पासवर्ड दर्ज करें।",
      cameraAria: "कैमरा", statusAria: "स्थिति",
    },
  };
  let lang = "en";
  try { lang = localStorage.getItem("sentinel_lang") || "en"; } catch (e) {}
  function t(key, en) { return (I18N[lang] && I18N[lang][key]) || en || key; }
  function applyLang() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const k = el.getAttribute("data-i18n"); const v = I18N[lang] && I18N[lang][k];
      if (v) el.textContent = v;
    });
    document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      const k = el.getAttribute("data-i18n-ph"); const v = I18N[lang] && I18N[lang][k];
      if (v) el.setAttribute("placeholder", v);
    });
  }

  // ---------------------------------------------------------------- map / video config
  // Two free, keyless basemaps, BOTH served from server.arcgisonline.com — the
  // one host already allowed in nginx's CSP img-src — so the dark option adds
  // no new network origin and needs no config change. Satellite (Esri World
  // Imagery) is the default because seeing the actual streets and buildings
  // matters for police; "Dark map" (Esri Dark Gray Canvas) is a clean, low-ink
  // basemap that matches the console's navy theme and makes coloured pins and
  // the dispatch routing lines pop. Operator picks; choice persists.
  const SAT = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
  const SAT_ATTR = "Imagery &copy; Esri, Maxar, Earthstar Geographics";
  const LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
  const DARK = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}";
  const DARK_LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}";
  const DARK_ATTR = "Tiles &copy; Esri &mdash; Esri, DeLorme, HERE";
  const CENTER = [22.6, 71.6], ZOOM = 7;

  const ANPR_KINDS = new Set(["watchlist_match_vehicle", "watchlist_match_person"]);
  const SEV_RANK = { critical: 0, urgent: 1, advisory: 2, info: 3 };

  function pinClass(status, alert, safety) {
    if (alert) return "s-alert";
    if (safety) return "s-isolated";
    if (status === "active") return "s-active";
    if (status === "faulty" || status === "maintenance") return "s-faulty";
    return "s-inactive";
  }
  // GRID-CAM07 -> cam07 (the grid's stream id); null for non-grid cameras.
  function gridId(c) {
    const m = /^GRID-(CAM\d+)$/i.exec(c.code || "");
    return m ? m[1].toLowerCase() : null;
  }

  // ---------------------------------------------------------------- geo
  function hav(a, b) { const R=6371, dLa=(b[0]-a[0])*Math.PI/180, dLo=(b[1]-a[1])*Math.PI/180;
    const s=Math.sin(dLa/2)**2+Math.cos(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*Math.sin(dLo/2)**2;
    return R*2*Math.atan2(Math.sqrt(s),Math.sqrt(1-s)); }
  function brg(a,b){ const p1=a[0]*Math.PI/180,p2=b[0]*Math.PI/180,dl=(b[1]-a[1])*Math.PI/180;
    const y=Math.sin(dl)*Math.cos(p2),x=Math.cos(p1)*Math.sin(p2)-Math.sin(p1)*Math.cos(p2)*Math.cos(dl);
    return ((Math.atan2(y,x)*180/Math.PI)+360)%360; }
  function compass(d){ return ["N","NE","E","SE","S","SW","W","NW"][Math.round(d/45)%8]; }

  // ---------------------------------------------------------------- state
  let cameras = [], map = null, cluster = null, markers = new Map(), trailLayer = null;
  let safetyZoneLayer = null, safetyZones = [], safetyZonesLoaded = false;
  let pcrLayer = null, relayLayer = null, pcrs = [], hospitals = [], fireStations = [], controlRoom = null, pcrsLoaded = false;
  let baseSat = null, baseSatLabels = null, baseDark = null, baseDarkLabels = null, basemap = "satellite";
  let statusFilter = "all", searchTerm = "", safetyMode = false, alertFilter = "all";
  const liveTracks = new Set(), alertsById = new Map();
  let openAlertCount = 0, audioCtx = null, seenAlertIds = new Set();
  const hlsInstances = new Map();

  document.addEventListener("DOMContentLoaded", async () => {
    step("lang", () => { applyLang(); wireLang(); });
    step("map", initMap);
    step("clock", initClock);
    step("sidebar", wireSidebar);
    step("tabs", wireTabs);
    step("alert-filters", wireAlertFilters);
    step("drawer", wireDrawer);
    step("wall", wireWall);
    step("reset", () => document.getElementById("reset-view").addEventListener("click", fitToCameras));
    step("auth", wireAuth);
    step("safety", wireSafety);
    step("sos", wireSos);
    step("privacy", wirePrivacy);
    step("watchlist", wireWatchlist);
    step("dispatch", wireDispatchPanel);
    step("overlay-escape", wireOverlayEscape);
    await boot();
  });
  function step(l, fn) { try { fn(); } catch (e) { console.error("init:", l, e); } }

  async function boot() {
    const s = window.SentinelAPI.getSession();
    updateUserChip(s ? s.user : null);
    cameras = await window.SentinelAPI.fetchCameras();
    renderCameraList(); plotCameras(); fitToCameras(); updateDataMode();
    if (s) await loadAlerts(); else refreshAll();
    window.SentinelAPI.connectEvents(cameras, handleMessage);
    setTimeout(updateDataMode, 2600);
  }

  function wireLang() {
    const sel = document.getElementById("lang-select");
    sel.value = lang;
    sel.addEventListener("change", () => {
      lang = sel.value; try { localStorage.setItem("sentinel_lang", lang); } catch (e) {}
      applyLang(); refreshAll();
    });
  }

  // ---------------------------------------------------------------- map
  function initMap() {
    map = L.map("map", { zoomControl: true, minZoom: 5, maxZoom: 18 }).setView(CENTER, ZOOM);
    baseSat = L.tileLayer(SAT, { attribution: SAT_ATTR, maxZoom: 18 });
    baseSatLabels = L.tileLayer(LABELS, { maxZoom: 18, opacity: 0.9 });
    baseDark = L.tileLayer(DARK, { attribution: DARK_ATTR, maxZoom: 16 });
    baseDarkLabels = L.tileLayer(DARK_LABELS, { maxZoom: 16, opacity: 0.9 });
    try { basemap = localStorage.getItem("sentinel_basemap") || "satellite"; } catch (e) {}
    applyBasemap();
    cluster = L.markerClusterGroup({ maxClusterRadius: 45, showCoverageOnHover: false, spiderfyOnMaxZoom: true });
    map.addLayer(cluster);
    // Curated women's-safety zones (see safety-map.html / db/migrations/011_safety_zone.sql)
    // live only on this layer group, only while Women's Safety mode is on — a
    // police judgement call overlaid on the operator's own map, not shown by default.
    safetyZoneLayer = L.layerGroup();
    // PCR units + control room (demo data, api.js). PCRs are response
    // infrastructure so they're shown by default; relayLayer holds the
    // incident→PCR→control-room routing lines drawn during a dispatch.
    pcrLayer = L.layerGroup().addTo(map);
    relayLayer = L.layerGroup();
    loadPcrs();
  }
  function applyBasemap() {
    if (!map) return;
    [baseSat, baseSatLabels, baseDark, baseDarkLabels].forEach((l) => { if (l && map.hasLayer(l)) map.removeLayer(l); });
    if (basemap === "dark") { baseDark.addTo(map); baseDarkLabels.addTo(map); }
    else { baseSat.addTo(map); baseSatLabels.addTo(map); }
    // Keep overlays above the freshly-added basemap tiles.
    if (cluster && map.hasLayer(cluster)) cluster.bringToFront && cluster.bringToFront();
  }
  function toggleBasemap() {
    basemap = basemap === "dark" ? "satellite" : "dark";
    try { localStorage.setItem("sentinel_basemap", basemap); } catch (e) {}
    applyBasemap();
    const btn = document.getElementById("basemap-btn");
    if (btn) btn.textContent = basemap === "dark" ? t("mapSatellite", "Satellite") : t("mapDark", "Dark map");
  }
  const RISK_ZONE_COLOR = { low: "#9db0ce", medium: "#f59e0b", high: "#fb923c", critical: "#ef4444" };
  async function loadSafetyZones() {
    try {
      const { rows } = await window.SentinelAPI.fetchSafetyZones();
      safetyZones = rows;
      drawSafetyZones();
    } catch (e) { /* zones stay empty; toggle still works for alerts */ }
  }
  function drawSafetyZones() {
    if (!safetyZoneLayer) return;
    safetyZoneLayer.clearLayers();
    for (const z of safetyZones) {
      L.circle([z.lat, z.lon], {
        radius: z.radius_m,
        color: RISK_ZONE_COLOR[z.risk_level] || RISK_ZONE_COLOR.medium,
        weight: 2, fillOpacity: 0.15,
      }).bindTooltip(esc(z.name) + " — " + esc(z.risk_level) + " (curated)", { sticky: true }).addTo(safetyZoneLayer);
    }
  }

  // ---------------------------------------------------------------- responders + response routing
  // Three responder categories, each its own marker style and relay colour.
  // The incident type decides which one an incident is routed to.
  const RESPONDER = {
    police:  { label: "PCR unit",      pin: "pcr-pin",   color: "#5b9dff" },
    medical: { label: "hospital",      pin: "hosp-pin",  color: "#14b8a6" },
    fire:    { label: "fire station",  pin: "fire-pin",  color: "#f26522" },
  };
  function responderSet(kind) {
    if (kind === "medical") return hospitals;
    if (kind === "fire") return fireStations;
    return pcrs;
  }
  // Map a recognised incident's kind to the responder who should get it first.
  function kindToResponder(kind) {
    const k = (kind || "").toLowerCase();
    if (k.includes("fire")) return "fire";
    if (k.includes("accident") || k.includes("collision") || k.includes("crash") || k.includes("medical")) return "medical";
    return "police";
  }

  async function loadPcrs() {
    try {
      const r = await window.SentinelAPI.fetchPcrs();
      pcrs = r.pcrs || []; hospitals = r.hospitals || []; fireStations = r.fireStations || [];
      controlRoom = r.controlRoom || null; pcrsLoaded = true;
      drawResponders();
    } catch (e) { /* map still works without responders */ }
  }
  function drawResponders() {
    if (!pcrLayer) return;
    pcrLayer.clearLayers();
    const add = (list, kind, glyph) => {
      for (const p of list) {
        const icon = L.divIcon({ className: "", html: '<div class="' + RESPONDER[kind].pin + '">' + (glyph || "") + '</div>', iconSize: [18,18], iconAnchor: [9,9] });
        L.marker([p.lat, p.lon], { icon })
          .bindTooltip(esc(p.name) + " · dial " + esc(p.phone), { direction: "top" })
          .addTo(pcrLayer);
      }
    };
    add(pcrs, "police", "");
    add(hospitals, "medical", "+");
    add(fireStations, "fire", "");
    if (controlRoom) {
      const icon = L.divIcon({ className: "", html: '<div class="cr-pin" title="State Control Room">★</div>', iconSize: [24,24], iconAnchor: [12,12] });
      L.marker([controlRoom.lat, controlRoom.lon], { icon })
        .bindTooltip(esc(controlRoom.name), { direction: "top" })
        .addTo(pcrLayer);
    }
  }
  function haversineKm(a, b) {
    const R = 6371, toRad = (d) => d * Math.PI / 180;
    const dLat = toRad(b[0] - a[0]), dLon = toRad(b[1] - a[1]);
    const s = Math.sin(dLat/2)**2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
  }
  function nearestResponder(ll, kind) {
    let best = null, bestD = Infinity;
    for (const p of responderSet(kind)) { const d = haversineKm(ll, [p.lat, p.lon]); if (d < bestD) { bestD = d; best = p; } }
    return best ? { unit: best, distKm: bestD } : null;
  }
  // The differentiator: a recognised incident is relayed to the NEAREST
  // responder of the right type (PCR for crime, hospital for an accident, fire
  // station for a fire) first, then escalated to the state control room. Draws
  // that routing on the map and animates the relay panel. Returns the dispatch
  // record so the alert card can show it.
  function runRelay(ll, label, kind, isAuto) {
    kind = kind || "police";
    if (!ll || !relayLayer || !controlRoom || !responderSet(kind).length) return null;
    const near = nearestResponder(ll, kind); if (!near) return null;
    const uLL = [near.unit.lat, near.unit.lon], crLL = [controlRoom.lat, controlRoom.lon];
    const etaMin = Math.max(1, Math.round(near.distKm / 32 * 60)); // ~32 km/h avg urban response
    const legColor = RESPONDER[kind].color;
    relayLayer.clearLayers();
    L.circleMarker(ll, { radius: 8, color: "#ef4444", weight: 2, fillColor: "#ef4444", fillOpacity: 0.5 })
      .bindTooltip("Incident: " + esc(label), { direction: "top", permanent: false }).addTo(relayLayer);
    L.polyline([ll, uLL], { color: legColor, weight: 3, opacity: 0.95, dashArray: "1 9", lineCap: "round" }).addTo(relayLayer);
    L.polyline([uLL, crLL], { color: "#5b9dff", weight: 2.5, opacity: 0.85, dashArray: "2 10", lineCap: "round" }).addTo(relayLayer);
    if (!map.hasLayer(relayLayer)) map.addLayer(relayLayer);
    try { map.fitBounds(L.latLngBounds([ll, uLL, crLL]).pad(0.35), { animate: true }); } catch (e) {}
    showDispatchPanel({ label, near, etaMin, isAuto, kind });
    return { unit: near.unit, distKm: near.distKm, etaMin, kind };
  }
  function showDispatchPanel(d) {
    const panel = document.getElementById("dispatch-panel"); if (!panel) return;
    panel.setAttribute("data-kind", d.kind);
    panel.querySelector("#dp-incident").textContent = d.label;
    panel.querySelector("#dp-step2-label").textContent = "Nearest " + RESPONDER[d.kind].label + " located";
    panel.querySelector("#dp-pcr").textContent = d.near.unit.name;
    panel.querySelector("#dp-dist").textContent = d.near.distKm.toFixed(1) + " km · ~" + d.etaMin + " min · dial " + d.near.unit.phone;
    panel.querySelector("#dp-cr").textContent = controlRoom.name;
    panel.querySelector("#dp-mode").textContent = d.isAuto ? "Auto-relay" : "Manual dispatch";
    panel.hidden = false;
    // Stagger the three steps so the relay reads as a sequence, not a dump.
    const steps = panel.querySelectorAll(".dp-step");
    steps.forEach((s, i) => { s.classList.remove("on"); setTimeout(() => s.classList.add("on"), 120 + i * 550); });
    clearTimeout(showDispatchPanel._t);
    showDispatchPanel._t = setTimeout(clearRelay, 14000);
  }
  function clearRelay() {
    const panel = document.getElementById("dispatch-panel"); if (panel) panel.hidden = true;
    if (relayLayer) relayLayer.clearLayers();
  }
  function wireDispatchPanel() {
    const close = document.getElementById("dp-close");
    if (close) close.addEventListener("click", clearRelay);
    const bm = document.getElementById("basemap-btn");
    if (bm) bm.addEventListener("click", toggleBasemap);
    // Simulate menu: a small dropdown of the three incident scenarios.
    const sim = document.getElementById("simulate-btn"), menu = document.getElementById("sim-menu");
    if (sim && menu) {
      sim.addEventListener("click", (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; });
      menu.querySelectorAll("button[data-sim]").forEach((b) =>
        b.addEventListener("click", () => { menu.hidden = true; simulateIncident(b.dataset.sim); }));
      document.addEventListener("click", (e) => { if (menu && !menu.hidden && !e.target.closest(".sim-wrap")) menu.hidden = true; });
    }
  }
  // Demo trigger: fabricate a recognised incident of the chosen scenario on a
  // random live camera and run the relay end to end. Labelled DEMO in the
  // panel; this is how the flow is shown when no live backend is raising alerts.
  const SIM_SCENARIOS = {
    crime:    { kind: "police",  label: "women safety risk" },
    accident: { kind: "medical", label: "road accident" },
    fire:     { kind: "fire",    label: "fire detected" },
  };
  function simulateIncident(scenario) {
    const s = SIM_SCENARIOS[scenario] || SIM_SCENARIOS.crime;
    const pool = cameras.filter((c) => c.status === "active" && camLatLng(c));
    if (!pool.length) { toast("No active cameras to simulate from.", true); return; }
    const c = pool[Math.floor(Math.random() * pool.length)];
    const rec = runRelay(camLatLng(c), c.name + " · " + s.label, s.kind, true);
    if (!rec) { toast("Dispatch routing unavailable.", true); return; }
    toast("DEMO " + s.label + " at " + c.name + " → nearest " + RESPONDER[s.kind].label + " " + rec.unit.name + " (" + rec.distKm.toFixed(1) + " km) → control room.", true);
  }
  function camLatLng(c) { return (c.location && c.location.lat != null) ? [c.location.lat, c.location.lon] : null; }
  function camerasWithAlerts() {
    const ids = new Set();
    for (const a of alertsById.values())
      if ((a.status === "open" || a.status === "acknowledged") && a.camera_id != null) ids.add(a.camera_id);
    return ids;
  }
  function plotCameras() {
    if (!map || !cluster) return;
    cluster.clearLayers(); markers.clear();
    const alerted = camerasWithAlerts();
    for (const c of visibleCameras()) {
      const ll = camLatLng(c); if (!ll) continue;
      const cls = pinClass(c.status, alerted.has(c.camera_id), safetyMode && c.isolated);
      const icon = L.divIcon({ className: "", html: '<div class="map-pin ' + cls + '"></div>', iconSize: [16,16], iconAnchor: [8,8] });
      const m = L.marker(ll, { icon });
      m.bindPopup(popupHtml(c));
      m.on("popupopen", () => {
        const b = document.querySelector(".popup-open[data-cam='" + c.camera_id + "']");
        if (b) b.addEventListener("click", () => selectCamera(c.camera_id, false));
      });
      cluster.addLayer(m); markers.set(c.camera_id, m);
    }
  }
  function fitToCameras() {
    if (!map) return;
    const pts = cameras.map(camLatLng).filter(Boolean);
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15)); else map.setView(CENTER, ZOOM);
  }
  function popupHtml(c) {
    return '<div><b>' + esc(c.name) + '</b><div class="popup-sub">' + esc(c.code) + ' &middot; ' + esc(c.status) + '</div>' +
      '<div class="popup-sub">' + esc(c.jurisdiction_path || c.department || "") + '</div>' +
      '<span class="popup-open" data-cam="' + c.camera_id + '">' + t("openDetails","Open details") + ' &rarr;</span></div>';
  }

  // ---------------------------------------------------------------- HLS video
  function attachHls(videoEl, url, msgEl, spinnerEl) {
    detachHls(videoEl, spinnerEl);
    if (msgEl) msgEl.textContent = "";
    if (spinnerEl) spinnerEl.hidden = false;
    let failed = false, started = false;
    const stopSpinner = () => { if (spinnerEl) spinnerEl.hidden = true; };
    const fail = () => { if (!failed) { failed = true; if (msgEl) msgEl.textContent = t("videoUnavail"); stopSpinner(); } };
    videoEl.addEventListener("playing", () => { started = true; stopSpinner(); }, { once: true });
    try {
      if (window.Hls && window.Hls.isSupported()) {
        const h = new window.Hls({ liveDurationInfinity: true, maxBufferLength: 8, manifestLoadingTimeOut: 8000 });
        h.loadSource(url); h.attachMedia(videoEl);
        h.on(window.Hls.Events.ERROR, (evt, data) => { if (data && data.fatal) { fail(); try { h.destroy(); } catch (e) {} hlsInstances.delete(videoEl); } });
        hlsInstances.set(videoEl, h);
        videoEl.play().catch(() => {});
      } else if (videoEl.canPlayType("application/vnd.apple.mpegurl")) {
        videoEl.src = url; videoEl.addEventListener("error", fail, { once: true }); videoEl.play().catch(() => {});
      } else { fail(); }
    } catch (e) { fail(); }
    // Safety net: if nothing is playing shortly, show the message.
    setTimeout(() => { if (!started && videoEl.readyState < 2) fail(); }, 9000);
  }
  function detachHls(videoEl, spinnerEl) {
    const h = hlsInstances.get(videoEl);
    if (h) { try { h.destroy(); } catch (e) {} hlsInstances.delete(videoEl); }
    try { videoEl.pause(); } catch (e) {}
    videoEl.removeAttribute("src"); try { videoEl.load(); } catch (e) {}
    if (spinnerEl) spinnerEl.hidden = true;
  }

  // ---------------------------------------------------------------- cameras list / drawer
  function wireSidebar() {
    document.getElementById("camera-search").addEventListener("input", (e) => { searchTerm = e.target.value.trim().toLowerCase(); renderCameraList(); plotCameras(); });
    document.querySelectorAll("#status-filters .chip").forEach((chip) => chip.addEventListener("click", () => {
      document.querySelectorAll("#status-filters .chip").forEach((c) => { c.classList.remove("chip-active"); c.setAttribute("aria-pressed", "false"); });
      chip.classList.add("chip-active"); chip.setAttribute("aria-pressed", "true");
      statusFilter = chip.dataset.status; renderCameraList(); plotCameras();
    }));
  }
  function visibleCameras() {
    return cameras.filter((c) => {
      if (statusFilter === "active" && c.status !== "active") return false;
      if (statusFilter === "faulty" && !(c.status === "faulty" || c.status === "maintenance")) return false;
      if (statusFilter === "inactive" && !(c.status === "inactive" || c.status === "decommissioned")) return false;
      if (!searchTerm) return true;
      return (c.code + " " + c.name + " " + (c.jurisdiction_path || "")).toLowerCase().includes(searchTerm);
    });
  }
  function renderCameraList() {
    const list = document.getElementById("camera-list"), rows = visibleCameras();
    list.setAttribute("aria-busy", "false");
    document.getElementById("cam-count").textContent = rows.length + " / " + cameras.length;
    list.innerHTML = "";
    if (!rows.length) { list.innerHTML = '<li class="empty-note">—</li>'; return; }
    const alerted = camerasWithAlerts();
    for (const c of rows) {
      const li = document.createElement("li"); li.className = "camera-row";
      li.tabIndex = 0; li.setAttribute("role", "button");
      li.setAttribute("aria-label", c.name + ", " + t("cameraAria","camera") + " " + c.code + ", " + t("statusAria","status") + " " + c.status);
      const cls = pinClass(c.status, alerted.has(c.camera_id), false);
      const color = { "s-active":"#22c55e","s-alert":"#ef4444","s-faulty":"#f59e0b","s-inactive":"#64748b","s-isolated":"#ec4899" }[cls];
      li.innerHTML = '<span class="cam-dot" style="background:' + color + '"></span><span class="meta"><span class="code"></span><span class="name"></span></span>';
      li.querySelector(".code").textContent = c.name;
      li.querySelector(".name").textContent = c.code + " · " + (c.jurisdiction_path || "");
      li.addEventListener("click", () => selectCamera(c.camera_id, true));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectCamera(c.camera_id, true); } });
      list.appendChild(li);
    }
  }
  function selectCamera(id, pan) {
    const c = cameras.find((x) => x.camera_id === id); if (!c) return;
    const ll = camLatLng(c), m = markers.get(id);
    if (pan && ll && map) map.setView(ll, Math.max(map.getZoom(), 13), { animate: true });
    if (m) m.openPopup();
    openDrawer(c);
  }
  let lastFocusEl = null;
  function wireDrawer() { document.getElementById("cd-close").addEventListener("click", closeDrawer); }
  function openDrawer(c) {
    lastFocusEl = document.activeElement;
    document.getElementById("cd-title").textContent = c.name;
    const player = document.getElementById("cd-player"), msg = document.getElementById("cd-video-msg"), spin = document.getElementById("cd-video-spinner");
    const gid = gridId(c);
    if (gid && c.status === "active") attachHls(player, "/grid/" + gid + "/index.m3u8", msg, spin);
    else { detachHls(player, spin); msg.textContent = gid ? "" : t("noStream", "No stream for this camera"); }
    const body = document.getElementById("cd-body"); body.innerHTML = "";
    const ll = camLatLng(c);
    const fields = [
      ["", t("liveView","Live view")], ["Camera code", c.code], ["Type", c.camera_type], ["Status", c.status],
      ["District / area", c.jurisdiction_path || "—"], ["Operator", c.department || "—"],
      ["Location", ll ? ll[0].toFixed(5) + ", " + ll[1].toFixed(5) : "—"],
    ];
    for (const [k, v] of fields) { if (!k) continue; const dt = document.createElement("dt"); dt.textContent = k; const dd = document.createElement("dd"); dd.textContent = v; body.appendChild(dt); body.appendChild(dd); }
    document.getElementById("camera-detail").hidden = false;
    document.getElementById("cd-close").focus();
  }
  function closeDrawer() {
    detachHls(document.getElementById("cd-player"), document.getElementById("cd-video-spinner"));
    document.getElementById("camera-detail").hidden = true;
    if (lastFocusEl && document.contains(lastFocusEl)) lastFocusEl.focus();
  }

  // ---------------------------------------------------------------- video wall
  function wireWall() {
    document.getElementById("wall-btn").addEventListener("click", openWall);
    document.getElementById("wall-close").addEventListener("click", closeWall);
  }
  function openWall() {
    lastFocusEl = document.activeElement;
    const grid = document.getElementById("wall-grid"); grid.innerHTML = "";
    const live = cameras.filter((c) => c.status === "active" && gridId(c)).slice(0, 12);
    document.getElementById("wall-sub").textContent = live.length + " " + t("live","live");
    live.forEach((c) => {
      const tile = document.createElement("div"); tile.className = "wall-tile";
      tile.innerHTML = '<video class="wall-video" playsinline muted></video><div class="video-spinner" hidden aria-hidden="true"></div><div class="wall-label"></div><div class="wall-msg"></div>';
      tile.querySelector(".wall-label").textContent = c.name;
      grid.appendChild(tile);
      attachHls(tile.querySelector("video"), "/grid/" + gridId(c) + "/index.m3u8", tile.querySelector(".wall-msg"), tile.querySelector(".video-spinner"));
    });
    if (!live.length) grid.innerHTML = '<div class="empty-note">No live grid cameras to show.</div>';
    document.getElementById("wall-overlay").hidden = false;
    document.getElementById("wall-close").focus();
  }
  function closeWall() {
    document.querySelectorAll("#wall-grid .wall-tile").forEach((tile) => detachHls(tile.querySelector("video"), tile.querySelector(".video-spinner")));
    document.getElementById("wall-grid").innerHTML = "";
    document.getElementById("wall-overlay").hidden = true;
    if (lastFocusEl && document.contains(lastFocusEl)) lastFocusEl.focus();
  }

  // ---------------------------------------------------------------- tabs / filters
  function wireTabs() {
    const tabs = document.querySelectorAll("#panel-tabs .tab");
    tabs.forEach((tab) => tab.addEventListener("click", () => {
      tabs.forEach((x) => x.classList.remove("tab-active")); tab.classList.add("tab-active");
      document.querySelectorAll(".tab-body").forEach((b) => b.classList.remove("tab-body-active"));
      document.getElementById("tab-" + tab.dataset.tab).classList.add("tab-body-active");
    }));
  }
  function wireAlertFilters() {
    document.querySelectorAll("#alert-filters .fchip").forEach((chip) => chip.addEventListener("click", () => {
      document.querySelectorAll("#alert-filters .fchip").forEach((c) => { c.classList.remove("fchip-active"); c.setAttribute("aria-pressed", "false"); });
      chip.classList.add("fchip-active"); chip.setAttribute("aria-pressed", "true");
      alertFilter = chip.dataset.filter; renderAlerts();
    }));
  }

  // ---------------------------------------------------------------- auth
  // The console no longer has its own login form — signing in happens on the
  // one styled page, login.html (opened from "Sign in" in the platform nav).
  // Here we only wire the signed-in user chip's sign-out. An unauthenticated
  // console just runs in demo mode.
  function wireAuth() {
    const chip = document.getElementById("user-chip");
    if (chip) chip.addEventListener("click", async () => {
      if (confirm("Sign out?")) { await window.SentinelAPI.logout(); location.reload(); }
    });
  }
  function updateUserChip(u) {
    const chip = document.getElementById("user-chip");
    if (!u) { chip.hidden = true; return; }
    chip.hidden = false;
    document.getElementById("user-name").textContent = u.username;
    document.getElementById("user-avatar").textContent = u.username.slice(0, 2).toUpperCase();
  }
  function updateDataMode() {
    const badge = document.getElementById("data-mode"), dot = document.getElementById("tel-dot"), mode = document.getElementById("tel-mode");
    if (window.SentinelAPI.usingDemoData) { badge.className = "badge badge-demo"; badge.innerHTML = '<span class="badge-dot"></span>DEMO DATA'; dot.className = "tel-dot"; mode.textContent = "Demo data"; }
    else { badge.className = "badge badge-live"; badge.innerHTML = '<span class="badge-dot"></span>LIVE'; dot.className = "tel-dot live"; mode.textContent = "Live"; }
  }

  // ---------------------------------------------------------------- safety / sos
  function wireSafety() {
    document.getElementById("safety-toggle").addEventListener("click", async () => {
      safetyMode = !safetyMode;
      document.getElementById("safety-toggle").classList.toggle("on", safetyMode);
      plotCameras(); renderAlerts();
      toast(safetyMode ? "Women’s Safety mode on — SOS and safety alerts prioritised, curated risk zones shown on the map." : "Women’s Safety mode off.", true);
      if (safetyMode) document.querySelector('.tab[data-tab="alerts"]').click();
      if (map && safetyZoneLayer) {
        if (safetyMode) {
          if (!safetyZonesLoaded) { safetyZonesLoaded = true; await loadSafetyZones(); }
          map.addLayer(safetyZoneLayer);
        } else {
          map.removeLayer(safetyZoneLayer);
        }
      }
    });
  }
  function toast(m, s) { const el = document.getElementById("map-toast"); el.textContent = m; el.className = "show" + (s ? " safety" : ""); clearTimeout(toast._t); toast._t = setTimeout(() => { el.className = ""; }, 4200); }
  function wireSos() {
    const modal = document.getElementById("sos-modal");
    const closeSos = () => { modal.hidden = true; if (lastFocusEl && document.contains(lastFocusEl)) lastFocusEl.focus(); };
    document.getElementById("sos-btn").addEventListener("click", () => { lastFocusEl = document.activeElement; modal.hidden = false; document.getElementById("sos-cancel").focus(); });
    document.getElementById("sos-cancel").addEventListener("click", closeSos);
    document.getElementById("sos-confirm").addEventListener("click", async () => {
      const b = document.getElementById("sos-confirm"); b.disabled = true; b.textContent = "…";
      try {
        const c = cameras[0], ll = c ? camLatLng(c) : null;
        await window.SentinelAPI.reportSos({ channel: "operator", camera_id: c ? c.camera_id : null, lat: ll ? ll[0] : null, lon: ll ? ll[1] : null, notes: "Raised from the control room." });
        toast("SOS sent — a critical alert has been opened.", true); closeSos(); await loadAlerts();
      } catch (e) { toast("SOS failed: " + e.message, true); }
      finally { b.disabled = false; b.textContent = t("sendSos","Send SOS"); }
    });
  }

  // ---------------------------------------------------------------- data & privacy
  function wirePrivacy() {
    const modal = document.getElementById("privacy-modal");
    document.getElementById("privacy-btn").addEventListener("click", () => {
      lastFocusEl = document.activeElement; modal.hidden = false; document.getElementById("privacy-close").focus();
    });
    document.getElementById("privacy-close").addEventListener("click", () => {
      modal.hidden = true; if (lastFocusEl && document.contains(lastFocusEl)) lastFocusEl.focus();
    });
  }

  // Escape closes whichever overlay/modal/drawer is currently open, and
  // returns focus to whatever opened it — keyboard-only operators shouldn't
  // need a mouse to back out of the video wall, a camera drawer, or a modal.
  function wireOverlayEscape() {
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const wall = document.getElementById("wall-overlay"), drawer = document.getElementById("camera-detail");
      const sos = document.getElementById("sos-modal"), privacy = document.getElementById("privacy-modal");
      if (privacy && !privacy.hidden) document.getElementById("privacy-close").click();
      else if (sos && !sos.hidden) document.getElementById("sos-cancel").click();
      else if (wall && !wall.hidden) closeWall();
      else if (drawer && !drawer.hidden) closeDrawer();
    });
  }

  // ---------------------------------------------------------------- alert sound
  function beep(critical) {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      o.frequency.value = critical ? 880 : 620; o.type = "sine";
      g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.4);
      o.start(); o.stop(audioCtx.currentTime + 0.42);
      if (critical) setTimeout(() => beep(false), 260);
    } catch (e) {}
  }

  // ---------------------------------------------------------------- live feed
  const MAX_FEED = 60;
  function handleMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "event") return handleEvent(msg.event);
    if (msg.type === "alert.created" || msg.type === "alert.updated") return handleAlert(msg.alert);
  }
  function handleEvent(evt) {
    if (evt.kind === "track_start" && evt.track_id) liveTracks.add(evt.track_id);
    if (evt.kind === "track_end" && evt.track_id) liveTracks.delete(evt.track_id);
    appendActivity(evt); updateStats();
  }
  function appendActivity(evt) {
    const list = document.getElementById("events-list"); const em = list.querySelector(".empty-note"); if (em) em.remove();
    const c = cameras.find((x) => x.camera_id === evt.camera_id);
    const li = document.createElement("li"); li.className = "feed-item sev-routine";
    li.innerHTML = '<div class="feed-head"><span class="feed-kind"></span><span class="feed-time"></span></div><div class="feed-cam"></div><div class="feed-detail"></div>';
    li.querySelector(".feed-kind").textContent = (evt.kind || "").replace(/_/g, " ");
    li.querySelector(".feed-time").textContent = new Date(evt.ts).toLocaleTimeString("en-GB");
    li.querySelector(".feed-cam").textContent = c ? c.name : "camera " + evt.camera_id;
    li.querySelector(".feed-detail").textContent = summarise(evt);
    list.insertBefore(li, list.firstChild);
    while (list.children.length > MAX_FEED) list.removeChild(list.lastChild);
  }
  function summarise(e) {
    const p = e.payload || {};
    if (e.kind === "anpr") return "plate " + (p.plate_text || "?") + " (" + fmt(p.confidence) + ")";
    if (e.kind.startsWith("track")) return (p.class_label || "") + (p.confidence != null ? " · " + fmt(p.confidence) : "");
    if (e.kind === "dwell") return (p.class_label || "") + " waiting " + (p.dwell_seconds || "?") + "s";
    if (e.kind === "proximity") return "two people close for " + (p.seconds || "?") + "s";
    if (e.kind === "stream_gap") return "camera dropped for " + (p.gap_seconds || "?") + "s";
    try { return JSON.stringify(p); } catch (x) { return ""; }
  }
  function fmt(v) { return typeof v === "number" ? Math.round(v*100) + "%" : "?"; }

  // ---------------------------------------------------------------- alerts
  async function loadAlerts() {
    const a = await window.SentinelAPI.fetchAlerts();
    if (a) { alertsById.clear(); seenAlertIds = new Set(a.map((x) => x.id)); for (const x of a) alertsById.set(x.id, x); }
    refreshAll();
  }
  function handleAlert(a) {
    if (!a || a.id == null) return;
    const isNew = !seenAlertIds.has(a.id) && (a.status === "open");
    seenAlertIds.add(a.id); alertsById.set(a.id, a); refreshAll();
    if (isNew) beep(a.severity === "critical" || a.kind === "sos");
    if (a.kind === "sos" || a.kind === "women_safety_risk") {
      toast((a.kind === "sos" ? "SOS: " : "Women’s safety alert: ") + a.summary, true);
      document.querySelector('.tab[data-tab="alerts"]').click();
    }
    // Auto-relay: a genuinely serious, newly-opened incident with a known
    // camera location is routed to the nearest PCR immediately, before any
    // operator clicks anything — that automatic first hop is the point.
    if (isNew && (a.severity === "critical" || a.kind === "sos" || a.kind === "women_safety_risk")) {
      const c = cameras.find((x) => x.camera_id === a.camera_id);
      const ll = c && camLatLng(c);
      if (ll) { const rec = runRelay(ll, (c ? c.name : "camera") + " · " + (a.kind || "").replace(/_/g, " "), kindToResponder(a.kind), true); if (rec) a.__dispatch = rec; }
    }
  }
  function refreshAll() { renderAlerts(); plotCameras(); renderCameraList(); updateStats(); }
  function matchesFilter(a) {
    if (alertFilter === "all") return true;
    if (alertFilter === "watchlist") return ANPR_KINDS.has(a.kind);
    if (alertFilter === "safety") return a.kind === "sos" || a.kind === "women_safety_risk";
    if (alertFilter === "other") return !ANPR_KINDS.has(a.kind) && a.kind !== "sos" && a.kind !== "women_safety_risk";
    return true;
  }
  function renderAlerts() {
    const list = document.getElementById("alerts-list"); list.innerHTML = "";
    let open = Array.from(alertsById.values()).filter((a) => (a.status === "open" || a.status === "acknowledged"));
    openAlertCount = open.length;
    const badge = document.getElementById("alerts-count"); badge.textContent = String(openAlertCount); badge.classList.toggle("zero", openAlertCount === 0);
    open = open.filter(matchesFilter);
    if (safetyMode) open.sort((a, b) => { const aw=(a.kind==="sos"||a.kind==="women_safety_risk")?0:1, bw=(b.kind==="sos"||b.kind==="women_safety_risk")?0:1; return aw-bw || (SEV_RANK[a.severity]??9)-(SEV_RANK[b.severity]??9); });
    else open.sort((a, b) => (SEV_RANK[a.severity]??9)-(SEV_RANK[b.severity]??9) || new Date(b.opened_at)-new Date(a.opened_at));
    if (!open.length) {
      list.innerHTML = window.SentinelAPI.getSession() ? '<li class="empty-note">No open alerts.</li>' : '<li class="empty-note">Sign in to see live alerts.<br>Activity tab still shows the demo feed.</li>';
      return;
    }
    for (const a of open) list.appendChild(alertItem(a));
  }
  function alertItem(a) {
    const c = cameras.find((x) => x.camera_id === a.camera_id);
    const li = document.createElement("li");
    li.className = "feed-item sev-" + a.severity + (a.kind === "sos" ? " kind-sos" : "");
    li.innerHTML = '<div class="feed-head"><span class="feed-kind"></span><span class="status-pill st-' + a.status + '"></span></div><div class="feed-cam"></div><div class="feed-detail"></div><div class="feed-actions"></div>';
    li.querySelector(".feed-kind").textContent = (a.kind || "").replace(/_/g, " ");
    li.querySelector(".status-pill").textContent = a.status.replace(/_/g, " ");
    li.querySelector(".feed-cam").textContent = (c ? c.name : (a.camera_id ? "camera " + a.camera_id : "no camera")) + (a.case_reference ? " · " + a.case_reference : "");
    li.querySelector(".feed-detail").textContent = a.summary;
    if (a.__dispatch) {
      const note = document.createElement("div"); note.className = "dispatch-note";
      note.textContent = "→ " + a.__dispatch.unit.name + " · " + a.__dispatch.distKm.toFixed(1) + " km · ~" + a.__dispatch.etaMin + " min → control room";
      li.querySelector(".feed-detail").after(note);
    }
    const act = li.querySelector(".feed-actions");
    if (c && camLatLng(c)) act.appendChild(mini(t("showOnMap","Show on map"), "ghost", () => selectCamera(c.camera_id, true)));
    if (ANPR_KINDS.has(a.kind) && a.detail && a.detail.entry_id) act.appendChild(mini(t("trackRoute","Track route"), "", () => { document.querySelector('.tab[data-tab="watchlist"]').click(); drawTrail(a.detail.entry_id, a.detail.plate_text || a.summary); }));
    if (a.status === "open") act.appendChild(mini(t("dispatch","Dispatch"), "", (b) => dispatch(a, b)));
    if (a.status === "open" || a.status === "acknowledged") act.appendChild(mini(t("close","Close"), "ghost", (b) => runAction(b, () => window.SentinelAPI.closeAlert(a.id))));
    if (!act.children.length) act.remove();
    return li;
  }
  async function dispatch(a, btn) {
    // Compute and show the nearest-PCR routing first (the differentiator),
    // then record the acknowledgement on the backend against that unit.
    const c = cameras.find((x) => x.camera_id === a.camera_id);
    const ll = c && camLatLng(c);
    const rec = ll ? runRelay(ll, (c ? c.name : "camera") + " · " + (a.kind || "").replace(/_/g, " "), kindToResponder(a.kind), false) : null;
    btn.disabled = true; btn.textContent = "…";
    try {
      const up = await window.SentinelAPI.acknowledgeAlert(a.id); handleAlert(up);
      if (rec) toast("Relayed to nearest " + RESPONDER[rec.kind].label + " " + rec.unit.name + " (" + rec.distKm.toFixed(1) + " km · ~" + rec.etaMin + " min) → control room.", true);
      else toast("Acknowledged.");
    } catch (e) { toast("Dispatch failed: " + e.message); btn.disabled = false; }
  }
  function mini(label, cls, fn) { const b = document.createElement("button"); b.className = "btn-mini" + (cls ? " " + cls : ""); b.textContent = label; b.addEventListener("click", () => fn(b)); return b; }
  async function runAction(btn, fn) { btn.disabled = true; try { handleAlert(await fn()); } catch (e) { toast("Action failed: " + e.message); btn.disabled = false; } }

  // ---------------------------------------------------------------- watchlist + trail
  function wireWatchlist() {
    const input = document.getElementById("wl-search-input"), run = () => searchWatchlist(input.value.trim());
    document.getElementById("wl-search-btn").addEventListener("click", run);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  }
  async function searchWatchlist(q) {
    const list = document.getElementById("wl-list"), hint = document.getElementById("wl-hint");
    if (!window.SentinelAPI.getSession()) { hint.textContent = "Sign in to search the watchlist."; return; }
    hint.textContent = ""; list.innerHTML = '<li class="empty-note">…</li>';
    const entries = await window.SentinelAPI.searchWatchlist(q); list.innerHTML = "";
    if (!entries || !entries.length) { list.innerHTML = '<li class="empty-note">No matching entries.</li>'; return; }
    for (const e of entries) list.appendChild(wlItem(e));
  }
  function wlItem(entry) {
    const li = document.createElement("li"); li.className = "wl-item";
    const label = '<span class="wl-plate">' + esc(entry.plate_number || entry.entry_type.replace(/_/g, " ")) + '</span>';
    li.innerHTML = '<div class="wl-head">' + label + '<span class="wl-risk risk-' + entry.risk_level + '">' + esc(entry.risk_level) + '</span></div><div class="wl-label"></div><div class="wl-case"></div>';
    li.querySelector(".wl-label").textContent = entry.label || "";
    li.querySelector(".wl-case").textContent = entry.case_reference ? "Ref: " + entry.case_reference : entry.status;
    const btn = mini(t("trackRouteMap","Track route on map"), "", () => drawTrail(entry.id, entry.plate_number || entry.label, li));
    btn.classList.add("wl-route-btn"); li.appendChild(btn);
    return li;
  }
  async function drawTrail(entryId, label, li) {
    try {
      const trail = await window.SentinelAPI.fetchTrail(entryId);
      if (!trail || !trail.length) { toast("No sightings recorded yet for this vehicle."); return; }
      if (trailLayer) { map.removeLayer(trailLayer); trailLayer = null; }
      trailLayer = L.layerGroup().addTo(map);
      const pts = [];
      trail.forEach((m, i) => {
        const c = cameras.find((x) => x.camera_id === m.camera_id), ll = c ? camLatLng(c) : null; if (!ll) return;
        pts.push(ll);
        const last = i === trail.length - 1;
        const icon = L.divIcon({ className: "", html: '<div class="trail-num' + (last ? " last" : "") + '">' + (i+1) + '</div>', iconSize: [24,24], iconAnchor: [12,12] });
        L.marker(ll, { icon }).addTo(trailLayer).bindPopup('<b>' + esc(label || "vehicle") + '</b><div class="popup-sub">' + (c ? esc(c.name) : "camera") + '</div><div class="popup-sub">' + new Date(m.matched_at).toLocaleString("en-GB") + '</div>');
      });
      if (pts.length >= 2) L.polyline(pts, { color: "#3b82f6", weight: 3, opacity: 0.9, dashArray: "6 6" }).addTo(trailLayer);
      if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.3));
      const kin = kinematics(trail);
      toast("Route drawn — " + trail.length + " sightings" + (kin.speed != null ? ", ~" + Math.round(kin.speed) + " km/h " + compass(kin.heading) : "") + ".");
      if (li) readout(li, trail, kin);
    } catch (e) { toast("Could not load route: " + e.message); }
  }
  function kinematics(trail) {
    const cams = new Set(trail.filter((m) => m.camera_id != null).map((m) => m.camera_id)); let speed = null, heading = null;
    for (let i = trail.length - 1; i > 0; i--) {
      const b = trail[i], a = trail[i-1]; if (b.camera_id == null || a.camera_id == null || b.camera_id === a.camera_id) continue;
      const cb = cameras.find((x) => x.camera_id === b.camera_id), ca = cameras.find((x) => x.camera_id === a.camera_id); if (!cb || !ca) continue;
      const hrs = (new Date(b.matched_at) - new Date(a.matched_at)) / 3600000;
      if (hrs > 0) speed = hav(camLatLng(ca), camLatLng(cb)) / hrs;
      heading = brg(camLatLng(ca), camLatLng(cb)); break;
    }
    return { hops: cams.size, speed, heading };
  }
  function readout(li, trail, kin) {
    li.querySelectorAll(".wl-readout").forEach((n) => n.remove());
    const box = document.createElement("div"); box.className = "wl-readout";
    const rows = [["Sightings", String(trail.length)], ["Cameras (hops)", String(kin.hops)], ["Est. speed", kin.speed != null ? Math.round(kin.speed) + " km/h" : "—"], ["Est. heading", kin.heading != null ? Math.round(kin.heading) + "° " + compass(kin.heading) : "—"]];
    box.innerHTML = rows.map(() => '<div class="r-row"><span></span><b></b></div>').join("");
    box.querySelectorAll(".r-row").forEach((r, i) => { r.querySelector("span").textContent = rows[i][0]; r.querySelector("b").textContent = rows[i][1]; });
    li.appendChild(box);
  }

  // ---------------------------------------------------------------- stats
  function updateStats() {
    document.getElementById("tel-cameras").textContent = String(cameras.length);
    document.getElementById("tel-live").textContent = String(cameras.filter((c) => c.status === "active").length);
    document.getElementById("tel-open").textContent = String(openAlertCount);
  }
  function initClock() { const el = document.getElementById("clock"); const tk = () => { el.textContent = new Date().toLocaleTimeString("en-GB"); }; tk(); setInterval(tk, 1000); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])); }
})();
