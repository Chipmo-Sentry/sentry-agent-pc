/* Floor-plan editor (docs/30) — Konva canvas in a pywebview window.
 *
 * Coordinate model: shapes store points in PLAN-logical coords; the Konva stage's
 * scale/position provides zoom/pan, so stage.getRelativePointerPosition() returns
 * plan coords directly. The Python side (window.pywebview.api) bridges to the
 * backend (load/save plan, list cameras, pick a background image) — the agent JWT
 * never touches JS. Homography + live dots come in Phase B / C. */
"use strict";

// height_m: physical height (m) used by the 3D calibration — the zone then
// covers the fixture's visible solid, not just its floor footprint. 0 = flat
// (exits are floor thresholds). The operator can override per fixture.
const FIX = {
  exit: { color: "#E5484D", label: "Орц/Гарц", height_m: 0 },
  shelf: { color: "#3DD56D", label: "Тавиур", height_m: 1.8 },
  checkout: { color: "#E0A82E", label: "Касс", height_m: 1.0 },
  // Item-taking area like a shelf — the edge engine counts fridge visits into
  // the same repeated-visit behaviour (edge/behavior.py).
  fridge: { color: "#38BDF8", label: "Хөргүүр", height_m: 2.0 },
  // Mannequin/display stand — an item-taking area like a shelf (clothes are
  // lifted off it), so it derives zones + feeds the repeated-visit behaviour.
  mannequin: { color: "#F472B6", label: "Маникен", height_m: 1.7 },
  // Scenery (буйдан/сандал/ширээ): drawable + shown in analytics, but NEVER
  // derived into Camera.zones (no engine meaning — see _compute_calibration).
  furniture: { color: "#A78BFA", label: "Тавилга", height_m: 0 },
};
const WALL_COLOR = "#9CA3AF";
const CAM_COLOR = "#2563EB"; // brand royal-blue (the camera is the Sentry element)
const SNAP_DEG = 15;
const ROT_SNAPS = [0, 45, 90, 135, 180, 225, 270, 315];

// ── real-world scale ────────────────────────────────────────────────────────
// 1 plan-unit == 1 METRE. PLAN.size therefore IS the store's real width × height
// in metres, so lengths are entered/shown directly in metres. A typical retail
// store is ~10×10 m → the default canvas is 20×20 (the old 200×200 made a real
// store a tiny speck); bigger stores set «Талбайн хэмжээ».
const DEFAULT_SIZE_M = [20, 20];
// Grid density adapts to the canvas so a 20 m plan gets 1 m lines and a 200 m
// warehouse doesn't drown in them. Returns [minor, major] in metres.
function gridSteps() {
  const m = Math.max(PLAN.size[0], PLAN.size[1]);
  if (m <= 40) return [1, 5];
  if (m <= 120) return [2, 10];
  return [5, 25];
}
const COORD_DP = 2; // store coords to 2 dp → 1 cm precision
const SHOW_AREA = true; // show m² on fixtures + a store-area total
// Camera coverage overlay: a CALIBRATED camera's footprint is exact (image 0-1
// corners projected through the inverse homography); an UNcalibrated one falls
// back to a rough wedge from its facing + these defaults.
const CAM_FOV_DEG = 90; // assumed horizontal field of view for the rough wedge
const CAM_RANGE_M = 12; // assumed useful range (m) for the rough wedge

const round2 = (v) => Math.round(v * 100) / 100;
// HTML-escape for user-supplied text (fixture labels) that lands in innerHTML.
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
const fmtM = (u) => round2(u).toFixed(COORD_DP); // "12.50" — inputs / status
// Clean display for on-canvas dimension labels: drop trailing ".00" (whole
// numbers show bare, else 1 dp) so "760.00" reads as "760" and "0.50" as "0.5".
const fmtDim = (u) => {
  const r = Math.round(u * 10) / 10;
  return Number.isInteger(r) ? String(r) : r.toFixed(1);
};
// polygon area via the shoelace formula → m² (points are metres)
function polyArea(pts) {
  let a = 0;
  for (let i = 0, n = pts.length; i < n; i++) {
    const p = pts[i], q = pts[(i + 1) % n];
    a += p[0] * q[1] - q[0] * p[1];
  }
  return Math.abs(a) / 2;
}
// width × height of a points bounding box, in metres
function bbox(pts) {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
}

let PLAN = { version: 1, size: DEFAULT_SIZE_M.slice(), walls: [], fixtures: [], cameras: [] };
let cameras = []; // registered cameras [{camera_id, name}]
const camStatus = {}; // camera_id → { online: bool|undefined } from a live check
const REPROJ_WARN = 0.05; // >5% reprojection error = a shaky calibration (yellow)
let tool = "select";
let snapOn = true;
let pointSnapOn = false; // snap new points onto existing shape corners (toggle «⊙ Цэг»)
let orthoOn = false; // lock wall segments to 0/90° (toggle «90°»)
let coverageOn = false; // show camera coverage + blind-spot overlay (toggle «👁»)
let snapMarker = null; // highlight ring shown over the active snap target
let draft = null; // { type, pts: [[x,y],...] } while drawing
let rectDraft = null; // { type, start:[x,y] } while Shift-dragging a rectangle
let marquee = null; // { start:[x,y] } while rubber-band selecting an area
let marqueeRect = null; // the live marquee box node
let marqueeSel = []; // [{kind, idx}] objects selected by the marquee
const marqueeNodes = []; // highlight boxes for the marquee selection
let panning = false; // Space held → drag the canvas instead of selecting
let lastPointer = null; // last plan-space cursor pos (for numeric wall length)
const undoStack = [];
const redoStack = [];

// ── Konva stage + layers ───────────────────────────────────────────────
const holder = document.getElementById("canvas-holder");
const stage = new Konva.Stage({
  container: "canvas-holder",
  width: holder.clientWidth,
  height: holder.clientHeight,
});
const gridLayer = new Konva.Layer({ listening: false });
const shapeLayer = new Konva.Layer();
const covLayer = new Konva.Layer({ listening: false }); // camera coverage + blind spots
const camLayer = new Konva.Layer();
const uiLayer = new Konva.Layer();
stage.add(gridLayer, shapeLayer, covLayer, camLayer, uiLayer);

const tr = new Konva.Transformer({
  rotationSnaps: ROT_SNAPS,
  rotateAnchorOffset: 24,
  enabledAnchors: [],
  borderStroke: "#ffffff",
});
uiLayer.add(tr);
let selectedNode = null;
let vertexAnchors = [];

function setStatus(t) {
  document.getElementById("status").textContent = t || "";
}

// ── geometry: angle snap ────────────────────────────────────────────────
function snapSeg(prev, raw, shift) {
  if (!snapOn || shift || !prev) return raw;
  const dx = raw[0] - prev[0], dy = raw[1] - prev[1];
  const dist = Math.hypot(dx, dy);
  if (dist < 1e-9) return raw;
  const a = (Math.round((Math.atan2(dy, dx) * 180) / Math.PI / SNAP_DEG) * SNAP_DEG * Math.PI) / 180;
  return [prev[0] + Math.cos(a) * dist, prev[1] + Math.sin(a) * dist];
}

function pointerPlan() {
  const p = stage.getRelativePointerPosition();
  return [p.x, p.y];
}

// ── point snap (to existing corners) + ortho lock ───────────────────────────
// Every corner of every wall/fixture is a candidate the new point can latch onto.
function snapCandidates() {
  const pts = [];
  PLAN.walls.forEach((w) => w.points.forEach((p) => pts.push(p)));
  PLAN.fixtures.forEach((f) => f.points.forEach((p) => pts.push(p)));
  return pts;
}
// Nearest existing corner within ~12 px (screen) of `raw`, or null. Toggle-gated.
function snapPoint(raw) {
  if (!pointSnapOn) return null;
  const thr = 12 / (stage.scaleX() || 1);
  let best = null, bd = thr;
  for (const p of snapCandidates()) {
    const d = Math.hypot(p[0] - raw[0], p[1] - raw[1]);
    if (d < bd) { bd = d; best = p; }
  }
  return best ? [best[0], best[1]] : null;
}
// Lock a segment to pure horizontal/vertical (keep the longer axis).
function orthoSeg(prev, raw) {
  return Math.abs(raw[0] - prev[0]) >= Math.abs(raw[1] - prev[1])
    ? [raw[0], prev[1]] : [prev[0], raw[1]];
}
// The point a wall vertex should land on: existing-corner snap wins, else ortho
// lock (if on), else the 15° angle snap.
function wallPoint(prev, raw) {
  const sp = snapPoint(raw);
  if (sp) return sp;
  if (!prev) return raw;
  if (orthoOn) return orthoSeg(prev, raw);
  return snapSeg(prev, raw, false);
}
// Show / hide the snap-target highlight ring at a plan point.
function showSnapMarker(p, color) {
  clearSnapMarker();
  const r = 7 / (stage.scaleX() || 1);
  snapMarker = new Konva.Circle({ x: p[0], y: p[1], radius: r, stroke: color || "#22d3ee", strokeWidth: 2 / (stage.scaleX() || 1), listening: false });
  uiLayer.add(snapMarker);
}
function clearSnapMarker() {
  if (snapMarker) { snapMarker.destroy(); snapMarker = null; }
}

// ── zoom / pan ──────────────────────────────────────────────────────────
// Plain wheel scrolls the canvas (Shift = sideways); Ctrl+wheel — which is also
// what browsers report for a trackpad pinch — zooms about the cursor. Space+drag
// stays as the free-pan fallback.
stage.on("wheel", (e) => {
  e.evt.preventDefault();
  if (!(e.evt.ctrlKey || e.evt.metaKey)) {
    const dx = e.evt.shiftKey && !e.evt.deltaX ? e.evt.deltaY : e.evt.deltaX;
    const dy = e.evt.shiftKey ? 0 : e.evt.deltaY;
    stage.position({ x: stage.x() - dx, y: stage.y() - dy });
    return;
  }
  const old = stage.scaleX();
  const pointer = stage.getPointerPosition();
  const to = { x: (pointer.x - stage.x()) / old, y: (pointer.y - stage.y()) / old };
  // 0.02..400 px/m: 400 zooms a 2 m shelf across an HD screen (fine vertex
  // work); the old cap of 20 couldn't even fill the window with a 10 m store.
  const ns = Math.max(0.02, Math.min(400, old * (e.evt.deltaY > 0 ? 1 / 1.1 : 1.1)));
  stage.scale({ x: ns, y: ns });
  stage.position({ x: pointer.x - to.x * ns, y: pointer.y - to.y * ns });
  redrawShapes(); // keep labels/strokes ~constant on-screen while zooming
});

function setTool(t) {
  tool = t;
  cancelDraft();
  cancelRect();
  cancelMarquee();
  document.querySelectorAll(".tool").forEach((b) => b.classList.toggle("active", b.dataset.tool === t));
  // Drag-empty = marquee-select (select mode) or draw — never pan; pan is on Space.
  stage.draggable(false);
  if (t !== "select") deselect();
  // In select mode every shape is draggable UP FRONT so a single press-and-drag
  // moves it (Konva checks draggable at mousedown-start; setting it only inside
  // the click handler would need a 2nd drag). Off in draw modes so it can't
  // interfere with drawing.
  setShapesDraggable(t === "select");
  // Show the width×height inputs for the box tools — fixtures AND the
  // room/outer-wall rectangle (so the store outline can be typed exactly).
  const rw = document.getElementById("rect-wrap");
  if (rw) rw.classList.toggle("len-hidden", !FIX[t] && t !== "room");
  stage.container().style.cursor = t === "select" ? "default" : "crosshair";
}

