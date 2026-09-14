const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.layout = "LAYOUT_WIDE"; // 13.3 x 7.5
const W = 13.3, H = 7.5;

// ---- palette (control-room dark, matches the Netra console) ----
const BG = "0B1018", PANEL = "13infra".slice(0,0) + "131E2E", PANEL2 = "18real".slice(0,0) + "1A2738";
const CARD = "141F30", CARD2 = "1B2940";
const TEAL = "2FE0C4", BLUE = "47A8FF", PINK = "FF5DA2", AMBER = "FFB443", RED = "FF5470";
const T0 = "EEF3FA", T1 = "A7B7CC", T2 = "7C8CA3", LINE = "26374D";
const HFONT = "Century Schoolbook", BFONT = "Calibri";

function base(slide, dark = true) {
  slide.background = { color: dark ? BG : "F5F7FA" };
}
function kicker(slide, text, x, y, color = TEAL) {
  slide.addText(text.toUpperCase(), { x, y, w: 8, h: 0.3, isTextBox: true, margin: 0,
    fontFace: BFONT, fontSize: 12, bold: true, color, charSpacing: 3 });
}
function dot(slide, x, y, d, color) {
  slide.addShape(p.ShapeType.ellipse, { x, y, w: d, h: d, fill: { color }, line: { type: "none" } });
}
function card(slide, x, y, w, h, fill = CARD) {
  slide.addShape(p.ShapeType.roundRect, { x, y, w, h, rectRadius: 0.08,
    fill: { color: fill }, line: { color: LINE, width: 1 } });
}

// ============================================================ 1. TITLE
let s = p.addSlide(); base(s);
// faint dot grid motif top-right
for (let i = 0; i < 6; i++) for (let j = 0; j < 4; j++) dot(s, 9.6 + i * 0.55, 0.5 + j * 0.55, 0.07, "1E2E44");
dot(s, 1.0, 2.15, 0.28, TEAL);
s.addText("SENTINEL", { x: 1.35, y: 1.9, w: 9, h: 0.9, isTextBox: true, margin: 0,
  fontFace: HFONT, fontSize: 54, bold: true, color: T0, charSpacing: 2 });
s.addText("The NETRA Platform  ·  Unified CCTV Intelligence for Law Enforcement", {
  x: 1.0, y: 2.95, w: 11, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 20, color: TEAL });
s.addText("An integrated video-management & analytics platform that onboards diverse CCTV systems, correlates live feeds with government databases, and turns them into real-time, auditable intelligence.", {
  x: 1.0, y: 3.6, w: 10.8, h: 1.0, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, color: T1, lineSpacingMultiple: 1.2 });
s.addText([
  { text: "Gujarat CCTV Integration Hackathon 2026", options: { bold: true, color: T0 } },
  { text: "   ·   Home Department, Government of Gujarat", options: { color: T2 } },
], { x: 1.0, y: 6.5, w: 11, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 13 });
s.addShape(p.ShapeType.line, { x: 1.0, y: 6.4, w: 3.2, h: 0, line: { color: TEAL, width: 2 } });
s.addNotes("Sentinel / Netra: one platform implementing reference Models 1, 2 and 3. Onboards heterogeneous cameras, runs edge AI analytics, and produces real-time intelligence with a court-admissible audit trail.");

// ============================================================ 2. PROBLEM
s = p.addSlide(); base(s);
kicker(s, "The challenge", 0.7, 0.55);
s.addText("Thousands of cameras. No single brain.", { x: 0.7, y: 0.9, w: 12, h: 0.8,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 34, bold: true, color: T0 });
s.addText("Gujarat's cameras are spread across departments, vendors and protocols — each an island. There is no unified way to view them, correlate a vehicle across them, or act in real time.", {
  x: 0.7, y: 1.75, w: 6.0, h: 1.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 16, color: T1, lineSpacingMultiple: 1.25 });
