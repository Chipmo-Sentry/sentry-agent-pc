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

let PLAN = { version: 1, size: [1000, 800], walls: [], fixtures: [], cameras: [] };
let cameras = []; // registered cameras [{camera_id, name}]
let tool = "select";
let snapOn = true;
let draft = null; // { type, pts: [[x,y],...] } while drawing
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
  document.querySelectorAll(".tool").forEach((b) => b.classList.toggle("active", b.dataset.tool === t));
  // Pan by dragging empty space only in select mode; shapes carry their own drag.
  stage.draggable(t === "select");
  if (t !== "select") deselect();
  stage.container().style.cursor = t === "select" ? "default" : "crosshair";
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
      x: pts[0][0] + 4, y: pts[0][1] - 16, text: label, fontSize: 13,
      fontStyle: "bold", fill: color, listening: false,
    });
    line._label = t;
    shapeLayer.add(t);
  }
  return line;
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
  const step = 50;
  for (let x = 0; x <= pw; x += step)
    gridLayer.add(new Konva.Line({ points: [x, 0, x, ph], stroke: "#1c1c20", strokeWidth: 1 / stage.scaleX() }));
  for (let y = 0; y <= ph; y += step)
    gridLayer.add(new Konva.Line({ points: [0, y, pw, y], stroke: "#1c1c20", strokeWidth: 1 / stage.scaleX() }));
  gridLayer.add(new Konva.Rect({ x: 0, y: 0, width: pw, height: ph, stroke: "#33333a", strokeWidth: 1 / stage.scaleX() }));
  gridLayer.draw();
}

// ── selection / transform / vertex editing ──────────────────────────────
function deselect() {
  tr.nodes([]);
  vertexAnchors.forEach((a) => a.destroy());
  vertexAnchors = [];
  selectedNode = null;
  uiLayer.draw();
}

function selectCamera(node, idx) {
  deselect();
  selectedNode = { node, kind: "cameras", idx };
  node.draggable(true);
  tr.nodes([node]);
  uiLayer.draw();
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
      arr[vi] = [a.x(), a.y()];
      line.points(arr.flat());
      if (line._label) line._label.position({ x: arr[0][0] + 4, y: arr[0][1] - 16 });
      shapeLayer.batchDraw();
    });
    a.on("dragend", pushUndo);
    vertexAnchors.push(a);
    uiLayer.add(a);
  });
  // Moving the whole shape bakes the offset back into the points.
  line.on("dragend", () => {
    const ox = line.x(), oy = line.y();
    arr.forEach((p) => { p[0] += ox; p[1] += oy; });
    line.points(arr.flat());
    line.position({ x: 0, y: 0 });
    pushUndo();
    selectShape(line, kind, idx); // refresh anchors at new spot
  });
  uiLayer.draw();
}

// ── drawing ──────────────────────────────────────────────────────────────
let previewLine = null;
function cancelDraft() {
  draft = null;
  if (previewLine) { previewLine.destroy(); previewLine = null; }
  uiLayer.batchDraw();
  setStatus("");
}

stage.on("mousedown", (e) => {
  if (tool === "select") return; // pan / selection handled elsewhere
  const raw = pointerPlan();
  if (tool === "camera") { placeCamera(raw); return; }
  // wall / fixture: add a (snapped) vertex
  const prev = draft && draft.pts.length ? draft.pts[draft.pts.length - 1] : null;
  const p = snapSeg(prev, raw, e.evt.shiftKey);
  if (!draft) draft = { type: tool, pts: [] };
  draft.pts.push(p);
  drawPreview(raw, e.evt.shiftKey);
});

stage.on("mousemove", () => {
  if (!draft) return;
  drawPreview(pointerPlan(), false);
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
    setStatus(`∠ ${ang.toFixed(0)}°  ·  урт ${len.toFixed(0)}`);
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
    row.innerHTML = `<span class="dot" style="background:${color}"></span><span class="name">${text}</span><button class="del">✕</button>`;
    row.querySelector(".del").onclick = () => { PLAN[kind].splice(idx, 1); deselect(); pushUndo(); render(); };
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
  PLAN.size = [1000, 800];
  PLAN.walls = JSON.parse(JSON.stringify(TEMPLATE.walls));
  PLAN.fixtures = JSON.parse(JSON.stringify(TEMPLATE.fixtures));
  // keep PLAN.cameras — the user's placed cameras are theirs
  deselect();
  pushUndo();
  render();
  fit();
  setStatus("Жишээ загвар ачаалагдлаа — өөрийн дэлгүүрт тааруулж засаарай");
}

// ── fit ─────────────────────────────────────────────────────────────────────
function fit() {
  const [pw, ph] = PLAN.size;
  const z = Math.min(stage.width() / pw, stage.height() / ph) * 0.9;
  stage.scale({ x: z, y: z });
  stage.position({ x: (stage.width() - pw * z) / 2, y: (stage.height() - ph * z) / 2 });
  drawGrid();
}

// ── stage click on empty → deselect ───────────────────────────────────────
stage.on("click", (e) => {
  if (tool === "select" && e.target === stage) deselect();
});

// ── toolbar wiring ────────────────────────────────────────────────────────
document.querySelectorAll(".tool").forEach((b) => (b.onclick = () => setTool(b.dataset.tool)));
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

// ── keyboard shortcuts ──────────────────────────────────────────────────────
const TOOL_KEYS = ["select", "wall", "shelf", "exit", "checkout", "camera"];
window.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
  if (e.key >= "1" && e.key <= "6") setTool(TOOL_KEYS[+e.key - 1]);
  else if (e.key === "Enter") finishDraft();
  else if (e.key === "Escape") cancelDraft();
  else if (e.key === "Backspace" && draft) { draft.pts.pop(); if (!draft.pts.length) cancelDraft(); else drawPreview(draft.pts[draft.pts.length - 1], false); }
  else if (e.key === "Delete" && selectedNode) { PLAN[selectedNode.kind].splice(selectedNode.idx, 1); deselect(); pushUndo(); render(); }
  else if (e.key.toLowerCase() === "g") document.getElementById("btn-snap").click();
  else if (e.key.toLowerCase() === "f") fit();
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
  render(); fit();
  setTool("select");
}

function normalize(p) {
  return {
    version: p.version || 1,
    size: p.size && p.size.length === 2 ? p.size : [1000, 800],
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