// Toggle draggability of every placed shape/camera (not labels/grid/preview).
function setShapesDraggable(on) {
  shapeLayer.find("Line").forEach((n) => n.draggable(on));
  camLayer.find("Group").forEach((n) => n.draggable(on));
}

// ── render plan → Konva ─────────────────────────────────────────────────
// Redraw ONLY the Konva canvas (shapes + labels + grid + cameras). Labels and
// strokes are counter-scaled (X / stage.scaleX()) so they read ~constant on
// screen — but that only holds if they're rebuilt AFTER a scale change, so this
// is called on every zoom/fit (not just on plan edits). Cheap: a handful of
// vector shapes, no HTML.
function redrawShapes() {
  shapeLayer.destroyChildren();
  camLayer.destroyChildren();
  PLAN.walls.forEach((w, i) => shapeLayer.add(makeLine(w.points, WALL_COLOR, false, "wall", i)));
  PLAN.fixtures.forEach((f, i) =>
    shapeLayer.add(makeLine(f.points, (FIX[f.type] || {}).color || "#999", true, "fixture", i, f.label || (FIX[f.type] || {}).label)),
  );
  PLAN.cameras.forEach((c, i) => camLayer.add(makeCamera(c, i)));
  shapeLayer.draw();
  camLayer.draw();
  drawGrid();
  // Keep the camera-coverage overlay tracking the scale while it's toggled on.
  if (typeof coverageOn !== "undefined" && coverageOn) renderCoverage();
  // Nodes are recreated non-draggable; restore one-gesture move in select mode.
  setShapesDraggable(tool === "select");
}

function render() {
  redrawShapes();
  renderElements();
  renderTotals();
  updateCoverageStat(renderCoverage());
  renderReadiness();
}

// Setup-readiness summary: are all cameras calibrated, and is the floor covered?
// Surfaces a store that's silently under-protected (uncalibrated → no zones;
// blind spots → unwatched). Hidden until at least one camera is placed.
function renderReadiness() {
  const el = document.getElementById("readiness");
  if (!el) return;
  const cams = PLAN.cameras;
  if (!cams.length) { el.classList.add("cs-hidden"); el.innerHTML = ""; return; }
  el.classList.remove("cs-hidden");
  const calib = cams.filter((c) => c.homography).length;
  const pct = coverageInfo().pct;
  const issues = [];
  if (calib < cams.length) issues.push(`⚠ ${cams.length - calib}/${cams.length} камер калибровкгүй (зон үүсэхгүй)`);
  if (pct != null && pct < 90) issues.push(`⚠ хамрагдалт ${pct}% — 🔴 сохор бүс бий`);
  el.innerHTML = issues.length === 0
    ? `<b style="color:#3DD56D">✅ Бэлэн</b> — ${calib}/${cams.length} калибровктой, хамрагдалт ${pct}%`
    : `<b style="color:#E0A82E">⚠ Бэлэн биш</b><br><span class="ss-muted">${issues.join("<br>")}</span>`;
}

// Sidebar coverage readout — % of the store floor at least one camera sees.
function updateCoverageStat(pct) {
  const el = document.getElementById("coverage-stat");
  if (!el) return;
  if (!coverageOn || pct == null) { el.classList.add("cs-hidden"); el.innerHTML = ""; return; }
  const exact = PLAN.cameras.filter((c) => c.homography).length;
  const color = pct >= 90 ? "#3DD56D" : pct >= 70 ? "#E0A82E" : "#E5484D";
  el.classList.remove("cs-hidden");
  el.innerHTML =
    `Хамрагдалт: <b style="color:${color}">${pct}%</b><br>` +
    `<span class="ss-muted">🟢 нарийн ${exact}/${PLAN.cameras.length} (калибровктой) · 🟡 баримжаа · 🔴 сохор бүс</span>`;
}

// Sidebar readout: store dimensions/area + element counts (metres, m²).
function renderTotals() {
  const el = document.getElementById("totals");
  if (!el) return;
  const [pw, ph] = PLAN.size;
  const fixArea = PLAN.fixtures.reduce((s, f) => s + polyArea(f.points), 0);
  el.innerHTML =
    `Талбай: <b>${fmtM(pw)} × ${fmtM(ph)} м</b> · ${Math.round(pw * ph).toLocaleString()} м²<br>` +
    (SHOW_AREA ? `Объект эзэлхүүн: <b>${fmtM(fixArea)} м²</b><br>` : "") +
    `Тавиур/объект: <b>${PLAN.fixtures.length}</b> · Хана: <b>${PLAN.walls.length}</b> · Камер: <b>${PLAN.cameras.length}</b>`;
  const wi = document.getElementById("plan-w"), hi = document.getElementById("plan-h");
  if (wi && document.activeElement !== wi) wi.value = fmtM(pw);
  if (hi && document.activeElement !== hi) hi.value = fmtM(ph);
}
// Set the store's real width × height (metres) and refit.
function setPlanSize(w, h) {
  if (!(w > 0 && h > 0)) return;
  PLAN.size = [round2(w), round2(h)];
  deselect();
  pushUndo();
  render();
  fit();
  setStatus(`Талбайн хэмжээ ${fmtM(w)} × ${fmtM(h)} м`);
}

function makeLine(pts, color, closed, kind, idx, label) {
  const flat = pts.flat();
  const line = new Konva.Line({
    points: flat,
    stroke: color,
    // Counter-scaled so the outline stays a crisp ~1.6 px on screen at any zoom
    // (was 2 plan-units → thick on a large store plan). Rebuilt on every
    // zoom/fit via redrawShapes(), so it tracks the scale.
    strokeWidth: 1.6 / stage.scaleX(),
    closed: closed,
    fill: closed ? color + "22" : undefined,
    draggable: false,
    name: `${kind}:${idx}`,
    hitStrokeWidth: 12 / stage.scaleX(),
  });
  line.on("mousedown", (e) => {
    if (tool === "select") {
      e.cancelBubble = true;
      selectShape(line, kind, idx);
    }
  });
  // Double-click near an edge (select mode) inserts a vertex at that spot, so a
  // drawn shape can gain corners without redrawing it. The distance gate keeps
  // dblclicks on a fixture's filled interior from inserting phantom points.
  line.on("dblclick dbltap", (e) => {
    if (tool !== "select") return;
    e.cancelBubble = true;
    const arr = kind === "wall" ? PLAN.walls[idx].points : PLAN.fixtures[idx].points;
    const p = pointerPlan();
    const segs = closed ? arr.length : arr.length - 1;
    let at = -1, bd = 12 / stage.scaleX(), bp = null;
    for (let i = 0; i < segs; i++) {
      const a = arr[i], b = arr[(i + 1) % arr.length];
      const vx = b[0] - a[0], vy = b[1] - a[1];
      const L2 = vx * vx + vy * vy;
      const t = L2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2)) : 0;
      const q = [a[0] + vx * t, a[1] + vy * t];
      const d = Math.hypot(p[0] - q[0], p[1] - q[1]);
      if (d < bd) { bd = d; at = i; bp = q; }
    }
    if (at < 0) return;
    arr.splice(at + 1, 0, [round2(bp[0]), round2(bp[1])]);
    pushUndo();
    reselectShape(kind === "wall" ? "walls" : "fixtures", idx);
    setStatus("Өнцөг нэмэгдлээ — цагаан цэгийг чирж байрлуулна");
  });
  if (label && pts.length) {
    const t = new Konva.Text({
      text: label, fontSize: 11.5 / stage.scaleX(), fontStyle: "bold", fill: color,
      align: "center", listening: false,
    });
    positionShapeLabel(t, pts);
    line._label = t;
    shapeLayer.add(t);
  }
  addDimLabels(pts, color, kind);
  return line;
}

// Centre the name label in the shape, its bottom sitting just above the centred
// W×H/м² block — anchored to the bbox, not to whichever corner was drawn first.
function positionShapeLabel(t, pts) {
  const b = bbox(pts);
  t.position({ x: b.x + b.w / 2, y: b.y + b.h / 2 });
  t.offsetX(t.width() / 2);
  t.offsetY(t.height() + (9.5 / stage.scaleX()) * 1.5);
}

// Always-on measurement labels (metres): per-segment length for walls; a centred
// «W × H м» + area «… м²» for fixtures. Counter-scaled so they read ~constant on
// screen; rebuilt every render(), so no manual cleanup.
function addDimLabels(pts, color, kind) {
  // Counter-scaled to read ~constant on screen. Lighter + un-bolded + smaller
  // than before (the bold 2-dp numbers read as heavy/ugly on a dense plan).
  const fs = 9.5 / stage.scaleX();
  const mk = (x, y, text, opts) => shapeLayer.add(new Konva.Text(Object.assign(
    { x, y, text, fontSize: fs, fill: "#9aa6b6", listening: false }, opts || {})));
  if (kind === "wall") {
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (len < 1e-6) continue;
      // nudge the label just off the segment midpoint (perpendicular)
      const nx = -(b[1] - a[1]) / len, ny = (b[0] - a[0]) / len;
      mk((a[0] + b[0]) / 2 + nx * fs * 0.6, (a[1] + b[1]) / 2 + ny * fs * 0.6, `${fmtDim(len)} м`, { fill: "#c2cad6" });
    }
  } else {
    const b = bbox(pts);
    const lines = [`${fmtDim(b.w)} × ${fmtDim(b.h)} м`];
    if (SHOW_AREA) lines.push(`${fmtDim(polyArea(pts))} м²`);
    const t = new Konva.Text({
      text: lines.join("\n"), fontSize: fs, fill: color,
      align: "center", lineHeight: 1.2, listening: false, opacity: 0.9,
    });
    t.position({ x: b.x + b.w / 2, y: b.y + b.h / 2 });
    t.offsetX(t.width() / 2);
    t.offsetY(t.height() / 2);
    shapeLayer.add(t);
  }
}

// Status badge color + mark for a camera icon: red=offline, yellow=needs/shaky
// calibration, green=calibrated & online, null=calibrated but not yet checked.
function cameraBadge(cam) {
  const calibrated = !!(cam.homography || cam._calibrated);
  const st = camStatus[cam.camera_id] || {};
  if (st.online === false) return { color: "#E5484D", mark: "!" };
  if (!calibrated) return { color: "#E0A82E", mark: "!" };
  if (cam.reproj_err != null && cam.reproj_err > REPROJ_WARN) return { color: "#E0A82E", mark: "!" };
  if (st.online === true) return { color: "#3DD56D", mark: "" };
  return null;
}

// Calibration quality from the reprojection error → a clear verdict + guidance,
// so a shaky calibration (→ wrong zones → missed theft) is obvious and fixable.
function calibVerdict(err) {
  if (err == null) return { word: "—", color: "#a1a1aa", hint: "" };
  if (err < 0.02) return { word: "Маш сайн", color: "#3DD56D", hint: "" };
  if (err < REPROJ_WARN) return { word: "Сайн", color: "#3DD56D", hint: "" };
  if (err < 0.1) return { word: "Дунд", color: "#E0A82E", hint: "цэг нэмж нарийсга" };
  return { word: "Сул", color: "#E5484D", hint: "цэгүүдээ дахин нарийн дар" };
}

// A status badge node (named "badge") for a camera, or null. Counter-rotated so
// any "!" stays upright; rebuilt in place by checkCameraStatus without a full render.
function makeBadge(cam) {
  const badge = cameraBadge(cam);
  if (!badge) return null;
  const bg = new Konva.Group({ x: -10, y: -10, rotation: -(cam.dir_deg || 0), name: "badge" });
  bg.add(new Konva.Circle({ radius: 6, fill: badge.color, stroke: "#0a0a0a", strokeWidth: 1.5 }));
  if (badge.mark) bg.add(new Konva.Text({ text: badge.mark, fontSize: 10, fontStyle: "bold", fill: "#0a0a0a", x: -2, y: -5.5 }));
  return bg;
}