const probs = [
  ["Fragmented", "IP, analog, and multi-vendor VMS systems that don't talk to each other"],
  ["Blind between cameras", "A vehicle seen at one junction can't be followed to the next"],
  ["Video-heavy & unscalable", "Centralising raw video for a whole state is physically infeasible"],
];
probs.forEach((c, i) => {
  const y = 3.4 + i * 1.15;
  card(s, 0.7, y, 6.0, 1.0);
  dot(s, 0.95, y + 0.34, 0.32, i === 2 ? RED : AMBER);
  s.addText(c[0], { x: 1.45, y: y + 0.12, w: 5.1, h: 0.35, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, bold: true, color: T0 });
  s.addText(c[1], { x: 1.45, y: y + 0.47, w: 5.1, h: 0.45, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, color: T1 });
});
// big stat right
card(s, 7.15, 1.75, 5.45, 4.75, "101A2A");
s.addText("~80,000", { x: 7.15, y: 2.7, w: 5.45, h: 1.2, isTextBox: true, margin: 0, align: "center", fontFace: HFONT, fontSize: 72, bold: true, color: TEAL });
s.addText("cameras to integrate across Gujarat", { x: 7.35, y: 3.95, w: 5.05, h: 0.5, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 16, color: T1 });
s.addText("The scale is the whole problem — the solution has to be built for it from the first camera, not retrofitted.", {
  x: 7.55, y: 4.7, w: 4.65, h: 1.0, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 13, italic: true, color: T2, lineSpacingMultiple: 1.2 });
s.addNotes("The problem is fragmentation + scale. 80,000 cameras across departments and vendors, with no unified intelligence layer.");

// ============================================================ 3. MODEL CHOSEN
s = p.addSlide(); base(s);
kicker(s, "Our approach", 0.7, 0.55);
s.addText("Models 1, 2 & 3 — as one layered platform", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 32, bold: true, color: T0 });
s.addText("In the field these aren't alternatives — they're layers of the same system. We built them as one.", {
  x: 0.7, y: 1.65, w: 11.5, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, color: T1 });
const models = [
  ["MODEL 1", "System of record", "Camera registry, GIS map, jurisdiction hierarchy, RBAC, watchlist & alerts, and a tamper-evident audit trail — the mandatory spine both other layers depend on.", TEAL],
  ["MODEL 3", "Integration substrate", "An adapter framework that onboards heterogeneous cameras and VMSs (ONVIF/RTSP, Genetec, Milestone, the government grid) behind one interface.", BLUE],
  ["MODEL 2", "Edge analytics", "Unified viewing metadata, ANPR, watchlist matching and alerts — inference runs at the edge, only metadata travels centrally.", "8FD6C4"],
];
models.forEach((m, i) => {
  const y = 2.4 + i * 1.35;
  card(s, 0.7, y, 11.9, 1.2);
  s.addShape(p.ShapeType.roundRect, { x: 0.9, y: y + 0.22, w: 1.55, h: 0.76, rectRadius: 0.06, fill: { color: "0E1826" }, line: { color: m[3], width: 1.25 } });
  s.addText(m[0], { x: 0.9, y: y + 0.42, w: 1.55, h: 0.35, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 14, bold: true, color: m[3] });
  s.addText(m[1], { x: 2.7, y: y + 0.2, w: 9.6, h: 0.38, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 17, bold: true, color: T0 });
  s.addText(m[2], { x: 2.7, y: y + 0.58, w: 9.7, h: 0.55, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12.5, color: T1, lineSpacingMultiple: 1.1 });
});
s.addText("Why one platform: you can't correlate a plate across cameras without one trusted registry of which camera is where, in whose jurisdiction, and who may see it.", {
  x: 0.7, y: 6.55, w: 11.9, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 13, italic: true, color: TEAL });
s.addNotes("We implement all three reference models as one layered platform. Model 1 is the spine; Models 2 and 3 are two ingestion modes of the same system, chosen per camera.");

