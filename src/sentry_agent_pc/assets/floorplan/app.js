/* Floor-plan editor (docs/30) — Konva canvas in a pywebview window.
 *
 * Coordinate model: shapes store points in PLAN-logical coords; the Konva stage's
 * scale/position provides zoom/pan, so stage.getRelativePointerPosition() returns
 * plan coords directly. The Python side (window.pywebview.api) bridges to the
 * backend (load/save plan, list cameras, pick a background image) — the agent JWT
 * never touches JS. Homography + live dots come in Phase B / C. */
"use strict";

const FIX = {
  exit: { color: "#E5484D", label: "Орц/Гарц" },
  shelf: { color: "#3DD56D", label: "Тавиур" },
  checkout: { color: "#E0A82E", label: "Касс" },
};
const WALL_COLOR = "#9CA3AF";
const CAM_COLOR = "#2563EB"; // brand royal-blue (the camera is the Sentry element)
const SNAP_DEG = 15;
const ROT_SNAPS = [0, 45, 90, 135, 180, 225, 270, 315];

// ── real-world scale ────────────────────────────────────────────────────────
// 1 plan-unit == 1 METRE. PLAN.size therefore IS the store's real width × height
// in metres (default 200×200 m), so lengths are entered/shown directly in metres.
const DEFAULT_SIZE_M = [200, 200];
const GRID_MINOR_M = 5; // faint grid line every 5 m
const GRID_MAJOR_M = 25; // brighter, labelled grid line every 25 m
const COORD_DP = 2; // store coords to 2 dp → 1 cm precision
const SHOW_AREA = true; // show m² on fixtures + a store-area total

const round2 = (v) => Math.round(v * 100) / 100;
const fmtM = (u) => round2(u).toFixed(COORD_DP); // "12.50"
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
const camLayer = new Konva.Layer();
const uiLayer = new Konva.Layer();
stage.add(gridLayer, shapeLayer, camLayer, uiLayer);

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

// ── zoom / pan ──────────────────────────────────────────────────────────
stage.on("wheel", (e) => {
  e.evt.preventDefault();
  const old = stage.scaleX();
  const pointer = stage.getPointerPosition();
  const to = { x: (pointer.x - stage.x()) / old, y: (pointer.y - stage.y()) / old };
  const ns = Math.max(0.05, Math.min(20, old * (e.evt.deltaY > 0 ? 1 / 1.1 : 1.1)));
  stage.scale({ x: ns, y: ns });
  stage.position({ x: pointer.x - to.x * ns, y: pointer.y - to.y * ns });
  drawGrid();
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
  // Show the fixture width×height inputs only in a fixture tool.
  const rw = document.getElementById("rect-wrap");
  if (rw) rw.classList.toggle("len-hidden", !FIX[t]);
  stage.container().style.cursor = t === "select" ? "default" : "crosshair";
}

// Toggle draggability of every placed shape/camera (not labels/grid/preview).
function setShapesDraggable(on) {
  shapeLayer.find("Line").forEach((n) => n.draggable(on));
  camLayer.find("Group").forEach((n) => n.draggable(on));
}

// ── render plan → Konva ─────────────────────────────────────────────────
function render() {
  shapeLayer.destroyChildren();
  camLayer.destroyChildren();
  PLAN.walls.forEach((w, i) => shapeLayer.add(makeLine(w.points, WALL_COLOR, false, "wall", i)));
  PLAN.fixtures.forEach((f, i) =>
    shapeLayer.add(makeLine(f.points, (FIX[f.type] || {}).color || "#999", true, "fixture", i, (FIX[f.type] || {}).label)),
  );
  PLAN.cameras.forEach((c, i) => camLayer.add(makeCamera(c, i)));
  shapeLayer.draw();
  camLayer.draw();
  drawGrid();
  renderElements();
  renderTotals();
  // Nodes are recreated non-draggable; restore one-gesture move in select mode.
  setShapesDraggable(tool === "select");
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
    strokeWidth: 2,
    closed: closed,
    fill: closed ? color + "22" : undefined,
    draggable: false,
    name: `${kind}:${idx}`,
    hitStrokeWidth: 12,
  });
  line.on("mousedown", (e) => {
    if (tool === "select") {
      e.cancelBubble = true;
      selectShape(line, kind, idx);
    }
  });
  if (label && pts.length) {
    const t = new Konva.Text({
      x: pts[0][0] + 4, y: pts[0][1] - 16 / stage.scaleX(), text: label,
      fontSize: 13 / stage.scaleX(), fontStyle: "bold", fill: color, listening: false,
    });
    line._label = t;
    shapeLayer.add(t);
  }
  addDimLabels(pts, color, kind);
  return line;
}