function makeCamera(cam, idx) {
  const g = new Konva.Group({
    x: cam.pos[0], y: cam.pos[1], rotation: cam.dir_deg || 0,
    draggable: false, name: `camera:${idx}`,
  });
  g.add(new Konva.Arrow({ points: [0, 0, 26, 0], stroke: CAM_COLOR, fill: CAM_COLOR, strokeWidth: 2, pointerLength: 7, pointerWidth: 7 }));
  g.add(new Konva.Circle({ radius: 7, fill: CAM_COLOR }));
  const label = new Konva.Text({ x: 10, y: -20, text: cam.name || cam.camera_id, fontSize: 11, fontStyle: "bold", fill: CAM_COLOR, rotation: 0 });
  // keep the label upright regardless of camera rotation
  label.rotation(-(cam.dir_deg || 0));
  g.add(label);
  const badgeNode = makeBadge(cam);
  if (badgeNode) g.add(badgeNode);
  // The glyph is drawn in plan-units; counter-scale the whole group so the
  // camera marker + label read ~constant on screen (not gigantic on a big store
  // plan). dragend reads g.x()/g.y(), which group scale does not affect.
  const s = stage.scaleX() || 1;
  g.scale({ x: 1 / s, y: 1 / s });
  g.on("mousedown", (e) => {
    if (tool === "select") {
      e.cancelBubble = true;
      selectCamera(g, idx);
    }
  });
  g.on("dragend", () => {
    PLAN.cameras[idx].pos = [g.x(), g.y()];
    pushUndo();
    if (coverageOn) updateCoverageStat(renderCoverage()); // footprint follows the camera
  });
  g.on("transformend", () => {
    PLAN.cameras[idx].dir_deg = Math.round(g.rotation()) % 360;
    label.rotation(-g.rotation());
    setStatus(`Чиглэл ${PLAN.cameras[idx].dir_deg}°`);
    pushUndo();
    if (coverageOn) updateCoverageStat(renderCoverage());
  });
  return g;
}

function drawGrid() {
  gridLayer.destroyChildren();
  const [pw, ph] = PLAN.size;
  const sx = stage.scaleX() || 1;
  const [GRID_MINOR_M, GRID_MAJOR_M] = gridSteps();
  // Too many minor lines → drop them, keep the major grid (guards a huge
  // hand-typed canvas even after the adaptive steps).
  const minor = pw / GRID_MINOR_M > 160 || ph / GRID_MINOR_M > 160 ? GRID_MAJOR_M : GRID_MINOR_M;
  const isMajor = (v) => Math.abs(v % GRID_MAJOR_M) < 1e-6 || Math.abs((v % GRID_MAJOR_M) - GRID_MAJOR_M) < 1e-6;
  const fs = 10 / sx;
  for (let x = 0; x <= pw + 1e-6; x += minor) {
    const major = isMajor(x);
    gridLayer.add(new Konva.Line({ points: [x, 0, x, ph], stroke: major ? "#26262e" : "#141418", strokeWidth: (major ? 1.2 : 1) / sx, listening: false }));
    if (major && x > 0) gridLayer.add(new Konva.Text({ x: x + 2 / sx, y: 2 / sx, text: String(Math.round(x)), fontSize: fs, fill: "#48484f", listening: false }));
  }
  for (let y = 0; y <= ph + 1e-6; y += minor) {
    const major = isMajor(y);
    gridLayer.add(new Konva.Line({ points: [0, y, pw, y], stroke: major ? "#26262e" : "#141418", strokeWidth: (major ? 1.2 : 1) / sx, listening: false }));
    if (major && y > 0) gridLayer.add(new Konva.Text({ x: 2 / sx, y: y + 2 / sx, text: String(Math.round(y)), fontSize: fs, fill: "#48484f", listening: false }));
  }
  gridLayer.add(new Konva.Rect({ x: 0, y: 0, width: pw, height: ph, stroke: "#3f3f46", strokeWidth: 1.5 / sx, listening: false }));
  gridLayer.draw();
  updateScaleBar();
}

// A bottom-left scale bar: pick a "nice" metre length that's ≤ ~130 px on screen.
function updateScaleBar() {
  const bar = document.getElementById("scalebar");
  if (!bar) return;
  const sx = stage.scaleX() || 1;
  // Down to 5 cm — the zoom now goes deep enough that 0.5 m spans the screen.
  const nice = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500];
  let m = nice[0];
  for (const n of nice) { if (n * sx <= 130) m = n; }
  bar.style.width = Math.round(m * sx) + "px";
  bar.textContent = m >= 1 ? `${m} м` : `${m * 100} см`;
}

// ── camera coverage + blind spots ───────────────────────────────────────────
// The stored homography maps PLAN → image(0-1) (see floor_plan_web._compute_
// calibration). Its inverse maps image(0-1) → plan, so the four image corners
// project to the exact floor quad the camera sees.
function invert3x3(m) {
  const [a, b, c] = m[0], [d, e, f] = m[1], [g, h, i] = m[2];
  const A = e * i - f * h, B = c * h - b * i, C = b * f - c * e;
  const det = a * A + d * B + g * C;
  if (!det || !isFinite(det)) return null;
  const id = 1 / det;
  return [
    [A * id, B * id, C * id],
    [(f * g - d * i) * id, (a * i - c * g) * id, (c * d - a * f) * id],
    [(d * h - e * g) * id, (b * g - a * h) * id, (a * e - b * d) * id],
  ];
}
function applyH(m, p) {
  const x = p[0], y = p[1];
  const w = m[2][0] * x + m[2][1] * y + m[2][2];
  if (Math.abs(w) < 1e-9) return null;
  return [(m[0][0] * x + m[0][1] * y + m[0][2]) / w, (m[1][0] * x + m[1][1] * y + m[1][2]) / w];
}
// The plan polygon a camera covers: the exact homography quad if calibrated,
// else a rough wedge from its facing. Returns {pts, exact} or null.
function cameraFootprint(cam) {
  if (cam.homography) {
    const inv = invert3x3(cam.homography);
    if (inv) {
      const quad = [[0, 0], [1, 0], [1, 1], [0, 1]].map((c) => applyH(inv, c));
      if (quad.every(Boolean)) return { pts: quad, exact: true };
    }
  }
  const [cx, cy] = cam.pos;
  const base = (cam.dir_deg || 0) * Math.PI / 180;
  const half = (CAM_FOV_DEG / 2) * Math.PI / 180;
  const pts = [[cx, cy]];
  for (let k = 0; k <= 12; k++) {
    const a = base - half + (2 * half) * (k / 12);
    pts.push([cx + Math.cos(a) * CAM_RANGE_M, cy + Math.sin(a) * CAM_RANGE_M]);
  }
  return { pts, exact: false };
}
function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// ── wall occlusion ───────────────────────────────────────────────────────────
// Walls block sight: a camera's footprint must STOP at the first wall each ray
// hits, so the area behind a wall reads (correctly) as a blind spot.

// Parametric t (>= eps) where ray o+t·d crosses segment a-b, or null.
function raySegT(o, d, a, b, eps) {
  const rx = b[0] - a[0], ry = b[1] - a[1];
  const den = d[0] * ry - d[1] * rx;
  if (Math.abs(den) < 1e-12) return null; // parallel
  const qx = a[0] - o[0], qy = a[1] - o[1];
  const t = (qx * ry - qy * rx) / den;
  const u = (qx * d[1] - qy * d[0]) / -den;
  return t >= (eps || 1e-6) && u >= 0 && u <= 1 ? t : null;
}

// Nearest wall hit along the ray, capped at tmax.
function nearestWallT(o, d, tmax) {
  let best = tmax;
  PLAN.walls.forEach((w) => {
    const pts = w.points;
    for (let i = 0; i < pts.length - 1; i++) {
      const t = raySegT(o, d, pts[i], pts[i + 1], 1e-3);
      if (t !== null && t < best) best = t;
    }
  });
  return best;
}

// [tin, tout] where the ray enters/leaves the (convex) footprint, or null.
function rayPolyInterval(o, d, poly) {
  const ts = [];
  for (let i = 0; i < poly.length; i++) {
    const t = raySegT(o, d, poly[i], poly[(i + 1) % poly.length], -1e-9);
    if (t !== null) ts.push(t);
  }
  if (!ts.length) return null;
  return [Math.max(0, Math.min(...ts)), Math.max(...ts)];
}

// The footprint polygon with everything behind a wall cut away: sweep rays from
// the camera across the footprint's angular span; each ray's visible stretch is
// [entry, min(exit, first wall)]. Near points forward + far points backward
// reassemble the clipped polygon. No walls → the footprint passes through as-is.
function occludeFootprint(cam, pts) {
  if (!PLAN.walls.length) return pts;
  const o = cam.pos;
  const angles = [];
  pts.forEach((p) => {
    const dx = p[0] - o[0], dy = p[1] - o[1];
    if (Math.hypot(dx, dy) > 1e-9) angles.push(Math.atan2(dy, dx));
  });
  if (!angles.length) return pts;
  const ref = angles[0];
  const rel = (a) => {
    let r = a - ref;
    while (r <= -Math.PI) r += 2 * Math.PI;
    while (r > Math.PI) r -= 2 * Math.PI;
    return r;
  };
  const offs = angles.map(rel);
  const amin = Math.min(...offs), amax = Math.max(...offs);
  const near = [], far = [];
  const N = 90;
  for (let k = 0; k <= N; k++) {
    const a = ref + amin + ((amax - amin) * k) / N;
    const d = [Math.cos(a), Math.sin(a)];
    const iv = rayPolyInterval(o, d, pts);
    if (!iv || iv[1] <= 1e-6) continue;
    const tw = nearestWallT(o, d, iv[1]);
    if (tw <= iv[0] + 1e-6) continue; // wall before the footprint even starts
    near.push([o[0] + d[0] * iv[0], o[1] + d[1] * iv[0]]);
    far.push([o[0] + d[0] * tw, o[1] + d[1] * tw]);
  }
  if (far.length < 2) return []; // fully walled off
  return near.concat(far.reverse());
}

// Compute footprints + grid coverage WITHOUT drawing (used by the readiness
// summary too). Returns {foots, blind:[[x,y]], step, pct}.
function coverageInfo() {
  const foots = PLAN.cameras
    .map((cam) => {
      const f = cameraFootprint(cam);
      if (!f) return null;
      const clipped = occludeFootprint(cam, f.pts);
      return clipped.length >= 3 ? { pts: clipped, exact: f.exact } : null;
    })
    .filter(Boolean);
  const [pw, ph] = PLAN.size;
  const step = Math.max(pw, ph) / 70; // ~70 cells across the long side
  const blind = [];
  let covered = 0, total = 0;
  for (let y = step / 2; y < ph; y += step) {
    for (let x = step / 2; x < pw; x += step) {
      total++;
      if (foots.some((f) => pointInPoly(x, y, f.pts))) covered++;
      else blind.push([x, y]);
    }
  }
  return { foots, blind, step, pct: total ? Math.round((covered / total) * 100) : null };
}

// Draw the coverage footprints + shade store cells no camera sees; return the
// covered-area percentage (or null when off / no cameras).
function renderCoverage() {
  covLayer.destroyChildren();
  if (!coverageOn) { covLayer.batchDraw(); return null; }
  const info = coverageInfo();
  info.foots.forEach((f) => {
    covLayer.add(new Konva.Line({
      points: f.pts.flat(), closed: true,
      fill: f.exact ? "#22c55e22" : "#eab30822", // green = exact, amber = rough estimate
      stroke: f.exact ? "#22c55e" : "#eab308", strokeWidth: 1 / (stage.scaleX() || 1),
      dash: f.exact ? undefined : [4, 3], listening: false,
    }));
  });
  const h = info.step;
  info.blind.forEach(([x, y]) => covLayer.add(new Konva.Rect({
    x: x - h / 2, y: y - h / 2, width: h, height: h, fill: "#ef444433", listening: false,
  })));
  covLayer.batchDraw();
  return info.pct;
}