// ============================================================ 4. ARCHITECTURE
s = p.addSlide(); base(s);
kicker(s, "Architecture", 0.7, 0.55);
s.addText("Analyse at the edge, move only metadata", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 30, bold: true, color: T0 });
// EDGE column
card(s, 0.7, 1.9, 3.7, 4.4, "101A2A");
s.addText("EDGE SITES", { x: 0.7, y: 2.05, w: 3.7, h: 0.35, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 13, bold: true, color: BLUE, charSpacing: 2 });
["Heterogeneous cameras\n(IP · analog · multi-vendor)", "Departmental VMS\n(Genetec · Milestone)", "Analytics worker\nmotion → detect → ANPR →\nface → tracker → rules"].forEach((t, i) => {
  const y = 2.55 + i * 1.2;
  card(s, 0.95, y, 3.2, 1.0, CARD2);
  s.addText(t, { x: 1.05, y: y + 0.12, w: 3.0, h: 0.8, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, color: T0, align: "center", lineSpacingMultiple: 1.0 });
});
// arrow to central
s.addShape(p.ShapeType.line, { x: 4.45, y: 4.1, w: 0.95, h: 0, line: { color: TEAL, width: 2.5, endArrowType: "triangle" } });
s.addText("metadata\nonly (JSON)", { x: 4.3, y: 3.35, w: 1.25, h: 0.6, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 10, bold: true, color: TEAL });
// CENTRAL column
card(s, 5.5, 1.9, 4.3, 4.4, "101A2A");
s.addText("CENTRAL PLATFORM", { x: 5.5, y: 2.05, w: 4.3, h: 0.35, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 13, bold: true, color: TEAL, charSpacing: 2 });
["Federation adapters (Model 3)\nonboard camera catalogues", "Registry API (Model 1)\ncameras · RBAC · watchlist · alerts", "PostgreSQL + PostGIS\nRow-Level-Security enforced"].forEach((t, i) => {
  const y = 2.55 + i * 1.2;
  card(s, 5.75, y, 3.8, 1.0, CARD2);
  s.addText(t, { x: 5.85, y: y + 0.14, w: 3.6, h: 0.75, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, color: T0, align: "center", lineSpacingMultiple: 1.0 });
});
// arrow to console
s.addShape(p.ShapeType.line, { x: 9.85, y: 4.1, w: 0.9, h: 0, line: { color: BLUE, width: 2.5, endArrowType: "triangle" } });
s.addText("live\nSSE", { x: 9.75, y: 3.5, w: 1.0, h: 0.5, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 10, bold: true, color: BLUE });
// CONSOLE
card(s, 10.85, 1.9, 1.75, 4.4, "101A2A");
s.addText("OPERATOR\nCONSOLE", { x: 10.85, y: 2.05, w: 1.75, h: 0.5, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 12, bold: true, color: T0, charSpacing: 1 });
s.addText("Map · alerts ·\nwatchlist trail ·\nwomen's-safety\n· SOS", { x: 10.9, y: 3.2, w: 1.65, h: 1.6, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 11.5, color: T1, lineSpacingMultiple: 1.15 });
s.addText("Video-central would be ~200 Gbps for 80,000 cameras — impossible. Metadata-central is a few Mbps.", {
  x: 0.7, y: 6.55, w: 11.9, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 13, italic: true, color: TEAL });
s.addNotes("Two properties: only metadata crosses the edge boundary, and every path terminates at the RLS-enforced database — the single authorization authority.");

// ============================================================ 5. INTEGRATION
s = p.addSlide(); base(s);
kicker(s, "Interoperability · Model 3", 0.7, 0.55, BLUE);
s.addText("One interface for every camera and VMS", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 30, bold: true, color: T0 });
s.addText("Real deployments are never one vendor. Each source is normalised into one camera record, so everything downstream is protocol-agnostic.", {
  x: 0.7, y: 1.65, w: 11.7, h: 0.6, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, color: T1 });