// Always-on measurement labels (metres): per-segment length for walls; a centred
// «W × H м» + area «… м²» for fixtures. Counter-scaled so they read ~constant on
// screen; rebuilt every render(), so no manual cleanup.
function addDimLabels(pts, color, kind) {
  const fs = 11 / stage.scaleX();
  const mk = (x, y, text, opts) => shapeLayer.add(new Konva.Text(Object.assign(
    { x, y, text, fontSize: fs, fill: "#cbd5e1", listening: false, fontStyle: "bold" }, opts || {})));
  if (kind === "wall") {
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (len < 1e-6) continue;
      // nudge the label just off the segment midpoint (perpendicular)
      const nx = -(b[1] - a[1]) / len, ny = (b[0] - a[0]) / len;
      mk((a[0] + b[0]) / 2 + nx * fs * 0.6, (a[1] + b[1]) / 2 + ny * fs * 0.6, `${fmtM(len)} м`, { fill: "#e2e8f0" });
    }
  } else {
    const b = bbox(pts);
    const lines = [`${fmtM(b.w)} × ${fmtM(b.h)} м`];
    if (SHOW_AREA) lines.push(`${fmtM(polyArea(pts))} м²`);
    const t = new Konva.Text({
      text: lines.join("\n"), fontSize: fs, fill: color, fontStyle: "bold",
      align: "center", lineHeight: 1.15, listening: false,
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
  g.on("mousedown", (e) => {
    if (tool === "select") {
      e.cancelBubble = true;
      selectCamera(g, idx);
    }
  });
  g.on("dragend", () => {
    PLAN.cameras[idx].pos = [g.x(), g.y()];
    pushUndo();
  });
  g.on("transformend", () => {
    PLAN.cameras[idx].dir_deg = Math.round(g.rotation()) % 360;
    label.rotation(-g.rotation());
    setStatus(`Чиглэл ${PLAN.cameras[idx].dir_deg}°`);
    pushUndo();
  });
  return g;
}

function drawGrid() {
  gridLayer.destroyChildren();
  const [pw, ph] = PLAN.size;
  const sx = stage.scaleX() || 1;
  // Too many minor lines (a very large plan) → drop them, keep the major grid.
  const minor = pw / GRID_MINOR_M > 600 || ph / GRID_MINOR_M > 600 ? GRID_MAJOR_M : GRID_MINOR_M;
  const isMajor = (v) => Math.abs(v % GRID_MAJOR_M) < 1e-6 || Math.abs((v % GRID_MAJOR_M) - GRID_MAJOR_M) < 1e-6;
  const fs = 10 / sx;
  for (let x = 0; x <= pw + 1e-6; x += minor) {
    const major = isMajor(x);
    gridLayer.add(new Konva.Line({ points: [x, 0, x, ph], stroke: major ? "#2b2b33" : "#191920", strokeWidth: (major ? 1.2 : 1) / sx, listening: false }));
    if (major && x > 0) gridLayer.add(new Konva.Text({ x: x + 2 / sx, y: 2 / sx, text: String(Math.round(x)), fontSize: fs, fill: "#52525b", listening: false }));
  }
  for (let y = 0; y <= ph + 1e-6; y += minor) {
    const major = isMajor(y);
    gridLayer.add(new Konva.Line({ points: [0, y, pw, y], stroke: major ? "#2b2b33" : "#191920", strokeWidth: (major ? 1.2 : 1) / sx, listening: false }));
    if (major && y > 0) gridLayer.add(new Konva.Text({ x: 2 / sx, y: y + 2 / sx, text: String(Math.round(y)), fontSize: fs, fill: "#52525b", listening: false }));
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
  const nice = [0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500];
  let m = nice[0];
  for (const n of nice) { if (n * sx <= 130) m = n; }
  bar.style.width = Math.round(m * sx) + "px";
  bar.textContent = m >= 1 ? `${m} м` : `${m * 100} см`;
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
      if (line._label) line._label.position({ x: arr[0][0] + 4, y: arr[0][1] - 16 / stage.scaleX() });
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
      `<div class="ss-row">өргөн (м)<input id="ss-w" type="number" min="0.1" step="0.01" value="${fmtM(b.w)}"></div>` +
      `<div class="ss-row">өндөр (м)<input id="ss-h" type="number" min="0.1" step="0.01" value="${fmtM(b.h)}"></div>` +
      `<div class="ss-muted">Талбай: ${fmtM(polyArea(f.points))} м²</div>` +
      `<button id="ss-apply" class="primary">Хэмжээ тавих</button>`;
    el.classList.remove("cs-hidden");
    const apply = () => {
      const w = parseFloat(document.getElementById("ss-w").value);
      const h = parseFloat(document.getElementById("ss-h").value);
      if (w > 0 && h > 0) resizeFixture(idx, w, h);
    };
    document.getElementById("ss-apply").onclick = apply;
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
  // Fixtures (тавиур/орц-гарц/касс) are boxes. If a width×height (m) is typed,
  // one click drops that exact box; otherwise just drag a rectangle (no Shift).
  if (FIX[tool]) {
    const dims = rectDims();
    if (dims) placeRectExact(raw, dims.w, dims.h);
    else startRect(raw);
    return;
  }
  // wall: add a (snapped) polyline vertex; offer numeric length for the next one.
  const prev = draft && draft.pts.length ? draft.pts[draft.pts.length - 1] : null;
  const p = snapSeg(prev, raw, false);
  if (!draft) draft = { type: tool, pts: [] };
  draft.pts.push([round2(p[0]), round2(p[1])]);
  drawPreview(raw, false);
  showLenInput();
});

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

stage.on("mousemove", () => {
  lastPointer = pointerPlan();
  if (rectDraft) { previewRect(lastPointer); return; }
  if (marquee) { growMarquee(lastPointer); return; }
  if (!draft) return;
  drawPreview(lastPointer, false);
});

stage.on("mouseup", () => {
  if (rectDraft) finishRect();
  else if (marquee) finishMarquee();
});

function drawPreview(raw, shift) {
  if (previewLine) previewLine.destroy();
  if (!draft) return;
  const color = draft.type === "wall" ? WALL_COLOR : (FIX[draft.type] || {}).color;
  const pts = draft.pts.slice();
  const last = pts[pts.length - 1];
  const hover = snapSeg(last, raw, shift);
  const flat = pts.flat().concat(hover);
  previewLine = new Konva.Line({ points: flat, stroke: color, strokeWidth: 2, dash: [6, 4], listening: false });
  uiLayer.add(previewLine);
  // vertex dots
  pts.forEach((p) => uiLayer.add(new Konva.Circle({ x: p[0], y: p[1], radius: 4 / stage.scaleX(), fill: color, listening: false })));
  if (last) {
    const ang = ((Math.atan2(hover[1] - last[1], hover[0] - last[0]) * 180) / Math.PI + 360) % 360;
    const len = Math.hypot(hover[0] - last[0], hover[1] - last[1]);
    setStatus(`∠ ${ang.toFixed(0)}°  ·  урт ${fmtM(len)} м`);
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

// ── drag rectangle (fixtures) ───────────────────────────────────────────────
function startRect(raw) {
  cancelDraft();
  rectDraft = { type: tool, start: raw };
  setStatus("Тэгш өнцөгт — чирээд тавь");
}
function previewRect(raw) {
  if (previewLine) previewLine.destroy();
  const [x0, y0] = rectDraft.start;
  const color = (FIX[rectDraft.type] || {}).color || "#999";
  previewLine = new Konva.Line({
    points: [x0, y0, raw[0], y0, raw[0], raw[1], x0, raw[1]],
    stroke: color, strokeWidth: 2, dash: [6, 4], closed: true, fill: color + "22", listening: false,
  });
  uiLayer.add(previewLine);
  uiLayer.batchDraw();
  setStatus(`▭ ${fmtM(Math.abs(raw[0] - x0))} × ${fmtM(Math.abs(raw[1] - y0))} м`);
}
function finishRect() {
  const raw = pointerPlan();
  const [x0, y0] = rectDraft.start;
  const t = rectDraft.type;
  cancelRect();
  const w = Math.abs(raw[0] - x0), h = Math.abs(raw[1] - y0);
  if (w < 0.2 || h < 0.2) { setStatus("Хэт жижиг — болилоо"); return; } // < 20 cm
  const x1 = round2(Math.min(x0, raw[0])), y1 = round2(Math.min(y0, raw[1]));
  const x2 = round2(Math.max(x0, raw[0])), y2 = round2(Math.max(y0, raw[1]));
  PLAN.fixtures.push({ type: t, points: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]] });
  pushUndo();
  render();
  setStatus(`▦ ${fmtM(w)} × ${fmtM(h)} м нэмэгдлээ`);
}
function cancelRect() {
  rectDraft = null;
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
  if (existing) existing.pos = raw;
  else PLAN.cameras.push({ camera_id: cid, name: name, pos: raw, dir_deg: 0, homography: null });
  pushUndo();
  render();
}

// ── undo / redo ───────────────────────────────────────────────────────────
function snapshot() { return JSON.stringify(PLAN); }
function pushUndo() { undoStack.push(snapshot()); if (undoStack.length > 100) undoStack.shift(); redoStack.length = 0; }
function undo() {
  if (undoStack.length < 2) return;
  redoStack.push(undoStack.pop());
  PLAN = JSON.parse(undoStack[undoStack.length - 1]);
  deselect(); render();
}
function redo() {
  if (!redoStack.length) return;
  const s = redoStack.pop(); undoStack.push(s); PLAN = JSON.parse(s); deselect(); render();
}

// ── element list (DOM) ─────────────────────────────────────────────────────
function renderElements() {
  const el = document.getElementById("elements");
  el.innerHTML = "";
  const rows = [];
  PLAN.cameras.forEach((c, i) => rows.push(["cameras", i, CAM_COLOR, "📷 " + (c.name || c.camera_id)]));
  PLAN.fixtures.forEach((f, i) => rows.push(["fixtures", i, (FIX[f.type] || {}).color || "#999", "▦ " + ((FIX[f.type] || {}).label || f.type)]));
  PLAN.walls.forEach((_w, i) => rows.push(["walls", i, WALL_COLOR, "▭ Хана " + (i + 1)]));
  if (!rows.length) { el.innerHTML = '<p class="hint">Хоосон</p>'; return; }
  rows.forEach(([kind, idx, color, text]) => {
    const row = document.createElement("div");
    row.className = "elem";
    // Camera rows get a «calibrate» action (Phase B) before the delete button.
    const calib = kind === "cameras"
      ? `<button class="calib" title="Калибровк хийх">📐</button>` : "";
    row.innerHTML = `<span class="dot" style="background:${color}"></span><span class="name">${text}</span>${calib}<button class="del">✕</button>`;
    row.querySelector(".del").onclick = () => { PLAN[kind].splice(idx, 1); deselect(); pushUndo(); render(); };
    if (kind === "cameras") row.querySelector(".calib").onclick = () => startCalibration(idx);
    el.appendChild(row);
  });
}

// ── new / clear ────────────────────────────────────────────────────────────
function clearPlan() {
  if (!window.confirm("Бүх зураг (хана, бүс, камер) устгаж шинээр эхлэх үү?")) return;
  PLAN.walls = [];
  PLAN.fixtures = [];
  PLAN.cameras = [];
  deselect();
  cancelDraft();
  pushUndo();
  render();
  fit();
  setStatus("Шинэ хоосон зураг — зурж эхлээрэй");
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
  const calibTxt = calibrated
    ? `✅ Хийсэн${cam.reproj_err != null ? ` (алдаа ${(cam.reproj_err * 100).toFixed(1)}%)` : ""}`
    : "❌ Хийгээгүй";
  el.innerHTML = `
    <h2>📷 ${cam.name || cam.camera_id}</h2>
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
  const onlineEl = document.getElementById("cs-online");
  if (onlineEl) onlineEl.textContent = "⏳ Шалгаж байна…";
  let r = null;
  try { r = await window.pywebview.api.camera_status(cam.camera_id); } catch (e) { r = null; }
  camStatus[cam.camera_id] = { online: r && r.ok ? !!r.online : undefined };
  // Refresh just this camera's badge in place (keeps the selection/transformer).
  const camG = camLayer.getChildren()[idx];
  if (camG) {
    const old = camG.findOne(".badge");
    if (old) old.destroy();
    const b = makeBadge(cam);
    if (b) camG.add(b);
    camLayer.draw();
  }
  if (selectedNode && selectedNode.kind === "cameras" && selectedNode.idx === idx) showCameraSettings(idx);
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
    imgLayer.add(new Konva.Image({ image: imageObj, x: ox, y: oy, width: dw, height: dh }));
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
  PLAN.walls.forEach((w) => planLayer.add(new Konva.Line({ points: w.points.flat(), stroke: WALL_COLOR, strokeWidth: 2 / pz })));
  PLAN.fixtures.forEach((f) => {
    const c = (FIX[f.type] || {}).color || "#999";
    planLayer.add(new Konva.Line({ points: f.points.flat(), stroke: c, strokeWidth: 2 / pz, closed: true, fill: c + "22" }));
  });
  calib.planMarks = new Konva.Group();
  planLayer.add(calib.planMarks);
  planLayer.draw();
  calib.plan.on("mousedown", () => {
    if (!calib.pendingImg) { setCalibStatus("Эхлээд камерын зураг дээр цэг дар ←"); return; }
    const p = calib.plan.getRelativePointerPosition();
    calib.pairs.push({ image: calib.pendingImg, plan: [+p.x.toFixed(1), +p.y.toFixed(1)] });
    calib.pendingImg = null;
    redrawCalibMarks();
    setCalibStatus(`${calib.pairs.length} цэг хослол${calib.pairs.length < 4 ? " (≥4 хэрэгтэй)" : " — Хадгалахад бэлэн"}`);
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
  setCalibStatus(`${calib.pairs.length} цэг хослол`);
}

function closeCalibration() {
  document.getElementById("calib").classList.add("calib-hidden");
  if (calib.img) { calib.img.destroy(); calib.img = null; }
  if (calib.plan) { calib.plan.destroy(); calib.plan = null; }
  calib.pairs = []; calib.pendingImg = null;
}

async function saveCalibration() {
  if (calib.pairs.length < 4) { setCalibStatus("Дор хаяж 4 цэг хослол хэрэгтэй"); return; }
  setCalibStatus("Хадгалж байна…");
  try {
    const r = await window.pywebview.api.save_calibration(calib.cam.camera_id, calib.pairs, PLAN);
    const errPct = (r.reproj_err * 100).toFixed(1);
    setCalibStatus(`✅ Хадгалагдлаа — ${r.zone_count} зон, алдаа ${errPct}%`);
    setStatus(`Калибровк хадгалагдлаа: ${r.zone_count} зон`);
    // Mark calibrated so the camera badge + settings panel update.
    calib.cam._calibrated = true;
    calib.cam.reproj_err = r.reproj_err;
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
  const K = 0.05; // template is authored in 1000×800 units → a realistic 50×40 m store
  const sc = (pts) => pts.map(([x, y]) => [round2(x * K), round2(y * K)]);
  PLAN.size = [50, 40];
  PLAN.walls = TEMPLATE.walls.map((w) => ({ points: sc(w.points) }));
  PLAN.fixtures = TEMPLATE.fixtures.map((f) => ({ type: f.type, points: sc(f.points) }));
  // keep PLAN.cameras — the user's placed cameras are theirs
  deselect();
  pushUndo();
  render();
  fit();
  setStatus("Жишээ загвар (50 × 40 м) ачаалагдлаа — өөрийн дэлгүүрт тааруулж засаарай");
}

// ── fit ─────────────────────────────────────────────────────────────────────
function fit() {
  const [pw, ph] = PLAN.size;
  const z = Math.min(stage.width() / pw, stage.height() / ph) * 0.9;
  stage.scale({ x: z, y: z });
  stage.position({ x: (stage.width() - pw * z) / 2, y: (stage.height() - ph * z) / 2 });
  drawGrid();
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
const TOOL_KEYS = ["select", "wall", "shelf", "exit", "checkout", "camera"];
window.addEventListener("keydown", (e) => {
  // Typing in an input (e.g. length) must not trigger tool shortcuts.
  if (e.target && e.target.tagName === "INPUT") return;
  if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (e.key >= "1" && e.key <= "6") setTool(TOOL_KEYS[+e.key - 1]);
  else if (e.key === "Enter") finishDraft();
  else if (e.key === "Escape") { cancelDraft(); cancelRect(); cancelMarquee(); deselect(); }
  else if (e.key === "Backspace" && draft) { draft.pts.pop(); if (!draft.pts.length) cancelDraft(); else drawPreview(draft.pts[draft.pts.length - 1], false); }
  else if (e.key === "Delete") deleteSelection();
  else if (e.key.toLowerCase() === "g") document.getElementById("btn-snap").click();
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
  drawGrid();
});

// ── load / save via pywebview bridge ───────────────────────────────────────
async function save() {
  if (draft && draft.pts.length >= (draft.type === "wall" ? 2 : 3)) finishDraft();
  setStatus("Хадгалж байна…");
  try {
    await window.pywebview.api.save_plan(PLAN);
    setStatus("✅ Хадгалагдлаа");
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
  return {
    version: p.version || 1,
    size: p.size && p.size.length === 2 ? p.size : DEFAULT_SIZE_M.slice(),
    walls: (p.walls || []).map((w) => ({ points: w.points || [] })),
    fixtures: (p.fixtures || []).map((f) => ({ id: f.id, type: f.type, points: f.points || [] })),
    cameras: (p.cameras || []).map((c) => ({
      camera_id: c.camera_id, name: c.name, pos: c.pos || [0, 0],
      dir_deg: c.dir_deg || 0, homography: c.homography || null,
      reproj_err: c.reproj_err, calib_points: c.calib_points,
    })),
  };
}

// pywebview injects the api asynchronously; wait for it.
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