// ── selection / transform / vertex editing ──────────────────────────────
function deselect() {
  tr.nodes([]);
  vertexAnchors.forEach((a) => a.destroy());
  vertexAnchors = [];
  selectedNode = null;
  clearMarqueeSel();
  hideCameraSettings();
  hideShapeSettings();
  uiLayer.draw();
}

function selectCamera(node, idx) {
  deselect();
  selectedNode = { node, kind: "cameras", idx };
  node.draggable(true);
  tr.nodes([node]);
  uiLayer.draw();
  showCameraSettings(idx);
  checkCameraStatus(idx); // live online/offline → updates badge + panel
}

function selectShape(line, kind, idx) {
  deselect();
  selectedNode = { node: line, kind: kind === "wall" ? "walls" : "fixtures", idx };
  line.draggable(true);
  // Draggable vertex anchors to reshape after drawing.
  const arr = kind === "wall" ? PLAN.walls[idx].points : PLAN.fixtures[idx].points;
  arr.forEach((pt, vi) => {
    const a = new Konva.Circle({
      x: pt[0], y: pt[1], radius: 5 / stage.scaleX(), fill: "#fff", stroke: "#000",
      strokeWidth: 1, draggable: true,
    });
    a.on("dragmove", () => {
      arr[vi] = [round2(a.x()), round2(a.y())];
      line.points(arr.flat());
      if (line._label) positionShapeLabel(line._label, arr);
      shapeLayer.batchDraw();
    });
    a.on("dragend", () => { pushUndo(); reselectShape(selectedNode.kind, idx); }); // refresh dim labels
    vertexAnchors.push(a);
    uiLayer.add(a);
  });
  // Moving the whole shape bakes the offset back into the points.
  line.on("dragend", () => {
    const ox = line.x(), oy = line.y();
    arr.forEach((p) => { p[0] = round2(p[0] + ox); p[1] = round2(p[1] + oy); });
    line.points(arr.flat());
    line.position({ x: 0, y: 0 });
    pushUndo();
    reselectShape(kind === "wall" ? "walls" : "fixtures", idx); // refresh anchors + dim labels
  });
  showShapeSettings(selectedNode.kind, idx);
  uiLayer.draw();
}

// Effective fixture height (m): explicit value wins, else the type default.
function fixHeight(f) {
  if (typeof f.height_m === "number" && isFinite(f.height_m) && f.height_m >= 0) return f.height_m;
  return (FIX[f.type] || {}).height_m || 0;
}

// ── selected-shape dimension panel (edit width×height / wall length in m) ────
function hideShapeSettings() {
  const el = document.getElementById("shape-settings");
  if (el) { el.classList.add("cs-hidden"); el.innerHTML = ""; }
}
function showShapeSettings(kindPlural, idx) {
  const el = document.getElementById("shape-settings");
  if (!el) return;
  if (kindPlural === "fixtures") {
    const f = PLAN.fixtures[idx];
    const b = bbox(f.points);
    el.innerHTML =
      `<h2>▦ ${(FIX[f.type] || {}).label || f.type}</h2>` +
      `<div class="ss-row">нэр<input id="ss-name" type="text" maxlength="64" placeholder="ж: Архины тавиур" value="${f.label ? esc(f.label) : ""}"></div>` +
      `<div class="ss-row">урт (м)<input id="ss-w" type="number" min="0.1" step="0.01" value="${fmtM(b.w)}"></div>` +
      `<div class="ss-row">өргөн (м)<input id="ss-h" type="number" min="0.1" step="0.01" value="${fmtM(b.h)}"></div>` +
      `<div class="ss-row">өндөр (м)<input id="ss-z" type="number" min="0" step="0.05" value="${fmtM(fixHeight(f))}"></div>` +
      `<div class="ss-muted">Талбай: ${fmtM(polyArea(f.points))} м² · өндөр 0 = хавтгай (зөвхөн шалны зон)</div>` +
      `<button id="ss-apply" class="primary">Тавих</button>`;
    el.classList.remove("cs-hidden");
    const apply = () => {
      // Name first (cheap, no geometry), then dimensions if they changed.
      const name = document.getElementById("ss-name").value.trim();
      const renamed = (name || null) !== (f.label || null);
      if (renamed) { f.label = name || null; pushUndo(); }
      const z = parseFloat(document.getElementById("ss-z").value);
      const heightChanged = isFinite(z) && z >= 0 && Math.abs(z - fixHeight(f)) > 0.005;
      if (heightChanged) { f.height_m = z; pushUndo(); }
      const w = parseFloat(document.getElementById("ss-w").value);
      const h = parseFloat(document.getElementById("ss-h").value);
      if (w > 0 && h > 0 && (Math.abs(w - b.w) > 0.005 || Math.abs(h - b.h) > 0.005)) {
        resizeFixture(idx, w, h);
      } else if (renamed || heightChanged) {
        reselectShape("fixtures", idx);
        setStatus(heightChanged ? `Өндөр ${z} м хадгалагдлаа` : (name ? `Нэр «${name}» хадгалагдлаа` : "Нэр арилгалаа"));
      }
    };
    document.getElementById("ss-apply").onclick = apply;
    const nameEl = document.getElementById("ss-name");
    nameEl.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } e.stopPropagation(); });
  } else if (kindPlural === "walls") {
    const segs = [];
    const pts = PLAN.walls[idx].points;
    for (let i = 0; i < pts.length - 1; i++) segs.push(Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]));
    if (segs.length === 1) {
      el.innerHTML =
        `<h2>▭ Хана</h2>` +
        `<div class="ss-row">урт (м)<input id="ss-l" type="number" min="0.1" step="0.01" value="${fmtM(segs[0])}"></div>` +
        `<button id="ss-apply" class="primary">Урт тавих</button>`;
      el.classList.remove("cs-hidden");
      document.getElementById("ss-apply").onclick = () => {
        const l = parseFloat(document.getElementById("ss-l").value);
        if (l > 0) setWallLen(idx, l);
      };
    } else {
      el.innerHTML =
        `<h2>▭ Хана</h2>` +
        `<div class="ss-muted">${segs.map((s, i) => `${i + 1}: ${fmtM(s)} м`).join("<br>")}</div>` +
        `<div class="ss-muted">Нийт: ${fmtM(segs.reduce((a, b) => a + b, 0))} м</div>`;
      el.classList.remove("cs-hidden");
    }
  } else {
    hideShapeSettings();
  }
}
// Re-render then re-select the same shape (panel + anchors stay live after edit).
function reselectShape(kindPlural, idx) {
  render();
  const singular = kindPlural === "walls" ? "wall" : "fixture";
  let n = null;
  shapeLayer.find("Line").forEach((l) => { if (l.name() === `${singular}:${idx}`) n = l; });
  if (n) selectShape(n, singular, idx);
}
function resizeFixture(idx, w, h) {
  const f = PLAN.fixtures[idx];
  const b = bbox(f.points);
  const x1 = round2(b.x), y1 = round2(b.y), x2 = round2(b.x + w), y2 = round2(b.y + h);
  f.points = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
  pushUndo();
  reselectShape("fixtures", idx);
  setStatus(`Хэмжээ ${fmtM(w)} × ${fmtM(h)} м`);
}
function setWallLen(idx, l) {
  const pts = PLAN.walls[idx].points;
  const a = pts[0], b = pts[1];
  const d = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
  pts[1] = [round2(a[0] + ((b[0] - a[0]) / d) * l), round2(a[1] + ((b[1] - a[1]) / d) * l)];
  pushUndo();
  reselectShape("walls", idx);
  setStatus(`Урт ${fmtM(l)} м`);
}

// ── drawing ──────────────────────────────────────────────────────────────
let previewLine = null;
function cancelDraft() {
  draft = null;
  if (previewLine) { previewLine.destroy(); previewLine = null; }
  clearSnapMarker();
  hideLenInput();
  uiLayer.batchDraw();
  setStatus("");
}

// ── numeric wall length ─────────────────────────────────────────────────────
// While drawing a wall: aim the cursor for DIRECTION, type a length + Enter to
// drop the next vertex at that exact distance (angle-snapped).
function showLenInput() {
  const w = document.getElementById("len-wrap");
  if (w) w.classList.remove("len-hidden");
}
function hideLenInput() {
  const w = document.getElementById("len-wrap");
  if (w) w.classList.add("len-hidden");
  const i = document.getElementById("len-input");
  if (i) i.value = "";
}
function applyLenInput() {
  const len = parseFloat(document.getElementById("len-input").value); // metres
  if (!draft || !draft.pts.length || !(len > 0)) return;
  const last = draft.pts[draft.pts.length - 1];
  const aim = snapSeg(last, lastPointer || [last[0] + 1, last[1]], false);
  const dx = aim[0] - last[0], dy = aim[1] - last[1];
  const d = Math.hypot(dx, dy) || 1;
  draft.pts.push([round2(last[0] + (dx / d) * len), round2(last[1] + (dy / d) * len)]);
  document.getElementById("len-input").value = "";
  drawPreview(draft.pts[draft.pts.length - 1], false);
  setStatus(`Сегмент ${fmtM(len)} м нэмэгдлээ — чиглэл заагаад дахин урт оруул, эсвэл Enter-ээр дуусга`);
}

stage.on("mousedown", (e) => {
  if (panning) return; // Space-pan → let the stage drag handle it
  const raw = pointerPlan();
  if (tool === "select") {
    // Drag on EMPTY canvas → marquee-select an area (shape clicks are caught by
    // the shape's own handler with cancelBubble).
    if (e.target === stage) startMarquee(raw);
    return;
  }
  if (tool === "camera") { placeCamera(raw); return; }
  // Room tool: drag a rectangle that becomes 4 connected WALLS (store outline).
  // If a width×height (m) is typed, one click drops that exact outline instead.
  if (tool === "room") {
    const dims = rectDims();
    if (dims) placeRoomExact(snapPoint(raw) || raw, dims.w, dims.h);
    else startRect(raw);
    return;
  }
  // Fixtures (тавиур/орц-гарц/касс) are boxes. If a width×height (m) is typed,
  // one click drops that exact box; otherwise just drag a rectangle (no Shift).
  if (FIX[tool]) {
    const dims = rectDims();
    if (dims) placeRectExact(snapPoint(raw) || raw, dims.w, dims.h);
    else startRect(raw);
    return;
  }
  // wall: snap the new vertex (corner-snap → ortho → angle); auto-close near start.
  const prev = draft && draft.pts.length ? draft.pts[draft.pts.length - 1] : null;
  if (draft && draft.pts.length >= 3 && nearStart(raw)) {
    draft.pts.push([...draft.pts[0]]); // close the loop cleanly
    finishDraft();
    return;
  }
  const p = wallPoint(prev, raw);
  if (!draft) draft = { type: "wall", pts: [] };
  draft.pts.push([round2(p[0]), round2(p[1])]);
  drawPreview(raw, false);
  showLenInput();
});

// True if `raw` is within ~12 px of the open wall's start vertex (auto-close).
function nearStart(raw) {
  if (!draft || !draft.pts.length) return false;
  const s = draft.pts[0];
  return Math.hypot(raw[0] - s[0], raw[1] - s[1]) < 12 / (stage.scaleX() || 1);
}