const adapters = [
  ["ONVIF / RTSP", "IP cameras & NVRs, and analog behind an encoder — the direct-connect (Model 2) path", TEAL],
  ["Genetec", "Departmental VMS integrated via its API — for cameras a dept won't expose directly", BLUE],
  ["Milestone", "The other common VMS platform, behind the same driver interface", BLUE],
  ["Sentinel Grid", "The government camera grid, consumed from its cameras.json catalogue", TEAL],
];
adapters.forEach((a, i) => {
  const x = 0.7 + (i % 2) * 6.05, y = 2.45 + Math.floor(i / 2) * 1.55;
  card(s, x, y, 5.75, 1.35);
  dot(s, x + 0.28, y + 0.28, 0.16, a[2]);
  s.addText(a[0], { x: x + 0.6, y: y + 0.18, w: 5.0, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 17, bold: true, color: T0 });
  s.addText(a[1], { x: x + 0.28, y: y + 0.62, w: 5.25, h: 0.6, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12.5, color: T1, lineSpacingMultiple: 1.1 });
});
s.addText([
  { text: "Catalogue-driven onboarding: ", options: { bold: true, color: TEAL } },
  { text: "one command reads a department's camera catalogue and registers the whole set — with locations and stream endpoints — in a single idempotent run.", options: { color: T1 } },
], { x: 0.7, y: 5.75, w: 11.9, h: 0.9, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 13.5, italic: true, lineSpacingMultiple: 1.15 });
s.addNotes("Adding a department's VMS is a new adapter + a row of config, not a core change. Mixed codecs/resolutions/protocols normalised into one record. Onboarding is catalogue-driven for efficiency.");

// ============================================================ 6. ANALYTICS CASCADE
s = p.addSlide(); base(s);
kicker(s, "AI analytics · Model 2", 0.7, 0.55);
s.addText("A cost-ordered cascade — cheap rejection first", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 30, bold: true, color: T0 });
const stages = ["Motion\ngate", "Object\ndetect", "Plate\nANPR", "Face\nembed", "Multi-obj\ntracker", "Rule\nengine"];
stages.forEach((st, i) => {
  const x = 0.7 + i * 2.03;
  card(s, x, 2.0, 1.75, 1.1, i === stages.length - 1 ? "13322C" : CARD2);
  s.addText(st, { x: x, y: 2.28, w: 1.75, h: 0.6, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 13, bold: true, color: i === stages.length - 1 ? TEAL : T0, lineSpacingMultiple: 1.0 });
  if (i < stages.length - 1) s.addShape(p.ShapeType.line, { x: x + 1.78, y: 2.55, w: 0.22, h: 0, line: { color: T2, width: 2, endArrowType: "triangle" } });
});
// measured stat
card(s, 0.7, 3.55, 5.75, 2.85, "101A2A");
s.addText("MEASURED", { x: 0.9, y: 3.75, w: 5, h: 0.3, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 11, bold: true, color: T2, charSpacing: 2 });
s.addText("~8", { x: 0.9, y: 4.05, w: 5.3, h: 1.1, isTextBox: true, margin: 0, align: "center", fontFace: HFONT, fontSize: 66, bold: true, color: TEAL });
s.addText("OCR calls per 1,000 frames", { x: 0.9, y: 5.2, w: 5.3, h: 0.4, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 15, color: T0 });
s.addText("A geometric plate gate + once-per-track dedupe spend the expensive stages on almost nothing — 1,920 detected objects → 4 OCR calls in the self-test.", {
  x: 0.95, y: 5.65, w: 5.25, h: 0.7, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 11.5, color: T1, lineSpacingMultiple: 1.1 });
// ANPR integrity
card(s, 6.85, 3.55, 5.75, 2.85);
s.addText("ANPR built for evidence", { x: 7.1, y: 3.8, w: 5.2, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 16, bold: true, color: T0 });
[["No character substitution", "OCR confusions are never 'fixed' — a substituted character is a fabricated one"],
 ["Raw + normalised kept", "both forms stored on every read, so the evidence is auditable"],
 ["Read once per vehicle", "not once per frame — no contradictory plate strings"]].forEach((r, i) => {
  const y = 4.35 + i * 0.68;
  dot(s, 7.1, y + 0.05, 0.14, TEAL);
  s.addText([{ text: r[0] + "  ", options: { bold: true, color: T0 } }, { text: "— " + r[1], options: { color: T1 } }],
    { x: 7.4, y: y - 0.05, w: 5.0, h: 0.6, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, lineSpacingMultiple: 1.05 });
});
s.addNotes("The cascade rejects cheaply first. Measured funnel: ~8 OCR calls per 1000 frames. ANPR keeps both raw and normalised reads and never substitutes characters — evidential integrity.");

