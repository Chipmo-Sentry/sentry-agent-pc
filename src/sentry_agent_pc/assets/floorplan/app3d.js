// 3D preview of the drawn plan (owner request 07-21) — mirrors the web
// dashboard's PlanViewport3D so the operator sees the same extruded store
// while drawing/calibrating. Classic script (file:// — no ES modules):
// three.js + OrbitControls come pre-bundled in vendor/three-bundle.min.js
// as the ThreeBundle global. Reads the live PLAN + FIX globals from app.js.
//
// Calibration tie-in: a calibrated camera whose solvePnP pose produced a
// mount height (cam_h_m, persisted since v0.7.102) hangs at that REAL height
// with its view cone — the operator instantly sees an implausible
// calibration (camera on the floor / in the ceiling) and re-clicks points.
/* global ThreeBundle, PLAN, FIX */

(function () {
  "use strict";

  const WALL_DEFAULT_H = 2.8;
  const WALL_THICKNESS = 0.12;
  // Door-like fixtures cut an OPENING through the wall they sit on (нэвт
  // харагдана) — doorway open to DOOR_OPENING_H, lintel above.
  const DOOR_TYPES = { door: 1, exterior_door: 1, exit: 1, entrance: 1 };
  const DOOR_OPENING_H = 2.05;
  const WINDOW_SILL_H = 0.9; // window: sill below, glass pane in the opening

  function segSpansInPoly(x1, y1, x2, y2, poly) {
    function inside(px, py) {
      let odd = false;
      for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const xi = poly[i][0], yi = poly[i][1];
        const xj = poly[j][0], yj = poly[j][1];
        if (yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) odd = !odd;
      }
      return odd;
    }
    const ts = [];
    const dx = x2 - x1, dy = y2 - y1;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const ax = poly[j][0], ay = poly[j][1];
      const rx = poly[i][0] - ax, ry = poly[i][1] - ay;
      const den = dx * ry - dy * rx;
      if (Math.abs(den) < 1e-12) continue;
      const t = ((ax - x1) * ry - (ay - y1) * rx) / den;
      const u = ((ax - x1) * dy - (ay - y1) * dx) / den;
      if (t > 0 && t < 1 && u >= 0 && u <= 1) ts.push(t);
    }
    ts.sort((a, b) => a - b);
    const bounds = [0].concat(ts, [1]);
    const spans = [];
    for (let i = 0; i < bounds.length - 1; i++) {
      const a = bounds[i], b = bounds[i + 1];
      if (b - a < 1e-6) continue;
      const mid = (a + b) / 2;
      if (inside(x1 + dx * mid, y1 + dy * mid)) spans.push([a, b]);
    }
    return spans;
  }

  function mergeSpans(spans) {
    if (!spans.length) return spans;
    spans.sort((a, b) => a[0] - b[0]);
    const out = [spans[0]];
    for (let i = 1; i < spans.length; i++) {
      const last = out[out.length - 1];
      if (spans[i][0] <= last[1] + 1e-6) last[1] = Math.max(last[1], spans[i][1]);
      else out.push(spans[i]);
    }
    return out;
  }

  // ── footprint wall occlusion (хана нэвтэлдэггүй) ─────────────────────────
  function raySegT(o, d, a, b, eps) {
    const rx = b[0] - a[0], ry = b[1] - a[1];
    const den = d[0] * ry - d[1] * rx;
    if (Math.abs(den) < 1e-12) return null;
    const qx = a[0] - o[0], qy = a[1] - o[1];
    const t = (qx * ry - qy * rx) / den;
    // /den, NOT /-den — the flipped sign made half the walls pass-through.
    const u = (qx * d[1] - qy * d[0]) / den;
    return t >= eps && u >= 0 && u <= 1 ? t : null;
  }
  function nearestWallT(o, d, tmax, walls, eps) {
    let best = tmax;
    for (const w of walls) {
      const pts = w.points;
      for (let i = 0; i < pts.length - 1; i++) {
        const t = raySegT(o, d, pts[i], pts[i + 1], eps);
        if (t !== null && t < best) best = t;
      }
    }
    return best;
  }
  function rayPolyInterval(o, d, poly) {
    const ts = [];
    for (let i = 0; i < poly.length; i++) {
      const t = raySegT(o, d, poly[i], poly[(i + 1) % poly.length], -1e-9);
      if (t !== null) ts.push(t);
    }
    if (!ts.length) return null;
    return [Math.max(0, Math.min.apply(null, ts)), Math.max.apply(null, ts)];
  }
  function occludeFootprint(pos, pts, walls, eps) {
    if (!walls.length) return pts;
    const o = pos;
    const angles = [];
    for (const p of pts) {
      const dx = p[0] - o[0], dy = p[1] - o[1];
      if (Math.hypot(dx, dy) > 1e-9) angles.push(Math.atan2(dy, dx));
    }
    if (!angles.length) return pts;
    const ref = angles[0];
    const rel = (a) => {
      let r = a - ref;
      while (r <= -Math.PI) r += 2 * Math.PI;
      while (r > Math.PI) r -= 2 * Math.PI;
      return r;
    };
    const offs = angles.map(rel);
    const amin = Math.min.apply(null, offs);
    const amax = Math.max.apply(null, offs);
    // Interior origin (a wide lens sees all around its base): the single
    // boundary crossing is the EXIT — near starts at the camera itself, and
    // the sweep covers the full circle. Treating the exit as the entry made
    // every ray degenerate → empty clip → raw wall-leaking patch fallback.
    let insideOrigin = false;
    {
      let odd = false;
      for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
        const xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
        if (yi > o[1] !== yj > o[1] && o[0] < ((xj - xi) * (o[1] - yi)) / (yj - yi) + xi) odd = !odd;
      }
      insideOrigin = odd;
    }
    const near = [], far = [];
    const N = 90;
    const sweepMin = insideOrigin ? -Math.PI : amin;
    const sweepMax = insideOrigin ? Math.PI : amax;
    for (let k = 0; k <= N; k++) {
      const a = ref + sweepMin + ((sweepMax - sweepMin) * k) / N;
      const d = [Math.cos(a), Math.sin(a)];
      const iv = rayPolyInterval(o, d, pts);
      if (!iv || iv[1] <= 1e-6) continue;
      const nearT = insideOrigin ? 0 : iv[0];
      const tw = nearestWallT(o, d, iv[1], walls, eps);
      if (tw <= nearT + 1e-6) continue;
      near.push([o[0] + d[0] * nearT, o[1] + d[1] * nearT]);
      far.push([o[0] + d[0] * tw, o[1] + d[1] * tw]);
    }
    if (far.length < 2) return [];
    return insideOrigin ? far : near.concat(far.reverse());
  }

  function fpArea(pts) {
    let s = 0;
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i], b = pts[(i + 1) % pts.length];
      s += a[0] * b[1] - b[0] * a[1];
    }
    return Math.abs(s) / 2;
  }

  // Adaptive: strict 5 cm first (mounting wall CANNOT leak the patch outside);
  // lenient 0.5 m only when strict swallowed nearly everything (camera drawn
  // slightly on the wrong side of its wall).
  function clipFootprint(pos, pts, walls) {
    const strict = occludeFootprint(pos, pts, walls, 0.05);
    if (fpArea(strict) >= fpArea(pts) * 0.2 && strict.length >= 3) return strict;
    const lenient = occludeFootprint(pos, pts, walls, 0.5);
    return fpArea(lenient) > fpArea(strict) ? lenient : strict;
  }

  let overlay = null;
  let cleanup = null;

  function planExtent() {
    const pts = [];
    for (const w of PLAN.walls) pts.push(...w.points);
    for (const f of PLAN.fixtures) pts.push(...f.points);
    for (const c of PLAN.cameras) pts.push(c.pos);
    if (pts.length === 0) return { x: 0, y: 0, w: PLAN.size[0], h: PLAN.size[1] };
    let x1 = 1e9, y1 = 1e9, x2 = -1e9, y2 = -1e9;
    for (const [x, y] of pts) {
      x1 = Math.min(x1, x); y1 = Math.min(y1, y);
      x2 = Math.max(x2, x); y2 = Math.max(y2, y);
    }
    const pad = Math.max(1, (x2 - x1) * 0.06);
    return { x: x1 - pad, y: y1 - pad, w: x2 - x1 + 2 * pad, h: y2 - y1 + 2 * pad };
  }

  function build(host) {
    const { THREE, OrbitControls } = ThreeBundle;
    const ext = planExtent();
    const cx = ext.x + ext.w / 2;
    const cz = ext.y + ext.h / 2;
    const span = Math.max(ext.w, ext.h);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0a0a0a);

    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, span * 10);
    camera.position.set(cx + span * 0.55, span * 0.75, cz + span * 0.85);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(cx, 0, cz);
    controls.maxPolarAngle = Math.PI / 2 - 0.02;
    controls.enableDamping = true;
    // Shift + left-drag pans (owner request 07-23) — release Shift to orbit.
    const onKey = (e) => {
      controls.mouseButtons.LEFT = e.shiftKey ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE;
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKey);

    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const sun = new THREE.DirectionalLight(0xffffff, 1.1);
    sun.position.set(cx - span * 0.4, span * 1.2, cz - span * 0.3);
    scene.add(sun);

    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(ext.w, ext.h),
      new THREE.MeshStandardMaterial({ color: 0x171717, roughness: 0.95 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(cx, 0, cz);
    scene.add(floor);
    const grid = new THREE.GridHelper(span, Math.round(span), 0x2a2a2a, 0x1f1f1f);
    grid.position.set(cx, 0.01, cz);
    scene.add(grid);

    // Translucent walls — the interior stays visible from any angle.
    const wallMat = new THREE.MeshStandardMaterial({
      color: 0xd4d4d4, roughness: 0.85, transparent: true, opacity: 0.45, depthWrite: false,
    });
    const glassMat = new THREE.MeshStandardMaterial({
      color: 0x93c5fd, transparent: true, opacity: 0.25, roughness: 0.1,
    });
    const doorPolys = PLAN.fixtures
      .filter((f) => DOOR_TYPES[f.type] && f.points && f.points.length >= 3)
      .map((f) => f.points);
    const windowPolys = PLAN.fixtures
      .filter((f) => f.type === "window" && f.points && f.points.length >= 3)
      .map((f) => f.points);
    function addWallPiece(x1, y1, x2, y2, a, b, h, yBase, mat) {
      const len = Math.hypot(x2 - x1, y2 - y1) * (b - a);
      if (len < 0.01 || h <= 0.01) return;
      const mx = x1 + (x2 - x1) * ((a + b) / 2);
      const my = y1 + (y2 - y1) * ((a + b) / 2);
      const mesh = new THREE.Mesh(
        new THREE.BoxGeometry(len, h, WALL_THICKNESS), mat || wallMat,
      );
      mesh.position.set(mx, yBase + h / 2, my);
      mesh.rotation.y = -Math.atan2(y2 - y1, x2 - x1);
      scene.add(mesh);
    }
    for (const wall of PLAN.walls) {
      const h = wall.height_m != null ? Number(wall.height_m) : WALL_DEFAULT_H;
      if (!(h > 0)) continue;
      const pts = wall.points;
      for (let i = 0; i < pts.length - 1; i++) {
        const [x1, y1] = pts[i];
        const [x2, y2] = pts[i + 1];
        if (Math.hypot(x2 - x1, y2 - y1) < 1e-6) continue;
        let ds = [];
        for (const p of doorPolys) ds = ds.concat(segSpansInPoly(x1, y1, x2, y2, p));
        const doorSpans = mergeSpans(ds);
        let wsRaw = [];
        for (const p of windowPolys) wsRaw = wsRaw.concat(segSpansInPoly(x1, y1, x2, y2, p));
        // Doors win where a window overlaps one.
        const winSpans = mergeSpans(wsRaw).filter(([a, b]) => {
          const mid = (a + b) / 2;
          return !doorSpans.some(([da, db]) => mid >= da && mid <= db);
        });
        const openings = doorSpans
          .map(([a, b]) => [a, b, "door"])
          .concat(winSpans.map(([a, b]) => [a, b, "window"]))
          .sort((p, q) => p[0] - q[0]);
        let cursor = 0;
        for (const [a, b, kind] of openings) {
          addWallPiece(x1, y1, x2, y2, cursor, a, h, 0);
          if (kind === "door") {
            if (h > DOOR_OPENING_H) addWallPiece(x1, y1, x2, y2, a, b, h - DOOR_OPENING_H, DOOR_OPENING_H);
          } else {
            addWallPiece(x1, y1, x2, y2, a, b, Math.min(WINDOW_SILL_H, h), 0);
            const top = Math.min(DOOR_OPENING_H, h);
            if (top > WINDOW_SILL_H) addWallPiece(x1, y1, x2, y2, a, b, top - WINDOW_SILL_H, WINDOW_SILL_H, glassMat);
            if (h > DOOR_OPENING_H) addWallPiece(x1, y1, x2, y2, a, b, h - DOOR_OPENING_H, DOOR_OPENING_H);
          }
          cursor = b;
        }
        addWallPiece(x1, y1, x2, y2, cursor, 1, h, 0);
      }
    }

    for (const f of PLAN.fixtures) {
      if (!f.points || f.points.length < 3) continue;
      const spec = FIX[f.type] || {};
      const h = f.height_m != null ? Number(f.height_m) : (spec.height_m || 0);
      const color = new THREE.Color(spec.color || "#999999");
      const shape = new THREE.Shape(f.points.map(([x, y]) => new THREE.Vector2(x, -y)));
      const mat = new THREE.MeshStandardMaterial({
        color, roughness: 0.7, transparent: true, opacity: h > 0 ? 0.85 : 0.4,
      });
      let mesh;
      if (h > 0) {
        const geo = new THREE.ExtrudeGeometry(shape, { depth: h, bevelEnabled: false });
        mesh = new THREE.Mesh(geo, mat);
        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geo),
          new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9 }),
        );
        edges.rotation.x = -Math.PI / 2;
        scene.add(edges);
      } else {
        mesh = new THREE.Mesh(new THREE.ShapeGeometry(shape), mat);
        mesh.position.y = 0.02;
      }
      mesh.rotation.x = -Math.PI / 2;
      scene.add(mesh);
    }

    const camMat = new THREE.MeshStandardMaterial({ color: 0x3b82f6 });
    const camMatCal = new THREE.MeshStandardMaterial({ color: 0x22c55e });
    const coneMat = new THREE.MeshBasicMaterial({
      color: 0x2563eb, transparent: true, opacity: 0.14,
      side: THREE.DoubleSide, depthWrite: false,
    });
    for (const cam of PLAN.cameras) {
      const [px, py] = cam.pos;
      // Calibrated cameras (solvePnP) hang at their MEASURED height, in green —
      // the whole point of "3D калибровк": байрлал нь үнэн эсэхийг нүдээр шалгана.
      const calibrated = cam.cam_h_m != null && Number(cam.cam_h_m) > 0;
      const mountH = calibrated ? Number(cam.cam_h_m) : WALL_DEFAULT_H - 0.2;
      const body = new THREE.Mesh(
        new THREE.BoxGeometry(0.35, 0.22, 0.22),
        calibrated ? camMatCal : camMat,
      );
      body.position.set(px, mountH, py);
      body.rotation.y = -((cam.dir_deg || 0) * Math.PI) / 180;
      body.userData.noPick = true; // calibration clicks fall through to surfaces
      scene.add(body);
      // Calibrated camera → its REAL ground footprint (H⁻¹ + k1, same math as
      // the editor's 2D coverage overlay) painted onto the floor, with faint
      // sight lines from the lens to the far corners. Uncalibrated → the
      // cosmetic view cone as before.
      let fp = null;
      try {
        if (cam.homography && typeof cameraFootprint === "function") fp = cameraFootprint(cam);
      } catch (e) { fp = null; }
      if (fp && fp.exact) {
        const MAXR = 25; // horizon-adjacent corners project absurdly far
        let pts = fp.pts.map(([fx, fy]) => {
          const dx = fx - px, dy = fy - py;
          const d = Math.hypot(dx, dy);
          return d > MAXR ? [px + (dx / d) * MAXR, py + (dy / d) * MAXR] : [fx, fy];
        });
        // Walls cut the patch (хана нэвтэлдэггүй) — sight stops at the first wall.
        const clipped = clipFootprint([px, py], pts, PLAN.walls);
        if (clipped.length >= 3) pts = clipped;
        const shape = new THREE.Shape(pts.map(([fx, fy]) => new THREE.Vector2(fx, -fy)));
        const patch = new THREE.Mesh(
          new THREE.ShapeGeometry(shape),
          new THREE.MeshBasicMaterial({
            color: 0x2563eb, transparent: true, opacity: 0.16, depthWrite: false,
          }),
        );
        patch.rotation.x = -Math.PI / 2;
        patch.position.y = 0.03;
        patch.userData.noPick = true;
        scene.add(patch);
        const loop = new THREE.LineLoop(
          new THREE.BufferGeometry().setFromPoints(
            pts.map(([fx, fy]) => new THREE.Vector3(fx, 0.04, fy)),
          ),
          new THREE.LineBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.6 }),
        );
        scene.add(loop);
        // A handful of sight lines — the clipped outline can be ~180 points.
        const step = Math.max(1, Math.floor(pts.length / 6));
        for (let si = 0; si < pts.length; si += step) {
          const [fx, fy] = pts[si];
          scene.add(new THREE.Line(
            new THREE.BufferGeometry().setFromPoints([
              new THREE.Vector3(px, mountH, py),
              new THREE.Vector3(fx, 0.04, fy),
            ]),
            new THREE.LineBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.22 }),
          ));
        }
      } else {
        const reach = Math.min(6, span * 0.3);
        const cone = new THREE.Mesh(
          new THREE.ConeGeometry(reach * 0.45, reach, 24, 1, true), coneMat,
        );
        const dir = ((cam.dir_deg || 0) * Math.PI) / 180;
        const tilt = (55 * Math.PI) / 180;
        const axis = new THREE.Vector3(
          Math.cos(dir) * Math.cos(tilt), -Math.sin(tilt), Math.sin(dir) * Math.cos(tilt),
        ).normalize();
        cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, -1, 0), axis);
        cone.position.set(
          px + axis.x * (reach / 2), mountH + axis.y * (reach / 2), py + axis.z * (reach / 2),
        );
        cone.userData.noPick = true;
        scene.add(cone);
      }

      // Height label sprite («3.1 м») over a calibrated camera.
      if (calibrated) {
        const spr = makeTextSprite(Number(cam.cam_h_m).toFixed(1) + " м", "#22c55e");
        spr.position.set(px, mountH + 0.5, py);
        scene.add(spr);
      }
    }

    let raf = 0;
    const render = () => {
      controls.update();
      renderer.render(scene, camera);
      raf = requestAnimationFrame(render);
    };
    const resize = () => {
      const w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    raf = requestAnimationFrame(render);

    return {
      scene, camera, renderer, controls,
      dispose() {
        cancelAnimationFrame(raf);
        ro.disconnect();
        window.removeEventListener("keydown", onKey);
        window.removeEventListener("keyup", onKey);
        controls.dispose();
        renderer.dispose();
        if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement);
      },
    };
  }

  // Text sprite («3.1 м», pair numbers) — canvas-textured, always camera-facing.
  function makeTextSprite(text, color, scale) {
    const { THREE } = ThreeBundle;
    const cv = document.createElement("canvas");
    cv.width = 128; cv.height = 48;
    const g = cv.getContext("2d");
    g.font = "bold 28px sans-serif";
    g.fillStyle = color;
    g.textAlign = "center";
    g.fillText(text, 64, 34);
    const spr = new THREE.Sprite(
      new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(cv), transparent: true }),
    );
    spr.scale.set(1.6 * (scale || 1), 0.6 * (scale || 1), 1);
    return spr;
  }

  window.toggle3D = function toggle3D() {
    if (overlay) {
      if (cleanup) cleanup();
      overlay.remove();
      overlay = null;
      cleanup = null;
      return;
    }
    overlay = document.createElement("div");
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:900;background:#0a0a0a;display:flex;flex-direction:column;";
    const bar = document.createElement("div");
    bar.style.cssText =
      "display:flex;align-items:center;gap:12px;padding:8px 12px;color:#d4d4d4;font:13px sans-serif;";
    bar.innerHTML =
      "<b>3D урьдчилан харах</b>" +
      "<span style='color:#737373'>Чирэх — эргүүлэх · Shift+чирэх — зөөх · Гүйлгэх — томруулах · " +
      "Ногоон камер = калибровкоор хэмжигдсэн өндөртөө</span>";
    const close = document.createElement("button");
    close.textContent = "✕ Хаах (Esc)";
    close.style.cssText =
      "margin-left:auto;background:#262626;color:#fafafa;border:1px solid #404040;" +
      "border-radius:6px;padding:6px 12px;cursor:pointer;";
    close.onclick = window.toggle3D;
    bar.appendChild(close);
    const host = document.createElement("div");
    host.style.cssText = "flex:1;min-height:0;";
    overlay.appendChild(bar);
    overlay.appendChild(host);
    document.body.appendChild(overlay);
    const handle = build(host);
    cleanup = () => handle.dispose();
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay) window.toggle3D();
  });

  // ── Calibrate IN 3D (owner request 07-22) ─────────────────────────────────
  // The calibration overlay's plan pane can switch to this interactive 3D
  // scene: rotate the store to roughly the camera's own viewpoint, then CLICK
  // the matching spot — the ray is dropped onto the floor plane and the (x, y)
  // becomes the plan half of the pair. Pair marks render as numbered spheres;
  // once the dry-run pose solves, a green ghost camera hangs at the MEASURED
  // height so the operator validates the calibration in space as they go.
  window.Calib3D = {
    _h: null,
    _marks: [],
    _cand: null,
    isOpen() { return !!this._h; },
    open(host, onPick) {
      this.close();
      const { THREE } = ThreeBundle;
      const h = (this._h = build(host));
      // Click = pick (with a drag tolerance so orbiting never drops points).
      let down = null;
      const el = h.renderer.domElement;
      el.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
      el.addEventListener("pointerup", (e) => {
        if (!down) return;
        const moved = Math.hypot(e.clientX - down[0], e.clientY - down[1]);
        down = null;
        if (moved > 5) return; // that was an orbit/pan drag
        const r = el.getBoundingClientRect();
        const ndc = new THREE.Vector2(
          ((e.clientX - r.left) / r.width) * 2 - 1,
          -(((e.clientY - r.top) / r.height) * 2 - 1),
        );
        const ray = new THREE.Raycaster();
        ray.setFromCamera(ndc, h.camera);
        // Pick REAL surfaces first (walls, shelf tops — elevated calibration
        // points, owner request 07-23); pair marks/ghost/sprites are noPick.
        const hits = ray
          .intersectObjects(h.scene.children, true)
          .filter((it) => it.object.isMesh && !it.object.userData.noPick);
        if (hits.length) {
          const p = hits[0].point;
          onPick(p.x, p.z, Math.max(0, p.y));
          return;
        }
        const hit = new THREE.Vector3();
        if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), hit)) {
          onPick(hit.x, hit.z, 0);
        }
      });
      return h;
    },
    // Mirror the 2D pair marks: numbered spheres at their picked HEIGHT (an
    // elevated wall/shelf-top point floats where it was clicked).
    setMarks(pairs, colors) {
      if (!this._h) return;
      const { THREE } = ThreeBundle;
      for (const m of this._marks) this._h.scene.remove(m);
      this._marks = [];
      pairs.forEach((pr, i) => {
        const color = colors[i % colors.length];
        const hM = Number(pr.h) || 0;
        const s = new THREE.Mesh(
          new THREE.SphereGeometry(0.14, 16, 12),
          new THREE.MeshBasicMaterial({ color }),
        );
        s.position.set(pr.plan[0], hM + 0.14, pr.plan[1]);
        s.userData.noPick = true;
        this._h.scene.add(s);
        this._marks.push(s);
        const label = makeTextSprite(String(i + 1), color, 0.8);
        label.position.set(pr.plan[0], hM + 0.75, pr.plan[1]);
        label.userData.noPick = true;
        this._h.scene.add(label);
        this._marks.push(label);
      });
    },
    // Green ghost camera at the dry-run solved height — «3D-ээр calibrate».
    setCandidate(camId, hM) {
      if (!this._h) return;
      const { THREE } = ThreeBundle;
      if (this._cand) {
        for (const o of this._cand) this._h.scene.remove(o);
        this._cand = null;
      }
      if (hM == null || !(hM > 0)) return;
      const cam = PLAN.cameras.find((c) => c.camera_id === camId);
      if (!cam) return;
      const body = new THREE.Mesh(
        new THREE.BoxGeometry(0.4, 0.26, 0.26),
        new THREE.MeshBasicMaterial({ color: 0x22c55e, transparent: true, opacity: 0.9 }),
      );
      body.position.set(cam.pos[0], hM, cam.pos[1]);
      const pole = new THREE.Mesh(
        new THREE.CylinderGeometry(0.02, 0.02, hM, 8),
        new THREE.MeshBasicMaterial({ color: 0x22c55e, transparent: true, opacity: 0.35 }),
      );
      pole.position.set(cam.pos[0], hM / 2, cam.pos[1]);
      const label = makeTextSprite(hM.toFixed(1) + " м", "#22c55e");
      label.position.set(cam.pos[0], hM + 0.5, cam.pos[1]);
      body.userData.noPick = true;
      pole.userData.noPick = true;
      label.userData.noPick = true;
      this._h.scene.add(body);
      this._h.scene.add(pole);
      this._h.scene.add(label);
      this._cand = [body, pole, label];
    },
    close() {
      if (this._h) this._h.dispose();
      this._h = null;
      this._marks = [];
      this._cand = null;
    },
  };
})();