// Read the typed fixture width×height (m), or null if not both > 0.
function rectDims() {
  const w = parseFloat((document.getElementById("rw-input") || {}).value);
  const h = parseFloat((document.getElementById("rh-input") || {}).value);
  return w > 0 && h > 0 ? { w, h } : null;
}
// Drop a fixture rectangle of an EXACT size (metres), top-left at the click.
function placeRectExact(raw, w, h) {
  const x1 = round2(raw[0]), y1 = round2(raw[1]);
  const x2 = round2(x1 + w), y2 = round2(y1 + h);
  PLAN.fixtures.push({ type: tool, points: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]] });
  pushUndo();
  render();
  setStatus(`▦ ${fmtM(w)} × ${fmtM(h)} м нэмэгдлээ`);
}
// Drop a room/outer-wall rectangle of an EXACT size (metres) — 4 connected
// walls (closed loop), top-left at the click. Mirrors finishRect's room branch.
function placeRoomExact(raw, w, h) {
  const x1 = round2(raw[0]), y1 = round2(raw[1]);
  const x2 = round2(x1 + w), y2 = round2(y1 + h);
  PLAN.walls.push({ points: [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]] });
  pushUndo();
  render();
  setStatus(`▭ Хана-дөрвөлжин ${fmtM(w)} × ${fmtM(h)} м нэмэгдлээ`);
}

stage.on("mousemove", () => {
  lastPointer = pointerPlan();
  if (rectDraft) { previewRect(lastPointer); return; }
  if (marquee) { growMarquee(lastPointer); return; }
  if (draft) { drawPreview(lastPointer, false); return; }
  // Not drawing yet: still preview the corner-snap target for the draw tools.
  if (pointSnapOn && (tool === "wall" || tool === "room" || FIX[tool])) {
    const sp = snapPoint(lastPointer);
    if (sp) showSnapMarker(sp, "#22d3ee"); else clearSnapMarker();
    uiLayer.batchDraw();
  } else if (snapMarker) { clearSnapMarker(); uiLayer.batchDraw(); }
});

stage.on("mouseup", () => {
  if (rectDraft) finishRect();
  else if (marquee) finishMarquee();
});

function drawPreview(raw, shift) {
  if (previewLine) previewLine.destroy();
  clearSnapMarker();
  if (!draft) return;
  const color = draft.type === "wall" ? WALL_COLOR : (FIX[draft.type] || {}).color;
  const pts = draft.pts.slice();
  const last = pts[pts.length - 1];
  // Hover vertex + a marker for what it will latch onto.
  let hover, closing = false;
  if (draft.type === "wall" && pts.length >= 3 && nearStart(raw)) {
    hover = pts[0].slice(); closing = true;
    showSnapMarker(hover, "#22c55e"); // green ring = click will CLOSE the loop
  } else {
    hover = draft.type === "wall" ? wallPoint(last, raw) : snapSeg(last, raw, shift);
    const sp = snapPoint(raw);
    if (sp) showSnapMarker(sp, "#22d3ee"); // cyan ring = snapping to a corner
  }
  // Counter-scaled: a fixed 2 plan-unit stroke was 2 METRES thick — a fat
  // ribbon on any real store. Same for the dash rhythm.
  const psx = stage.scaleX() || 1;
  const flat = pts.flat().concat(hover);
  previewLine = new Konva.Line({ points: flat, stroke: color, strokeWidth: 1.6 / psx, dash: [6 / psx, 4 / psx], listening: false });
  uiLayer.add(previewLine);
  // (no per-vertex dots — the dashed preview line + snap ring are enough, and
  //  loose dots were never cleaned up → they lingered after drawing.)
  if (last) {
    const ang = ((Math.atan2(hover[1] - last[1], hover[0] - last[0]) * 180) / Math.PI + 360) % 360;
    const len = Math.hypot(hover[0] - last[0], hover[1] - last[1]);
    setStatus(closing ? `Гогцоо хаах — урт ${fmtM(len)} м` : `∠ ${ang.toFixed(0)}°  ·  урт ${fmtM(len)} м`);
  }
  uiLayer.batchDraw();
}

function finishDraft() {
  if (!draft) return;
  const min = draft.type === "wall" ? 2 : 3;
  if (draft.pts.length < min) { setStatus(`Дор хаяж ${min} цэг`); return; }
  if (draft.type === "wall") PLAN.walls.push({ points: draft.pts });
  else PLAN.fixtures.push({ type: draft.type, points: draft.pts });
  cancelDraft();
  pushUndo();
  render();
}

stage.on("dblclick", () => { if (draft) finishDraft(); });

// ── drag rectangle (fixtures + room walls) ──────────────────────────────────
function startRect(raw) {
  cancelDraft();
  rectDraft = { type: tool, start: snapPoint(raw) || raw }; // corner-snap the start
  setStatus(tool === "room" ? "Хана-дөрвөлжин — чирээд тавь" : "Тэгш өнцөгт — чирээд тавь");
}
function previewRect(rawIn) {
  if (previewLine) previewLine.destroy();
  clearSnapMarker();
  const snapped = snapPoint(rawIn);
  const raw = snapped || rawIn;
  if (snapped) showSnapMarker(raw, "#22d3ee");
  const [x0, y0] = rectDraft.start;
  const isRoom = rectDraft.type === "room";
  const color = isRoom ? WALL_COLOR : ((FIX[rectDraft.type] || {}).color || "#999");
  const rsx = stage.scaleX() || 1; // counter-scaled like drawPreview
  previewLine = new Konva.Line({
    points: [x0, y0, raw[0], y0, raw[0], raw[1], x0, raw[1]],
    stroke: color, strokeWidth: 1.6 / rsx, dash: [6 / rsx, 4 / rsx], closed: true,
    fill: isRoom ? undefined : color + "22", listening: false,
  });
  uiLayer.add(previewLine);
  uiLayer.batchDraw();
  setStatus(`▭ ${fmtM(Math.abs(raw[0] - x0))} × ${fmtM(Math.abs(raw[1] - y0))} м`);
}
function finishRect() {
  const raw = snapPoint(pointerPlan()) || pointerPlan();
  const [x0, y0] = rectDraft.start;
  const t = rectDraft.type;
  cancelRect();
  const w = Math.abs(raw[0] - x0), h = Math.abs(raw[1] - y0);
  if (w < 0.2 || h < 0.2) { setStatus("Хэт жижиг — болилоо"); return; } // < 20 cm
  const x1 = round2(Math.min(x0, raw[0])), y1 = round2(Math.min(y0, raw[1]));
  const x2 = round2(Math.max(x0, raw[0])), y2 = round2(Math.max(y0, raw[1]));
  if (t === "room") {
    // four connected walls (a closed loop) — the store outline in one drag
    PLAN.walls.push({ points: [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]] });
    setStatus(`▭ Хана-дөрвөлжин ${fmtM(w)} × ${fmtM(h)} м нэмэгдлээ`);
  } else {
    PLAN.fixtures.push({ type: t, points: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]] });
    setStatus(`▦ ${fmtM(w)} × ${fmtM(h)} м нэмэгдлээ`);
  }
  pushUndo();
  render();
}
function cancelRect() {
  rectDraft = null;
  clearSnapMarker();
  if (previewLine) { previewLine.destroy(); previewLine = null; uiLayer.batchDraw(); }
}

// ── marquee (area) selection ────────────────────────────────────────────────
function startMarquee(raw) {
  deselect();
  marquee = { start: raw };
}
function growMarquee(raw) {
  if (marqueeRect) marqueeRect.destroy();
  const [x0, y0] = marquee.start;
  marqueeRect = new Konva.Rect({
    x: Math.min(x0, raw[0]), y: Math.min(y0, raw[1]),
    width: Math.abs(raw[0] - x0), height: Math.abs(raw[1] - y0),
    stroke: "#2563EB", strokeWidth: 1 / stage.scaleX(), dash: [4, 3], fill: "#2563EB22", listening: false,
  });
  uiLayer.add(marqueeRect);
  uiLayer.batchDraw();
}
function finishMarquee() {
  const raw = pointerPlan();
  const [x0, y0] = marquee.start;
  cancelMarquee();
  const x1 = Math.min(x0, raw[0]), y1 = Math.min(y0, raw[1]);
  const x2 = Math.max(x0, raw[0]), y2 = Math.max(y0, raw[1]);
  if (x2 - x1 < 4 && y2 - y1 < 4) { deselect(); return; } // a click, not a drag
  deselect();
  const within = (pts) => pts.some(([px, py]) => px >= x1 && px <= x2 && py >= y1 && py <= y2);
  PLAN.fixtures.forEach((f, i) => { if (within(f.points)) marqueeSel.push({ kind: "fixtures", idx: i }); });
  PLAN.walls.forEach((w, i) => { if (within(w.points)) marqueeSel.push({ kind: "walls", idx: i }); });
  PLAN.cameras.forEach((c, i) => {
    const [px, py] = c.pos;
    if (px >= x1 && px <= x2 && py >= y1 && py <= y2) marqueeSel.push({ kind: "cameras", idx: i });
  });
  highlightMarqueeSel();
  setStatus(marqueeSel.length ? `${marqueeSel.length} объект сонгогдлоо — Del товчоор устга` : "Юу ч сонгогдсонгүй");
}
function highlightMarqueeSel() {
  marqueeNodes.forEach((n) => n.destroy());
  marqueeNodes.length = 0;
  marqueeSel.forEach(({ kind, idx }) => {
    const pts = kind === "fixtures" ? PLAN.fixtures[idx].points
      : kind === "walls" ? PLAN.walls[idx].points : [PLAN.cameras[idx].pos];
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const pad = 6 / stage.scaleX();
    marqueeNodes.push(new Konva.Rect({
      x: Math.min(...xs) - pad, y: Math.min(...ys) - pad,
      width: Math.max(...xs) - Math.min(...xs) + 2 * pad, height: Math.max(...ys) - Math.min(...ys) + 2 * pad,
      stroke: "#2563EB", strokeWidth: 1.5 / stage.scaleX(), dash: [4, 3], listening: false,
    }));
  });
  marqueeNodes.forEach((n) => uiLayer.add(n));
  uiLayer.batchDraw();
}
function cancelMarquee() {
  marquee = null;
  if (marqueeRect) { marqueeRect.destroy(); marqueeRect = null; uiLayer.batchDraw(); }
}
function clearMarqueeSel() {
  marqueeSel = [];
  marqueeNodes.forEach((n) => n.destroy());
  marqueeNodes.length = 0;
}

// Ctrl+M / Ctrl+A — select EVERYTHING (walls, fixtures, cameras) as a marquee
// selection, so a whole draft can be deleted (Del) or inspected in one go.
function selectAll() {
  setTool("select");
  deselect();
  PLAN.fixtures.forEach((_f, i) => marqueeSel.push({ kind: "fixtures", idx: i }));
  PLAN.walls.forEach((_w, i) => marqueeSel.push({ kind: "walls", idx: i }));
  PLAN.cameras.forEach((_c, i) => marqueeSel.push({ kind: "cameras", idx: i }));
  highlightMarqueeSel();
  setStatus(marqueeSel.length
    ? `Бүгд сонгогдлоо — ${marqueeSel.length} объект (Del товчоор устгана)`
    : "Сонгох объект алга");
}

// ── delete the current selection (single or marquee) ────────────────────────
function deleteSelection() {
  if (marqueeSel.length) {
    const byKind = {};
    marqueeSel.forEach(({ kind, idx }) => (byKind[kind] = byKind[kind] || []).push(idx));
    Object.entries(byKind).forEach(([kind, idxs]) => {
      idxs.sort((a, b) => b - a).forEach((i) => PLAN[kind].splice(i, 1)); // high→low keeps indices valid
    });
    deselect();
    pushUndo();
    render();
    setStatus("Сонгосон объектууд устгагдлаа");
    return;
  }
  if (selectedNode) {
    PLAN[selectedNode.kind].splice(selectedNode.idx, 1);
    deselect();
    pushUndo();
    render();
  }
}