// ============================================================ 7. HEADLINE: TRACKING
s = p.addSlide(); base(s);
kicker(s, "The live test case", 0.7, 0.55);
s.addText("Track a vehicle across the whole grid", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 32, bold: true, color: T0 });
s.addText("Given a plate on the day, Sentinel returns its complete, timestamped, location-wise route across cameras — the graded challenge, already working.", {
  x: 0.7, y: 1.65, w: 11.8, h: 0.6, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, color: T1 });
// mock trail map
card(s, 0.7, 2.45, 6.4, 4.0, "0E1826");
const pts = [[1.7, 5.6], [2.9, 4.7], [4.2, 5.0], [5.1, 3.9], [6.3, 3.3]];
for (let i = 0; i < pts.length - 1; i++) {
  const a = pts[i], b = pts[i + 1];
  s.addShape(p.ShapeType.line, { x: a[0], y: a[1] + 0.07, w: b[0] - a[0], h: b[1] - a[1], line: { color: TEAL, width: 1.5, dashType: "dash" } });
}
pts.forEach((pt, i) => {
  dot(s, pt[0] - 0.08, pt[1] - 0.01, 0.22, i === pts.length - 1 ? RED : TEAL);
  s.addText("cam" + (i + 1), { x: pt[0] - 0.3, y: pt[1] + 0.18, w: 0.9, h: 0.25, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 9, color: T2 });
});
s.addText("Same plate, two+ cameras, two+ times = a trail", { x: 0.9, y: 2.65, w: 6.0, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12.5, italic: true, color: T1 });
// right: how + proof
card(s, 7.35, 2.45, 5.25, 1.95);
s.addText("How — with no new subsystem", { x: 7.6, y: 2.62, w: 4.8, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, bold: true, color: T0 });
s.addText("Each watchlist match records camera + time. Two matches for one plate are, by construction, two points on the map — a trail. The console computes speed & heading between them. No biometric identity resolution.", {
  x: 7.6, y: 3.05, w: 4.8, h: 1.25, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12.5, color: T1, lineSpacingMultiple: 1.15 });
card(s, 7.35, 4.55, 5.25, 1.9, "101A2A");
s.addText("VERIFIED LIVE", { x: 7.6, y: 4.72, w: 4.8, h: 0.3, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 11, bold: true, color: TEAL, charSpacing: 2 });
s.addText("Two sightings of one stolen-plate entry from two cameras collapsed into one alert with a two-point trail, and the console rendered the route with estimated speed and heading — tested against a real database.", {
  x: 7.6, y: 5.05, w: 4.8, h: 1.3, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12.5, color: T0, lineSpacingMultiple: 1.15 });
s.addNotes("This is the money slide — the graded live test case is our existing watchlist-trail feature. Match A + match B = a route with computed speed/heading. Already verified live.");

// ============================================================ 8. WOMEN'S SAFETY
s = p.addSlide(); base(s);
kicker(s, "Crimes against women", 0.7, 0.55, PINK);
s.addText("A dedicated women's-safety response path", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 30, bold: true, color: T0 });
const ws = [
  ["Instant SOS — no login", "POST /api/sos opens a critical alert immediately. A kiosk or a phone in a crisis can't be expected to authenticate first.", PINK],
  ["Behavioural, not appearance", "A lone person followed, or encircled, in a flagged zone or after dark — geometry and time, never demographics.", BLUE],
  ["Safety Mode on the console", "Reprioritises SOS & safety alerts and highlights isolated/high-risk zones on the map at a glance.", TEAL],
];
ws.forEach((c, i) => {
  const y = 2.1 + i * 1.45;
  card(s, 0.7, y, 11.9, 1.28);
  s.addShape(p.ShapeType.roundRect, { x: 0.95, y: y + 0.24, w: 0.8, h: 0.8, rectRadius: 0.4, fill: { color: "1A1220" }, line: { color: c[2], width: 1.5 } });
  dot(s, 1.2, y + 0.49, 0.3, c[2]);
  s.addText(c[0], { x: 2.0, y: y + 0.2, w: 10.3, h: 0.42, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 17, bold: true, color: T0 });
  s.addText(c[1], { x: 2.0, y: y + 0.64, w: 10.4, h: 0.55, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 13, color: T1, lineSpacingMultiple: 1.1 });
});
s.addText("Additive to the same alert pipeline — an SOS gets the audit trail and live push for free, not a bolted-on parallel system.", {
  x: 0.7, y: 6.55, w: 11.9, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 13, italic: true, color: PINK });
