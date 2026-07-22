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

    const wallMat = new THREE.MeshStandardMaterial({ color: 0xd4d4d4, roughness: 0.85 });
    for (const wall of PLAN.walls) {
      const h = wall.height_m != null ? Number(wall.height_m) : WALL_DEFAULT_H;
      if (!(h > 0)) continue;
      const pts = wall.points;
      for (let i = 0; i < pts.length - 1; i++) {
        const [x1, y1] = pts[i];
        const [x2, y2] = pts[i + 1];
        const len = Math.hypot(x2 - x1, y2 - y1);
        if (len < 1e-6) continue;
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(len, h, WALL_THICKNESS), wallMat);
        mesh.position.set((x1 + x2) / 2, h / 2, (y1 + y2) / 2);
        mesh.rotation.y = -Math.atan2(y2 - y1, x2 - x1);
        scene.add(mesh);
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
      scene.add(body);
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
      scene.add(cone);

      // Height label sprite («3.1 м») over a calibrated camera.
      if (calibrated) {
        const cv = document.createElement("canvas");
        cv.width = 128; cv.height = 48;
        const g = cv.getContext("2d");
        g.font = "bold 26px sans-serif";
        g.fillStyle = "#22c55e";
        g.textAlign = "center";
        g.fillText(Number(cam.cam_h_m).toFixed(1) + " м", 64, 32);
        const tex = new THREE.CanvasTexture(cv);
        const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
        spr.scale.set(1.6, 0.6, 1);
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

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      host.removeChild(renderer.domElement);
    };
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
      "<span style='color:#737373'>Чирэх — эргүүлэх · Гүйлгэх — томруулах · Баруун чирэх — зөөх · " +
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
    cleanup = build(host);
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay) window.toggle3D();
  });
})();