function placeCamera(raw) {
  const sel = document.getElementById("cam-pick");
  const name = sel.value;
  const cam = cameras.find((c) => c.name === name) || {};
  const cid = cam.camera_id || name;
  const existing = PLAN.cameras.find((c) => c.camera_id === cid);
  if (existing) {
    existing.pos = raw;
    setStatus(`📷 «${name}» аль хэдийн байрлуулсан — шинэ цэг рүү зөөлөө (Ctrl+Z буцаана)`);
  } else {
    PLAN.cameras.push({ camera_id: cid, name: name, pos: raw, dir_deg: 0, homography: null });
  }
  pushUndo();
  render();
}

// ── unsaved-changes (dirty) tracking ────────────────────────────────────────
// Every mutation funnels through pushUndo/undo/redo, so those are the single
// place dirty flips on. The Python side mirrors it (set_dirty) to guard the
// window close; the save button shows a «•» so the state is visible too.
let dirty = false;
function setDirty(on) {
  dirty = on;
  const b = document.getElementById("btn-save");
  if (b) b.textContent = on ? "Хадгалах •" : "Хадгалах";
  try { window.pywebview.api.set_dirty(on); } catch (e) { /* bridge not ready */ }
}

// ── undo / redo ───────────────────────────────────────────────────────────
function snapshot() { return JSON.stringify(PLAN); }
function pushUndo() { undoStack.push(snapshot()); if (undoStack.length > 100) undoStack.shift(); redoStack.length = 0; setDirty(true); }
function undo() {
  if (undoStack.length < 2) return;
  redoStack.push(undoStack.pop());
  PLAN = JSON.parse(undoStack[undoStack.length - 1]);
  setDirty(true);
  relinkCalibCam(); deselect(); render();
}
function redo() {
  if (!redoStack.length) return;
  const s = redoStack.pop(); undoStack.push(s); PLAN = JSON.parse(s); setDirty(true); relinkCalibCam(); deselect(); render();
}
// undo/redo replace PLAN wholesale (fresh camera objects). If a calibration is
// open, re-point calib.cam at the NEW object with the same id, so a later save
// mutates the live camera — not an orphan copy that render() never reflects.
function relinkCalibCam() {
  if (typeof calib !== "undefined" && calib && calib.cam) {
    const c = PLAN.cameras.find((x) => x.camera_id === calib.cam.camera_id);
    if (c) calib.cam = c;
  }
}

// ── element list (DOM) ─────────────────────────────────────────────────────
function renderElements() {
  const el = document.getElementById("elements");
  el.innerHTML = "";
  const rows = [];
  PLAN.cameras.forEach((c, i) => rows.push(["cameras", i, CAM_COLOR, "📷 " + (c.name || c.camera_id)]));
  PLAN.fixtures.forEach((f, i) => rows.push(["fixtures", i, (FIX[f.type] || {}).color || "#999", "▦ " + (f.label || (FIX[f.type] || {}).label || f.type)]));
  PLAN.walls.forEach((_w, i) => rows.push(["walls", i, WALL_COLOR, "▭ Хана " + (i + 1)]));
  if (!rows.length) { el.innerHTML = '<p class="hint">Хоосон</p>'; return; }
  rows.forEach(([kind, idx, color, text]) => {
    const row = document.createElement("div");
    row.className = "elem";
    // Camera rows get a «calibrate» action (Phase B) before the delete button.
    const calib = kind === "cameras"
      ? `<button class="calib" title="Калибровк хийх">📐</button>` : "";
    row.innerHTML = `<span class="dot" style="background:${color}"></span><span class="name" title="Дарж сонгох">${esc(text)}</span>${calib}<button class="del">✕</button>`;
    row.querySelector(".del").onclick = () => { PLAN[kind].splice(idx, 1); deselect(); pushUndo(); render(); };
    if (kind === "cameras") row.querySelector(".calib").onclick = () => startCalibration(idx);
    // Click a row → select that element on the canvas (same as clicking the shape).
    row.querySelector(".name").onclick = () => selectFromList(kind, idx);
    el.appendChild(row);
  });
}

// Select an element from the sidebar list — resolve its live Konva node the
// same way canvas clicks do, so the settings panel/anchors open identically.
function selectFromList(kind, idx) {
  setTool("select");
  if (kind === "cameras") {
    const g = camLayer.getChildren()[idx];
    if (g) selectCamera(g, idx);
    return;
  }
  const singular = kind === "walls" ? "wall" : "fixture";
  let node = null;
  shapeLayer.find("Line").forEach((l) => { if (l.name() === `${singular}:${idx}`) node = l; });
  if (node) selectShape(node, singular, idx);
}

// ── new / clear ────────────────────────────────────────────────────────────
function clearPlan() {
  if (!window.confirm("Бүх зураг (хана, бүс, камер) устгаж шинээр эхлэх үү?")) return;
  PLAN.size = DEFAULT_SIZE_M.slice(); // «Шинээр эхлэх» → анхдагч талбай
  PLAN.walls = [];
  PLAN.fixtures = [];
  PLAN.cameras = [];
  deselect();
  cancelDraft();
  pushUndo();
  render();
  fit();
  setStatus(`Шинэ хоосон зураг (${DEFAULT_SIZE_M[0]} × ${DEFAULT_SIZE_M[1]} м) — зурж эхлээрэй`);
}

// ── camera settings panel (click a camera → its settings + live status) ─────
function hideCameraSettings() {
  const el = document.getElementById("cam-settings");
  if (el) el.classList.add("cs-hidden");
}

function showCameraSettings(idx) {
  const cam = PLAN.cameras[idx];
  const el = document.getElementById("cam-settings");
  if (!cam || !el) return;
  const st = camStatus[cam.camera_id] || {};
  const onlineTxt = st.online === true ? "🟢 Холбогдсон"
    : st.online === false ? "🔴 Холбогдоогүй" : "⚪ Шалгаагүй";
  const calibrated = !!(cam.homography || cam._calibrated);
  const v = calibVerdict(cam.reproj_err);
  const calibTxt = calibrated
    ? `✅ Хийсэн${cam.reproj_err != null ? ` · <b style="color:${v.color}">${v.word}</b> (${(cam.reproj_err * 100).toFixed(1)}%)` : ""}`
    : `<span style="color:#E0A82E">❌ Хийгээгүй — зон үүсэхгүй</span>`;
  el.innerHTML = `
    <h2>📷 ${esc(cam.name || cam.camera_id)}</h2>
    <div class="cs-row">Төлөв: <span id="cs-online">${onlineTxt}</span></div>
    <div class="cs-row">Калибрац: ${calibTxt}</div>
    <div class="cs-row">Чиглэл: ${cam.dir_deg || 0}°</div>
    <div class="cs-btns">
      <button id="cs-calib" class="primary">📐 Калибровк</button>
      <button id="cs-check">🔄 Шалгах</button>
      <button id="cs-remove">🗑 Хасах</button>
    </div>`;
  el.classList.remove("cs-hidden");
  document.getElementById("cs-calib").onclick = () => startCalibration(idx);
  document.getElementById("cs-check").onclick = () => checkCameraStatus(idx);
  document.getElementById("cs-remove").onclick = () => {
    PLAN.cameras.splice(idx, 1); deselect(); pushUndo(); render();
  };
}

async function checkCameraStatus(idx) {
  const cam = PLAN.cameras[idx];
  if (!cam) return;
  const camId = cam.camera_id;
  const onlineEl = document.getElementById("cs-online");
  if (onlineEl) onlineEl.textContent = "⏳ Шалгаж байна…";
  let r = null;
  try { r = await window.pywebview.api.camera_status(camId); } catch (e) { r = null; }
  camStatus[camId] = { online: r && r.ok ? !!r.online : undefined };
  // The camera list may have changed during the await — re-resolve by id so the
  // badge lands on the RIGHT camera (or is skipped if it was deleted), never a
  // shifted index.
  const curIdx = PLAN.cameras.findIndex((c) => c.camera_id === camId);
  if (curIdx < 0) return;
  const camG = camLayer.getChildren()[curIdx];
  if (camG) {
    const old = camG.findOne(".badge");
    if (old) old.destroy();
    const b = makeBadge(PLAN.cameras[curIdx]);
    if (b) camG.add(b);
    camLayer.draw();
  }
  if (selectedNode && selectedNode.kind === "cameras" && selectedNode.idx === curIdx) showCameraSettings(curIdx);
}

// ── Phase B: per-camera homography calibration ──────────────────────────────
// Match ≥4 points between the camera's snapshot and the plan; the Python side
// fits a plan→image homography and turns the plan fixtures into THIS camera's
// zones (which the behaviour engine then uses).
const calib = { cam: null, img: null, plan: null, pairs: [], pendingImg: null, imgFit: null };
const CALIB_COLORS = ["#2563EB", "#3DD56D", "#E0A82E", "#E5484D", "#A855F7", "#06B6D4", "#F97316", "#EC4899"];

function setCalibStatus(t) {
  document.getElementById("calib-status").textContent = t || "";
}

async function startCalibration(camIdx) {
  const cam = PLAN.cameras[camIdx];
  if (!cam) return;
  calib.cam = cam;
  calib.pairs = [];
  calib.pendingImg = null;
  document.getElementById("calib").classList.remove("calib-hidden");
  document.getElementById("calib-title").textContent = "Калибровк — " + (cam.name || cam.camera_id);
  setCalibStatus("Камерын зураг авч байна…");
  let frame = null;
  try {
    frame = await window.pywebview.api.get_camera_frame(cam.camera_id);
  } catch (e) { frame = { ok: false, error: String(e) }; }
  if (!frame || !frame.ok) {
    setCalibStatus("❌ " + ((frame && frame.error) || "Зураг авч чадсангүй"));
    return;
  }
  buildCalibStages(frame);
}