s.addNotes("Women's safety is a first-class path: unauthenticated SOS, behavioural (non-demographic) detection, and a console Safety Mode. It reuses the alert pipeline.");

// ============================================================ 9. SECURITY
s = p.addSlide(); base(s);
kicker(s, "Security & compliance", 0.7, 0.55);
s.addText("Authorization enforced by the database", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 30, bold: true, color: T0 });
s.addText("A bug or compromise in the API cannot widen access — PostgreSQL decides, not the application.", {
  x: 0.7, y: 1.65, w: 11.7, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, color: T1 });
const sec = [
  ["One claim, derived authority", "The app asserts only who the user is; jurisdiction, permissions and scope are derived in-database by SECURITY DEFINER functions. No broader claim exists to forge."],
  ["Tamper-evident audit trail", "Append-only, hash-chained by database trigger, independently re-verifiable. Records purpose + case reference per access — DPDP Act 2023 aligned."],
  ["Credentials encrypted at rest", "Per-credential keys wrapped by a master key held only in the environment; a DB-only compromise yields no usable passwords."],
];
sec.forEach((c, i) => {
  const y = 2.35 + i * 1.15;
  card(s, 0.7, y, 8.3, 1.0);
  dot(s, 0.95, y + 0.34, 0.3, TEAL);
  s.addText(c[0], { x: 1.45, y: y + 0.13, w: 7.4, h: 0.35, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 15, bold: true, color: T0 });
  s.addText(c[1], { x: 1.45, y: y + 0.47, w: 7.45, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 11.5, color: T1, lineSpacingMultiple: 1.05 });
});
card(s, 9.25, 2.35, 3.35, 3.95, "101A2A");
s.addText("198", { x: 9.25, y: 3.35, w: 3.35, h: 1.1, isTextBox: true, margin: 0, align: "center", fontFace: HFONT, fontSize: 64, bold: true, color: TEAL });
s.addText("adversarial security tests", { x: 9.35, y: 4.5, w: 3.15, h: 0.4, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 14, color: T0 });
s.addText("Forged tokens, credential transplant, audit tampering, timing oracles — the attacks, not just round-trips.", {
  x: 9.45, y: 5.0, w: 2.95, h: 1.1, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 11.5, italic: true, color: T2, lineSpacingMultiple: 1.15 });
s.addNotes("RLS in the database, one asserted claim, hash-chained audit trail, encrypted credentials. 198 mostly-adversarial tests cover the security core.");

// ============================================================ 10. SCALE
s = p.addSlide(); base(s);
kicker(s, "Built for the number", 0.7, 0.55);
s.addText("Scaling to 80,000 cameras", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 32, bold: true, color: T0 });
// comparison
card(s, 0.7, 2.0, 5.75, 2.4, "2A1520");
s.addText("VIDEO-CENTRAL", { x: 0.9, y: 2.2, w: 5.3, h: 0.3, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, bold: true, color: RED, charSpacing: 2 });
s.addText("~200 Gbps", { x: 0.9, y: 2.55, w: 5.4, h: 0.9, isTextBox: true, margin: 0, align: "center", fontFace: HFONT, fontSize: 52, bold: true, color: RED });
s.addText("continuous ingress for 80,000 streams — physically infeasible", { x: 0.95, y: 3.6, w: 5.25, h: 0.6, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 13, color: T1 });
card(s, 6.85, 2.0, 5.75, 2.4, "0E2A22");
s.addText("METADATA-CENTRAL (OURS)", { x: 7.05, y: 2.2, w: 5.3, h: 0.3, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, bold: true, color: TEAL, charSpacing: 2 });
s.addText("a few Mbps", { x: 7.05, y: 2.55, w: 5.4, h: 0.9, isTextBox: true, margin: 0, align: "center", fontFace: HFONT, fontSize: 52, bold: true, color: TEAL });
s.addText("only alerts & matches travel centrally — ~16 GB/day statewide", { x: 7.1, y: 3.6, w: 5.25, h: 0.6, isTextBox: true, margin: 0, align: "center", fontFace: BFONT, fontSize: 13, color: T1 });
// three supporting facts
[["Edge compute", "~25–50 cameras / edge box → ~1,600–3,200 boxes, per district, linear"],
 ["Flat security cost", "RLS filters on indexed columns — query plans don't change from 5k to 80k"],
 ["Storage & rollout", "metadata-only, monthly-partitioned; phased on the jurisdiction tree"]].forEach((c, i) => {
  const x = 0.7 + i * 4.05;
  card(s, x, 4.65, 3.85, 1.75);
  s.addText(c[0], { x: x + 0.25, y: 4.85, w: 3.4, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 14, bold: true, color: TEAL });
  s.addText(c[1], { x: x + 0.25, y: 5.28, w: 3.45, h: 1.0, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, color: T1, lineSpacingMultiple: 1.15 });
});
s.addNotes("The metadata-not-video decision makes 80,000 cameras affordable. Edge compute linear per district; RLS cost flat; metadata-only storage; phased rollout on the jurisdiction tree.");