function buildCalibStages(frame) {
  // Camera image pane — letterbox the snapshot into the pane; clicks → 0-1 coords.
  const camHolder = document.getElementById("calib-cam");
  const planHolder = document.getElementById("calib-plan");
  if (calib.img) calib.img.destroy();
  if (calib.plan) calib.plan.destroy();

  const cw = camHolder.clientWidth, ch = camHolder.clientHeight;
  calib.img = new Konva.Stage({ container: "calib-cam", width: cw, height: ch });
  const imgLayer = new Konva.Layer();
  calib.img.add(imgLayer);
  const imageObj = new Image();
  imageObj.onload = () => {
    const iw = imageObj.naturalWidth, ih = imageObj.naturalHeight;
    const sc = Math.min(cw / iw, ch / ih);
    const dw = iw * sc, dh = ih * sc, ox = (cw - dw) / 2, oy = (ch - dh) / 2;
    calib.imgFit = { ox, oy, dw, dh };
    calib.aspect = ih > 0 ? iw / ih : null; // frame w/h → Python 3D pose (solvePnP intrinsics)
    imgLayer.add(new Konva.Image({ image: imageObj, x: ox, y: oy, width: dw, height: dh }));
    // Derived-zone preview UNDER the point marks: the operator sees exactly
    // where the plan fixtures land on this camera before saving.
    calib.zoneMarks = new Konva.Group();
    imgLayer.add(calib.zoneMarks);
    calib.imgMarks = new Konva.Group();
    imgLayer.add(calib.imgMarks);
    imgLayer.draw();
  };
  imageObj.src = frame.image;
  calib.img.on("mousedown", () => {
    const p = calib.img.getPointerPosition();
    const f = calib.imgFit;
    if (!f) return;
    const nx = (p.x - f.ox) / f.dw, ny = (p.y - f.oy) / f.dh;
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) { setCalibStatus("Зурагнаас гадуур — зураг дотор дар"); return; }
    calib.pendingImg = [+nx.toFixed(4), +ny.toFixed(4)];
    redrawCalibMarks();
    setCalibStatus("Одоо планы таарах цэгийг дар →");
  });

  // Plan pane — render the plan read-only, fit; clicks → plan coords.
  const pw = planHolder.clientWidth, ph = planHolder.clientHeight;
  calib.plan = new Konva.Stage({ container: "calib-plan", width: pw, height: ph });
  const planLayer = new Konva.Layer();
  calib.plan.add(planLayer);
  const [PW, PH] = PLAN.size;
  const pz = Math.min(pw / PW, ph / PH) * 0.92;
  calib.plan.scale({ x: pz, y: pz });
  calib.plan.position({ x: (pw - PW * pz) / 2, y: (ph - PH * pz) / 2 });
  const planShapes = []; // outlines to counter-scale on zoom (stay ~2px on screen)
  PLAN.walls.forEach((w) => {
    const l = new Konva.Line({ points: w.points.flat(), stroke: WALL_COLOR, strokeWidth: 2 / pz });
    planShapes.push(l);
    planLayer.add(l);
  });
  PLAN.fixtures.forEach((f) => {
    const c = (FIX[f.type] || {}).color || "#999";
    const l = new Konva.Line({ points: f.points.flat(), stroke: c, strokeWidth: 2 / pz, closed: true, fill: c + "22" });
    planShapes.push(l);
    planLayer.add(l);
  });
  calib.planMarks = new Konva.Group();
  planLayer.add(calib.planMarks);
  planLayer.draw();
  // Same wheel language as the main canvas: wheel scrolls, Shift+wheel sideways,
  // Ctrl+wheel (trackpad pinch) zooms about the cursor. Precise pair-clicking on
  // a big store needs to get CLOSE — a fitted 40 m plan makes 1 px ≈ 8 cm.
  calib.plan.on("wheel", (e) => {
    e.evt.preventDefault();
    if (!(e.evt.ctrlKey || e.evt.metaKey)) {
      const dx = e.evt.shiftKey && !e.evt.deltaX ? e.evt.deltaY : e.evt.deltaX;
      const dy = e.evt.shiftKey ? 0 : e.evt.deltaY;
      calib.plan.position({ x: calib.plan.x() - dx, y: calib.plan.y() - dy });
      return;
    }
    const old = calib.plan.scaleX();
    const pointer = calib.plan.getPointerPosition();
    if (!pointer) return;
    const to = { x: (pointer.x - calib.plan.x()) / old, y: (pointer.y - calib.plan.y()) / old };
    const ns = Math.max(pz * 0.5, Math.min(pz * 40, old * (e.evt.deltaY > 0 ? 1 / 1.15 : 1.15)));
    calib.plan.scale({ x: ns, y: ns });
    calib.plan.position({ x: pointer.x - to.x * ns, y: pointer.y - to.y * ns });
    planShapes.forEach((l) => l.strokeWidth(2 / ns));
    redrawCalibMarks(); // point marks re-derive their size from the new scale
  });
  calib.plan.on("mousedown", () => {
    if (!calib.pendingImg) { setCalibStatus("Эхлээд камерын зураг дээр цэг дар ←"); return; }
    const p = calib.plan.getRelativePointerPosition();
    // 2 dp = 1 cm — anchor points at 0.1 m (1 dp) were coarse enough to inflate
    // the homography's reprojection error on small stores.
    calib.pairs.push({ image: calib.pendingImg, plan: [round2(p.x), round2(p.y)] });
    calib.pendingImg = null;
    redrawCalibMarks();
    refreshCalibPreview();
  });
  setCalibStatus("1) Камерын зураг дээр танигдах цэг дар → 2) планы таарах цэгийг дар. ≥4 хослол.");
}

function _mark(group, x, y, n, scaleInv) {
  const color = CALIB_COLORS[(n - 1) % CALIB_COLORS.length];
  const r = 7 * (scaleInv || 1);
  group.add(new Konva.Circle({ x, y, radius: r, stroke: color, strokeWidth: 2 * (scaleInv || 1), fill: color + "55" }));
  group.add(new Konva.Text({ x: x + r, y: y - r, text: String(n), fontSize: 13 * (scaleInv || 1), fontStyle: "bold", fill: color }));
}

function redrawCalibMarks() {
  if (calib.imgMarks) {
    calib.imgMarks.destroyChildren();
    const f = calib.imgFit;
    calib.pairs.forEach((pr, i) => _mark(calib.imgMarks, f.ox + pr.image[0] * f.dw, f.oy + pr.image[1] * f.dh, i + 1));
    if (calib.pendingImg) _mark(calib.imgMarks, f.ox + calib.pendingImg[0] * f.dw, f.oy + calib.pendingImg[1] * f.dh, calib.pairs.length + 1);
    calib.imgMarks.getLayer().batchDraw();
  }
  if (calib.planMarks) {
    calib.planMarks.destroyChildren();
    const inv = 1 / calib.plan.scaleX();
    calib.pairs.forEach((pr, i) => _mark(calib.planMarks, pr.plan[0], pr.plan[1], i + 1, inv));
    calib.planMarks.getLayer().batchDraw();
  }
}

function undoCalibPoint() {
  if (calib.pendingImg) calib.pendingImg = null;
  else calib.pairs.pop();
  redrawCalibMarks();
  refreshCalibPreview();
}

// ── live zone preview ────────────────────────────────────────────────────────
// From the 4th point pair on, every added/removed pair re-fits the homography
// (dry-run, nothing saved) and paints the DERIVED zones straight onto the
// camera snapshot — "does the касс polygon actually sit on the касс?" is
// answered by eye before Хадгалах, which used to require a blind save.
async function refreshCalibPreview() {
  const n = calib.pairs.length;
  if (n < 4) {
    // Invalidate any in-flight ≥4-pair preview too — otherwise its late
    // response would repaint stale zones right after an undo below 4 pairs.
    calib.previewSeq = (calib.previewSeq || 0) + 1;
    if (calib.zoneMarks) { calib.zoneMarks.destroyChildren(); calib.zoneMarks.getLayer().batchDraw(); }
    setCalibStatus(`${n} цэг хослол (≥4 хэрэгтэй)`);
    return;
  }
  const seq = (calib.previewSeq = (calib.previewSeq || 0) + 1);
  let r = null;
  try { r = await window.pywebview.api.preview_calibration(calib.pairs, PLAN, calib.cam.camera_id, calib.aspect || null); }
  catch (e) { r = { ok: false, error: String(e) }; }
  if (seq !== calib.previewSeq || !calib.zoneMarks) return; // stale response / closed
  calib.zoneMarks.destroyChildren();
  if (!r || !r.ok) {
    calib.zoneMarks.getLayer().batchDraw();
    setCalibStatus(`${n} цэг хослол · ⚠ ${(r && r.error) || "урьдчилан харуулж чадсангүй"}`);
    return;
  }
  const f = calib.imgFit;
  (r.zones || []).forEach((z) => {
    const color = (FIX[z.type] || {}).color || "#999";
    const flat = [];
    z.points.forEach(([zx, zy]) => { flat.push(f.ox + zx * f.dw, f.oy + zy * f.dh); });
    calib.zoneMarks.add(new Konva.Line({
      points: flat, closed: true, stroke: color, strokeWidth: 2,
      fill: color + "33", dash: [6, 4], listening: false,
    }));
  });
  calib.zoneMarks.getLayer().batchDraw();
  const v = calibVerdict(r.reproj_err);
  const zn = (r.zones || []).length;
  setCalibStatus(
    `${n} цэг · алдаа ${(r.reproj_err * 100).toFixed(1)}% — ${v.word}` +
    (zn ? ` · ${zn} зон зураг дээр буув — байрлал таарч байвал Хадгал` : " · энэ камерт харагдах зон алга") +
    (v.hint ? ` (${v.hint})` : ""),
  );
}

function closeCalibration() {
  document.getElementById("calib").classList.add("calib-hidden");
  if (calib.img) { calib.img.destroy(); calib.img = null; }
  if (calib.plan) { calib.plan.destroy(); calib.plan = null; }
  calib.pairs = []; calib.pendingImg = null;
  calib.zoneMarks = null; calib.previewSeq = (calib.previewSeq || 0) + 1; // drop in-flight previews
}

async function saveCalibration() {
  if (calib.pairs.length < 4) { setCalibStatus("Дор хаяж 4 цэг хослол хэрэгтэй"); return; }
  setCalibStatus("Хадгалж байна…");
  try {
    const r = await window.pywebview.api.save_calibration(calib.cam.camera_id, calib.pairs, PLAN, calib.aspect || null);
    const errPct = (r.reproj_err * 100).toFixed(1);
    const v = calibVerdict(r.reproj_err);
    setCalibStatus(`✅ ${r.zone_count} зон · алдаа ${errPct}% — ${v.word}${v.hint ? ` (${v.hint})` : ""}`);
    setStatus(`Калибровк: ${r.zone_count} зон, чанар ${v.word}`);
    // Mark calibrated so the camera badge + settings panel update.
    calib.cam._calibrated = true;
    calib.cam.reproj_err = r.reproj_err;
    // save_calibration persisted the whole PLAN we passed (the overlay blocks
    // canvas edits meanwhile), so the unsaved-changes flag stands down too.
    setDirty(false);
    render();
    setTimeout(closeCalibration, 1400);
  } catch (e) {
    setCalibStatus("❌ " + e);
  }
}

// A ready-made EDITABLE starter plan (real walls/fixtures the user can use as-is
// or tweak) — not a background image. Cameras stay as the user placed them.
// Layout = a tidy convenience-store: outer wall, a back-wall display + two side
// perimeter shelves, three centre gondola rows with even 80px aisles between
// them, an Орц/Гарц door bottom-left and two checkout counters bottom-right
// (so the shopper's path is enter ▸ aisles ▸ checkout ▸ exit).
const TEMPLATE = {
  walls: [{ points: [[60, 60], [940, 60], [940, 740], [60, 740], [60, 60]] }],
  fixtures: [
    // entrance / exit door threshold (bottom-left)
    { type: "exit", points: [[110, 700], [290, 700], [290, 740], [110, 740]] },
    // checkout counters by the exit (bottom-right)
    { type: "checkout", points: [[600, 630], [740, 630], [740, 700], [600, 700]] },
    { type: "checkout", points: [[770, 630], [900, 630], [900, 700], [770, 700]] },
    // back-wall display run (top)
    { type: "shelf", points: [[120, 110], [880, 110], [880, 170], [120, 170]] },
    // left + right perimeter shelves
    { type: "shelf", points: [[80, 230], [150, 230], [150, 600], [80, 600]] },
    { type: "shelf", points: [[850, 230], [920, 230], [920, 560], [850, 560]] },
    // three centre gondola rows (even aisles)
    { type: "shelf", points: [[230, 250], [770, 250], [770, 310], [230, 310]] },
    { type: "shelf", points: [[230, 390], [770, 390], [770, 450], [230, 450]] },
    { type: "shelf", points: [[230, 530], [680, 530], [680, 590], [230, 590]] },
  ],
};
function loadTemplate() {
  // Replaces the drawn walls/fixtures — irreversible except via undo, so ask
  // first when there is anything to lose (a blank canvas loads silently).
  if ((PLAN.walls.length || PLAN.fixtures.length) &&
      !window.confirm("Жишээ загвар таны зурсан хана/бүсийг ДАРЖ бичнэ (камерууд хэвээр). Үргэлжлүүлэх үү?")) return;
  // A ~44×34 m store on a matching 50×40 m canvas (template authored in
  // 1000×800 units spanning ~60..940 / 60..740; K scales to metres).
  const K = 0.05, OX = 0, OY = 0;
  const sc = (pts) => pts.map(([x, y]) => [round2(x * K + OX), round2(y * K + OY)]);
  PLAN.size = [50, 40];
  PLAN.walls = TEMPLATE.walls.map((w) => ({ points: sc(w.points) }));
  PLAN.fixtures = TEMPLATE.fixtures.map((f) => ({ type: f.type, points: sc(f.points) }));
  // keep PLAN.cameras — the user's placed cameras are theirs
  deselect();
  pushUndo();
  render();
  fit();
  setStatus("Жишээ загвар — 44×34 м дэлгүүр (50×40 м талбайд). Өөрийн дэлгүүрт тааруулж засаарай.");
}

// ── fit ─────────────────────────────────────────────────────────────────────
// Bounding box of everything DRAWN (walls/fixtures/cameras), or null when blank.
function contentBBox() {
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity, any = false;
  const eat = (p) => { any = true; x1 = Math.min(x1, p[0]); y1 = Math.min(y1, p[1]); x2 = Math.max(x2, p[0]); y2 = Math.max(y2, p[1]); };
  PLAN.walls.forEach((w) => w.points.forEach(eat));
  PLAN.fixtures.forEach((f) => f.points.forEach(eat));
  PLAN.cameras.forEach((c) => eat(c.pos));
  return any ? { x: x1, y: y1, w: Math.max(x2 - x1, 0.5), h: Math.max(y2 - y1, 0.5) } : null;
}

// Fit the DRAWN store, not the whole canvas: a 10×10 m store on a big canvas
// used to shrink to a speck. Padded 10% (min 1 m); a blank plan fits the canvas.
function fit() {
  const b = contentBBox();
  let x, y, w, h;
  if (b) {
    const pad = Math.max(Math.min(b.w, b.h) * 0.1, 1);
    x = b.x - pad; y = b.y - pad; w = b.w + 2 * pad; h = b.h + 2 * pad;
  } else {
    x = 0; y = 0; [w, h] = PLAN.size;
  }
  const z = Math.min(stage.width() / w, stage.height() / h) * 0.95;
  stage.scale({ x: z, y: z });
  stage.position({ x: (stage.width() - w * z) / 2 - x * z, y: (stage.height() - h * z) / 2 - y * z });
  redrawShapes(); // recompute counter-scaled labels/strokes at the new scale
}

// (deselect on empty is handled by the marquee — a click = a zero-size marquee)

// ── toolbar wiring ────────────────────────────────────────────────────────
document.querySelectorAll(".tool").forEach((b) => (b.onclick = () => setTool(b.dataset.tool)));
document.getElementById("btn-del").onclick = deleteSelection;
document.getElementById("btn-new").onclick = clearPlan;
document.getElementById("btn-example").onclick = loadTemplate;
document.getElementById("btn-fit").onclick = fit;
document.getElementById("btn-undo").onclick = undo;
document.getElementById("btn-redo").onclick = redo;
document.getElementById("btn-save").onclick = save;
document.getElementById("btn-snap").onclick = () => {
  snapOn = !snapOn;
  document.getElementById("btn-snap").classList.toggle("active", snapOn);
  setStatus("Өнцөг-snap " + (snapOn ? "ON" : "OFF"));
};
function togglePointSnap() {
  pointSnapOn = !pointSnapOn;
  const b = document.getElementById("btn-psnap");
  if (b) b.classList.toggle("active", pointSnapOn);
  if (!pointSnapOn) { clearSnapMarker(); uiLayer.batchDraw(); }
  setStatus("Цэг-snap " + (pointSnapOn ? "ON" : "OFF"));
}
function toggleOrtho() {
  orthoOn = !orthoOn;
  const b = document.getElementById("btn-ortho");
  if (b) b.classList.toggle("active", orthoOn);
  setStatus("Босоо/хэвтээ түгжээ " + (orthoOn ? "ON" : "OFF"));
}
function toggleCoverage() {
  coverageOn = !coverageOn;
  const b = document.getElementById("btn-coverage");
  if (b) b.classList.toggle("active", coverageOn);
  const pct = renderCoverage();
  updateCoverageStat(pct);
  setStatus(coverageOn
    ? (PLAN.cameras.length ? `Камерын хамрах талбай: ${pct}% (🔴 = сохор бүс)` : "Эхлээд камер байрлуулна уу")
    : "Хамрах талбай OFF");
}
{
  const bp = document.getElementById("btn-psnap");
  if (bp) bp.onclick = togglePointSnap;
  const bo = document.getElementById("btn-ortho");
  if (bo) bo.onclick = toggleOrtho;
  const bc = document.getElementById("btn-coverage");
  if (bc) bc.onclick = toggleCoverage;
}
document.getElementById("calib-save").onclick = saveCalibration;
document.getElementById("calib-cancel").onclick = closeCalibration;
document.getElementById("calib-undo").onclick = undoCalibPoint;
// Length input: Enter drops the next wall vertex at the typed distance.
document.getElementById("len-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    // a length → drop a segment; empty Enter → finish the wall (matches the hint)
    if (parseFloat(e.target.value) > 0) applyLenInput();
    else if (draft) { e.target.blur(); finishDraft(); }
  } else if (e.key === "Escape") { e.target.blur(); }
  e.stopPropagation(); // don't let digits hit the tool shortcuts below
});

// Plan real-size (W×H metres) controls.
const planApply = () =>
  setPlanSize(parseFloat(document.getElementById("plan-w").value), parseFloat(document.getElementById("plan-h").value));
const planBtn = document.getElementById("plan-apply");
if (planBtn) planBtn.onclick = planApply;
["plan-w", "plan-h"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); planApply(); } e.stopPropagation(); });
});

// ── keyboard shortcuts ──────────────────────────────────────────────────────
const TOOL_KEYS = ["select", "wall", "room", "shelf", "exit", "checkout", "fridge", "furniture", "camera"];
window.addEventListener("keydown", (e) => {
  // Typing in an input (e.g. length) must not trigger tool shortcuts.
  if (e.target && e.target.tagName === "INPUT") return;
  if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (e.ctrlKey && (e.key.toLowerCase() === "m" || e.key.toLowerCase() === "a")) {
    e.preventDefault(); selectAll(); return;
  }
  // While drawing a wall, typing a digit/decimal opens the length box pre-filled,
  // so you can set the length without reaching for the toolbar.
  if (draft && draft.type === "wall" && /^[0-9.]$/.test(e.key)) {
    const i = document.getElementById("len-input");
    if (i) { showLenInput(); i.value = e.key; i.focus(); e.preventDefault(); return; }
  }
  if (e.key >= "1" && e.key <= "9") setTool(TOOL_KEYS[+e.key - 1]);
  else if (e.key === "Enter") finishDraft();
  else if (e.key === "Escape") { cancelDraft(); cancelRect(); cancelMarquee(); deselect(); }
  else if (e.key === "Backspace" && draft) { draft.pts.pop(); if (!draft.pts.length) cancelDraft(); else drawPreview(draft.pts[draft.pts.length - 1], false); }
  else if (e.key === "Delete" || e.key === "Backspace") deleteSelection();
  else if (e.key.toLowerCase() === "g") document.getElementById("btn-snap").click();
  else if (e.key.toLowerCase() === "p") togglePointSnap();
  else if (e.key.toLowerCase() === "o") toggleOrtho();
  else if (e.key.toLowerCase() === "v") toggleCoverage();
  else if (e.key.toLowerCase() === "f") fit();
  else if (e.key === " " && !panning) { e.preventDefault(); panning = true; stage.draggable(true); stage.container().style.cursor = "grab"; }
});

window.addEventListener("keyup", (e) => {
  if (e.key === " ") {
    panning = false;
    stage.draggable(false);
    stage.container().style.cursor = tool === "select" ? "default" : "crosshair";
  }
});

window.addEventListener("resize", () => {
  stage.size({ width: holder.clientWidth, height: holder.clientHeight });
  redrawShapes(); // includes drawGrid + coverage, so overlays track the new size
});

// ── load / save via pywebview bridge ───────────────────────────────────────
async function save() {
  if (draft && draft.pts.length >= (draft.type === "wall" ? 2 : 3)) finishDraft();
  setStatus("Хадгалж байна…");
  const saved = snapshot(); // what actually went to the backend
  try {
    await window.pywebview.api.save_plan(PLAN);
    // Only stand the dirty flag down if nothing changed DURING the await —
    // an edit made mid-save must survive as unsaved.
    if (snapshot() === saved) setDirty(false);
    else try { window.pywebview.api.set_dirty(true); } catch (e) { /* keep JS state */ }
    // Save always succeeds (WIP is fine), but flag a not-yet-protected setup.
    const cams = PLAN.cameras;
    const uncal = cams.filter((c) => !c.homography).length;
    setStatus(uncal
      ? `✅ Хадгалагдлаа — ⚠ ${uncal} камер калибровкгүй (зон үүсэхгүй)`
      : "✅ Хадгалагдлаа");
  } catch (err) {
    setStatus("❌ " + err);
  }
}

async function boot() {
  try {
    cameras = (await window.pywebview.api.list_cameras()) || [];
  } catch { cameras = []; }
  const sel = document.getElementById("cam-pick");
  sel.innerHTML = "";
  (cameras.length ? cameras : [{ name: "—" }]).forEach((c) => {
    const o = document.createElement("option");
    o.value = c.name; o.textContent = c.name; sel.appendChild(o);
  });
  try {
    const loaded = await window.pywebview.api.load_plan();
    if (loaded && typeof loaded === "object") PLAN = normalize(loaded);
  } catch { /* empty plan */ }
  undoStack.length = 0; undoStack.push(snapshot());
  setDirty(false); // freshly loaded = nothing to lose yet
  // Re-measure the canvas now that the window is shown + laid out — if the stage
  // was created before layout (0×0), pan/draw coords would be broken until a
  // resize. boot() runs on pywebviewready (window visible), so the holder is sized.
  if (holder.clientWidth && holder.clientHeight) {
    stage.size({ width: holder.clientWidth, height: holder.clientHeight });
  }
  render(); fit();
  setTool("select");
}

function normalize(p) {
  const out = {
    version: p.version || 1,
    size: p.size && p.size.length === 2 ? p.size : DEFAULT_SIZE_M.slice(),
    walls: (p.walls || []).map((w) => ({
      points: w.points || [],
      height_m: typeof w.height_m === "number" && isFinite(w.height_m) && w.height_m > 0 ? w.height_m : null,
    })),
    // height_m: the EFFECTIVE height (explicit or type default) so the Python
    // 3D calibration sees it without duplicating the FIX defaults table.
    fixtures: (p.fixtures || []).map((f) => ({
      id: f.id, type: f.type, label: f.label || null, points: f.points || [],
      height_m: fixHeight(f),
    })),
    cameras: (p.cameras || []).map((c) => ({
      camera_id: c.camera_id, name: c.name, pos: c.pos || [0, 0],
      dir_deg: c.dir_deg || 0, homography: c.homography || null,
      reproj_err: c.reproj_err, calib_points: c.calib_points,
    })),
  };
  // Units migration: stores created before the metre pivot (v0.7.66) got the
  // old backend default 1000×800 "relative units" canvas; briefly the metre
  // default was 200×200 (too big for a ~10×10 m store). If NOTHING was ever
  // drawn, quietly start on the current default.
  const legacy = (out.size[0] === 1000 && out.size[1] === 800) ||
    (out.size[0] === 200 && out.size[1] === 200);
  if (!out.walls.length && !out.fixtures.length && !out.cameras.length && legacy) {
    out.size = DEFAULT_SIZE_M.slice();
  }
  return out;
}

// pywebview injects the api asynchronously; wait for it.
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