// ============================================================ 11. PROOF
s = p.addSlide(); base(s);
kicker(s, "Not slideware — running software", 0.7, 0.55);
s.addText("Built & verified", { x: 0.7, y: 0.9, w: 12, h: 0.7,
  isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 32, bold: true, color: T0 });
const stats = [["240", "automated tests passing", "198 security + 42 analytics"],
  ["30", "government cameras onboarded", "across 10 Gujarat districts, live"],
  ["3", "reference models", "delivered as one platform"],
  ["0", "video leaves the edge", "metadata-only, by design"]];
stats.forEach((st, i) => {
  const x = 0.7 + (i % 2) * 6.05, y = 2.1 + Math.floor(i / 2) * 2.2;
  card(s, x, y, 5.75, 1.95, "101A2A");
  s.addText(st[0], { x: x + 0.25, y: y + 0.3, w: 2.0, h: 1.3, isTextBox: true, margin: 0, align: "center", fontFace: HFONT, fontSize: 56, bold: true, color: TEAL });
  s.addText(st[1], { x: x + 2.35, y: y + 0.5, w: 3.2, h: 0.6, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 16, bold: true, color: T0, lineSpacingMultiple: 1.0 });
  s.addText(st[2], { x: x + 2.35, y: y + 1.15, w: 3.25, h: 0.5, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 12, color: T1 });
});
s.addNotes("Proof points: 240 tests pass, 30 cameras onboarded live across 10 districts, all three models as one platform, metadata-only architecture.");

// ============================================================ 12. CLOSING
s = p.addSlide(); base(s);
for (let i = 0; i < 6; i++) for (let j = 0; j < 4; j++) dot(s, 9.6 + i * 0.55, 0.5 + j * 0.55, 0.07, "1E2E44");
dot(s, 1.0, 2.35, 0.28, TEAL);
s.addText("See it live", { x: 1.35, y: 2.1, w: 10, h: 0.9, isTextBox: true, margin: 0, fontFace: HFONT, fontSize: 46, bold: true, color: T0 });
s.addText("Sign in → onboard the government grid → watch a plate light up across cameras → pull its route with speed & heading → raise an SOS.", {
  x: 1.0, y: 3.2, w: 10.8, h: 1.0, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 17, color: T1, lineSpacingMultiple: 1.25 });
s.addShape(p.ShapeType.line, { x: 1.0, y: 4.5, w: 3.2, h: 0, line: { color: TEAL, width: 2 } });
s.addText("SENTINEL · The NETRA Platform", { x: 1.0, y: 6.5, w: 11, h: 0.4, isTextBox: true, margin: 0, fontFace: BFONT, fontSize: 14, bold: true, color: T0 });
s.addNotes("Close on the live demo script. Everything shown is real, running software verified against a real database.");

p.writeFile({ fileName: "/root/sentinel/build-tmp/Sentinel_Solution_Presentation.pptx" }).then(f => console.log("WROTE", f));
