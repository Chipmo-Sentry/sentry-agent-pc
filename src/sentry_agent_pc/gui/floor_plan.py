"""Floor-plan editor page (docs/30 Phase A) — draw the store top-down + place
cameras, save to the backend (`Store.floor_plan`).

The canvas works in PLAN-logical coordinates; a ViewTransform (pan/zoom) maps to
screen pixels. Walls are polylines, fixtures (shelf/exit/entrance/checkout) are
polygons, cameras are points with a direction. Calibration (homography) + live
dots land in Phase B / C — here every camera's `homography` stays null.
"""

from __future__ import annotations

import contextlib
import threading
import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from sentry_agent_pc.gui import widgets
from sentry_agent_pc.gui.floor_plan_model import (
    CAMERA_COLOR,
    DEFAULT_PLAN_SIZE,
    WALL_COLOR,
    ViewTransform,
    angle_deg,
    dir_handle,
    fixture_color,
    fixture_label,
)
from sentry_agent_pc.gui.widgets import BRAND_ORANGE, BRAND_ORANGE_HOVER
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.state import CameraRecord

log = get_logger("sentry_agent_pc.gui.floor_plan")

_BG = "#0E0E10"
_GRID = "#1C1C20"
_MIN_FIXTURE_PTS = 3
_MIN_WALL_PTS = 2
_HANDLE_R = 5  # camera body / direction-handle radius (screen px)
_DIR_LEN = 26  # direction handle distance from the camera (screen px)
_HIT_PX = 12  # click tolerance for selecting a camera (screen px)

# Draw modes.
_FIXTURE_MODES = ("shelf", "exit", "entrance", "checkout")


class FloorPlanPage(ctk.CTkFrame):
    """Full-page editor for the store floor plan."""

    def __init__(
        self, master: ctk.CTkBaseClass, get_cameras: Callable[[], list[CameraRecord]]
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._get_cameras = get_cameras

        self._plan: dict[str, Any] = _empty_plan()
        self._view = ViewTransform()
        self._fitted = False
        self._mode = "select"
        self._draft: list[tuple[float, float]] = []  # plan coords of in-progress shape
        self._sel_cam: int | None = None  # selected camera index (for move/rotate)
        self._drag: str | None = None  # "pan" | "cam" | "dir" | None
        self._last_drag: tuple[int, int] | None = None
        self._loaded = False

        self._build()

    # ── layout ───────────────────────────────────────────────────────────
    def _build(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="top", fill="x", padx=12, pady=(12, 4))
        ctk.CTkLabel(bar, text="Plan зураг", font=ctk.CTkFont(size=18, weight="bold")).pack(
            side="left"
        )
        self._save_btn = ctk.CTkButton(
            bar,
            text="Хадгалах",
            width=110,
            fg_color=BRAND_ORANGE,
            hover_color=BRAND_ORANGE_HOVER,
            command=self._save,
        )
        self._save_btn.pack(side="right")
        self._spinner = widgets.Spinner(bar)
        self._spinner.pack(side="right", padx=6)
        self._status = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=12), text_color="gray60")
        self._status.pack(side="right", padx=8)

        # Tool row.
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.pack(side="top", fill="x", padx=12, pady=(0, 4))
        self._tool_btns: dict[str, ctk.CTkButton] = {}
        for key, label in (
            ("select", "↖ Сонгох"),
            ("wall", "▭ Хана"),
            ("shelf", "▦ Тавиур"),
            ("exit", "🚪 Гарц"),
            ("entrance", "↳ Орц"),
            ("checkout", "🛒 Касс"),
            ("camera", "📷 Камер"),
        ):
            b = ctk.CTkButton(
                tools,
                text=label,
                width=86,
                height=30,
                fg_color="transparent",
                border_width=1,
                command=lambda k=key: self._set_mode(k),
            )
            b.pack(side="left", padx=2)
            self._tool_btns[key] = b
        ctk.CTkButton(
            tools,
            text="⤢ Багтаах",
            width=86,
            height=30,
            fg_color="transparent",
            border_width=1,
            command=self._fit_view,
        ).pack(side="left", padx=(12, 2))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 12))

        holder = ctk.CTkFrame(body, fg_color=_BG, corner_radius=10)
        holder.pack(side="left", fill="both", expand=True, padx=(0, 10))
        holder.pack_propagate(False)
        self.canvas = tk.Canvas(holder, bg=_BG, bd=0, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Button-3>", self._on_right)
        self.canvas.bind("<MouseWheel>", self._on_wheel)  # Windows wheel
        self.canvas.bind("<Configure>", self._on_resize)

        side = ctk.CTkFrame(body, fg_color="transparent", width=240)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        ctk.CTkLabel(
            side,
            text="Байрлуулах камер",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(2, 2))
        self._cam_pick = ctk.CTkOptionMenu(side, values=["—"], command=lambda _v: None)
        self._cam_pick.pack(fill="x")
        ctk.CTkLabel(
            side,
            text="«Камер» горимд камер сонгоод зураг дээр дарж байрлуул. Сонгох "
            "горимд камерыг чирж зөөх / эргүүлэх. Хана/бүс — дарж булан нэмж, давхар "
            "дарж дуусга.",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
            justify="left",
            wraplength=220,
            anchor="w",
        ).pack(fill="x", pady=(6, 8))

        ctk.CTkLabel(
            side,
            text="Элементүүд",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(8, 2))
        self._elem_list = ctk.CTkScrollableFrame(side, fg_color="#161618")
        self._elem_list.pack(fill="both", expand=True)

        self._set_mode("select")

    # ── load / show ──────────────────────────────────────────────────────
    def on_show(self) -> None:
        """Called when the page becomes visible — load once + refresh camera picker."""
        self._refresh_cam_picker()
        if not self._loaded:
            self._loaded = True
            self._load()

    def _refresh_cam_picker(self) -> None:
        cams = [c for c in self._get_cameras() if c.mediamtx_path]
        names = [c.name for c in cams] or ["—"]
        self._cam_pick.configure(values=names)
        if self._cam_pick.get() not in names:
            self._cam_pick.set(names[0])

    def _load(self) -> None:
        self._spinner.start()
        self._set_status("Plan ачаалж байна…", "gray60")

        def work() -> dict[str, Any]:
            from sentry_agent_pc.backend_client import BackendClient

            return BackendClient().agent_get_floor_plan()

        self._run_bg(work, self._on_loaded)

    def _on_loaded(self, result: Any) -> None:
        self._spinner.stop()
        if isinstance(result, dict):
            self._plan = _normalize_plan(result)
            self._set_status("", "gray60")
        else:
            self._set_status("Plan ачаалж чадсангүй (хоосноос эхэлж болно).", "#FFB454")
        self._fitted = False
        self._redraw()

    # ── modes ────────────────────────────────────────────────────────────
    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._draft = []
        self._sel_cam = None
        for k, b in self._tool_btns.items():
            b.configure(fg_color=BRAND_ORANGE if k == mode else "transparent")
        self.canvas.configure(cursor="hand2" if mode == "select" else "crosshair")
        self._redraw()

    # ── canvas geometry ──────────────────────────────────────────────────
    def _on_resize(self, _e: object) -> None:
        if not self._fitted:
            self._fit_view()
        else:
            self._redraw()

    def _fit_view(self) -> None:
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        pw, ph = self._plan.get("size", DEFAULT_PLAN_SIZE)
        self._view.fit(float(pw), float(ph), w, h)
        self._fitted = True
        self._redraw()

    def _on_wheel(self, event: tk.Event) -> None:
        factor = 1.1 if event.delta > 0 else 1 / 1.1
        self._view.zoom_at(event.x, event.y, factor)
        self._redraw()

    # ── click handling ───────────────────────────────────────────────────
    def _on_click(self, event: tk.Event) -> None:
        px, py = self._view.to_plan(event.x, event.y)
        if self._mode == "select":
            self._select_at(event.x, event.y)
        elif self._mode == "camera":
            self._place_camera(px, py)
        elif self._mode in _FIXTURE_MODES or self._mode == "wall":
            self._draft.append((px, py))
            self._redraw()

    def _on_double(self, _e: object) -> None:
        if self._mode == "wall":
            self._finish_wall()
        elif self._mode in _FIXTURE_MODES:
            self._finish_fixture()

    def _on_right(self, _e: object) -> None:
        if self._draft:
            self._draft.pop()
            self._redraw()

    def _on_drag(self, event: tk.Event) -> None:
        last = self._last_drag
        self._last_drag = (event.x, event.y)
        if last is None:
            return
        dsx, dsy = event.x - last[0], event.y - last[1]
        if self._drag == "pan":
            self._view.pan_by_screen(dsx, dsy)
            self._redraw()
        elif self._drag == "cam" and self._sel_cam is not None:
            self._plan["cameras"][self._sel_cam]["pos"] = list(self._view.to_plan(event.x, event.y))
            self._redraw()
        elif self._drag == "dir" and self._sel_cam is not None:
            cam = self._plan["cameras"][self._sel_cam]
            csx, csy = self._view.to_screen(*cam["pos"])
            cam["dir_deg"] = angle_deg(csx, csy, event.x, event.y)
            self._redraw()

    def _on_release(self, _e: object) -> None:
        self._drag = None
        self._last_drag = None

    def _select_at(self, sx: int, sy: int) -> None:
        """Pick a camera body or its direction handle near (sx,sy); else start a pan."""
        self._last_drag = (sx, sy)
        cams = self._plan["cameras"]
        # Direction handle of the already-selected camera first (sits outside body).
        if self._sel_cam is not None and self._sel_cam < len(cams):
            cam = cams[self._sel_cam]
            csx, csy = self._view.to_screen(*cam["pos"])
            hx, hy = dir_handle(csx, csy, cam.get("dir_deg", 0.0), _DIR_LEN)
            if (sx - hx) ** 2 + (sy - hy) ** 2 <= (_HIT_PX**2):
                self._drag = "dir"
                return
        for i, cam in enumerate(cams):
            csx, csy = self._view.to_screen(*cam["pos"])
            if (sx - csx) ** 2 + (sy - csy) ** 2 <= (_HIT_PX**2):
                self._sel_cam = i
                self._drag = "cam"
                self._redraw()
                return
        self._sel_cam = None
        self._drag = "pan"
        self._redraw()

    # ── element mutation ─────────────────────────────────────────────────
    def _place_camera(self, px: float, py: float) -> None:
        name = self._cam_pick.get()
        cam = next((c for c in self._get_cameras() if c.name == name), None)
        cid = (cam.mediamtx_path if cam else None) or name
        # Re-place an existing camera rather than duplicating it.
        for c in self._plan["cameras"]:
            if c["camera_id"] == cid:
                c["pos"] = [px, py]
                self._redraw()
                return
        self._plan["cameras"].append(
            {"camera_id": cid, "name": name, "pos": [px, py], "dir_deg": 0.0, "homography": None}
        )
        self._refresh_elems()
        self._redraw()

    def _finish_fixture(self) -> None:
        if len(self._draft) < _MIN_FIXTURE_PTS:
            self._set_status("Бүс дор хаяж 3 цэгтэй.", "#FFB454")
            return
        self._plan["fixtures"].append(
            {"type": self._mode, "points": [[x, y] for x, y in self._draft]}
        )
        self._draft = []
        self._refresh_elems()
        self._redraw()

    def _finish_wall(self) -> None:
        if len(self._draft) < _MIN_WALL_PTS:
            self._set_status("Хана дор хаяж 2 цэгтэй.", "#FFB454")
            return
        self._plan["walls"].append({"points": [[x, y] for x, y in self._draft]})
        self._draft = []
        self._refresh_elems()
        self._redraw()

    def _delete(self, kind: str, idx: int) -> None:
        seq = self._plan.get(kind, [])
        if 0 <= idx < len(seq):
            seq.pop(idx)
            if kind == "cameras":
                self._sel_cam = None
            self._refresh_elems()
            self._redraw()

    # ── drawing ──────────────────────────────────────────────────────────
    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._draw_bounds()
        for w in self._plan["walls"]:
            self._draw_path(w["points"], WALL_COLOR, width=3, closed=False)
        for f in self._plan["fixtures"]:
            self._draw_path(
                f["points"],
                fixture_color(f["type"]),
                width=2,
                closed=True,
                label=fixture_label(f["type"]),
            )
        for i, cam in enumerate(self._plan["cameras"]):
            self._draw_camera(cam, selected=(i == self._sel_cam))
        if self._draft:
            col = WALL_COLOR if self._mode == "wall" else fixture_color(self._mode)
            self._draw_path(self._draft, col, width=2, closed=False, vertices=True)

    def _draw_bounds(self) -> None:
        pw, ph = self._plan.get("size", DEFAULT_PLAN_SIZE)
        x0, y0 = self._view.to_screen(0, 0)
        x1, y1 = self._view.to_screen(float(pw), float(ph))
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=_GRID, width=1)

    def _draw_path(
        self,
        pts_plan: list[tuple[float, float]] | list[list[float]],
        color: str,
        *,
        width: int,
        closed: bool,
        label: str | None = None,
        vertices: bool = False,
    ) -> None:
        c = self.canvas
        px = [self._view.to_screen(float(p[0]), float(p[1])) for p in pts_plan]
        if closed and len(px) >= 3:
            flat = [v for p in px for v in p]
            c.create_polygon(*flat, outline=color, width=width, fill=color, stipple="gray12")
            if label:
                c.create_text(
                    px[0][0] + 4,
                    px[0][1] - 8,
                    text=label,
                    fill=color,
                    anchor="w",
                    font=("Segoe UI", 10, "bold"),
                )
        else:
            for i in range(len(px) - 1):
                c.create_line(*px[i], *px[i + 1], fill=color, width=width)
        if vertices or not closed:
            for x, y in px:
                c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline="")

    def _draw_camera(self, cam: dict[str, Any], *, selected: bool) -> None:
        c = self.canvas
        sx, sy = self._view.to_screen(*cam["pos"])
        hx, hy = dir_handle(sx, sy, cam.get("dir_deg", 0.0), _DIR_LEN)
        c.create_line(sx, sy, hx, hy, fill=CAMERA_COLOR, width=2, arrow=tk.LAST)
        r = _HANDLE_R + (2 if selected else 0)
        c.create_oval(
            sx - r, sy - r, sx + r, sy + r, fill=CAMERA_COLOR, outline="white" if selected else ""
        )
        if selected:
            c.create_oval(hx - 4, hy - 4, hx + 4, hy + 4, fill="white", outline=CAMERA_COLOR)
        name = cam.get("name") or cam.get("camera_id", "?")
        c.create_text(
            sx + 8, sy - 8, text=name, fill=CAMERA_COLOR, anchor="w", font=("Segoe UI", 9, "bold")
        )

    # ── element list panel ───────────────────────────────────────────────
    def _refresh_elems(self) -> None:
        for ch in self._elem_list.winfo_children():
            ch.destroy()
        rows: list[tuple[str, int, str, str]] = []
        for i, cam in enumerate(self._plan["cameras"]):
            rows.append(("cameras", i, CAMERA_COLOR, f"📷 {cam.get('name') or cam['camera_id']}"))
        for i, f in enumerate(self._plan["fixtures"]):
            rows.append(("fixtures", i, fixture_color(f["type"]), f"▦ {fixture_label(f['type'])}"))
        for i, _w in enumerate(self._plan["walls"]):
            rows.append(("walls", i, WALL_COLOR, f"▭ Хана {i + 1}"))
        if not rows:
            ctk.CTkLabel(self._elem_list, text="Хоосон", text_color="gray50").pack(pady=10)
            return
        for kind, idx, color, text in rows:
            r = ctk.CTkFrame(self._elem_list, fg_color="transparent")
            r.pack(fill="x", pady=2, padx=2)
            ctk.CTkLabel(
                r, text=text, text_color=color, anchor="w", font=ctk.CTkFont(size=12)
            ).pack(side="left")
            ctk.CTkButton(
                r,
                text="✕",
                width=26,
                height=22,
                fg_color="transparent",
                border_width=1,
                text_color="#FF6B6B",
                border_color="#FF6B6B",
                hover_color="gray25",
                command=lambda k=kind, i=idx: self._delete(k, i),
            ).pack(side="right")

    # ── save ─────────────────────────────────────────────────────────────
    def _save(self) -> None:
        # Auto-finish a valid in-progress shape so it isn't lost on save.
        if self._mode == "wall" and len(self._draft) >= _MIN_WALL_PTS:
            self._finish_wall()
        elif self._mode in _FIXTURE_MODES and len(self._draft) >= _MIN_FIXTURE_PTS:
            self._finish_fixture()
        plan = _strip_for_save(self._plan)
        self._save_btn.configure(state="disabled")
        self._spinner.start()
        self._set_status("Хадгалж байна…", "gray60")

        def work() -> dict[str, Any]:
            from sentry_agent_pc.backend_client import BackendClient

            return BackendClient().agent_update_floor_plan(plan)

        self._run_bg(work, self._on_saved)

    def _on_saved(self, result: Any) -> None:
        self._spinner.stop()
        self._save_btn.configure(state="normal")
        if isinstance(result, dict):
            self._set_status("✅ Хадгалагдлаа.", "#4ADE80")
        else:
            self._set_status(f"❌ {result}", "#FF6B6B")

    # ── helpers ──────────────────────────────────────────────────────────
    def _set_status(self, text: str, color: str = "gray60") -> None:
        with contextlib.suppress(Exception):
            self._status.configure(text=text, text_color=color)

    def _run_bg(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def runner() -> None:
            try:
                result: Any = work()
            except Exception as e:  # noqa: BLE001
                log.exception("floor_plan_bg_failed")
                result = str(e)
            with contextlib.suppress(Exception):
                self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()


def _empty_plan() -> dict[str, Any]:
    return {
        "version": 1,
        "size": list(DEFAULT_PLAN_SIZE),
        "walls": [],
        "fixtures": [],
        "cameras": [],
    }


def _normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a backend plan dict into the editor's working shape (lists, defaults)."""
    p = _empty_plan()
    with contextlib.suppress(Exception):
        if raw.get("size"):
            p["size"] = [float(raw["size"][0]), float(raw["size"][1])]
        p["walls"] = [
            {"points": [[float(x), float(y)] for x, y in w["points"]]} for w in raw.get("walls", [])
        ]
        p["fixtures"] = [
            {
                "id": f.get("id"),
                "type": f["type"],
                "points": [[float(x), float(y)] for x, y in f["points"]],
            }
            for f in raw.get("fixtures", [])
        ]
        p["cameras"] = [
            {
                "camera_id": c["camera_id"],
                "name": c.get("name"),
                "pos": [float(c["pos"][0]), float(c["pos"][1])],
                "dir_deg": float(c.get("dir_deg", 0.0)),
                "homography": c.get("homography"),
                "reproj_err": c.get("reproj_err"),
                "calib_points": c.get("calib_points"),
            }
            for c in raw.get("cameras", [])
        ]
    return p


def _strip_for_save(plan: dict[str, Any]) -> dict[str, Any]:
    """Drop the editor-only camera `name` key the backend schema doesn't define,
    so a PATCH validates cleanly. None-valued homography/calib stay (schema-optional)."""
    out = {k: v for k, v in plan.items() if k != "cameras"}
    out["cameras"] = [{k: v for k, v in cam.items() if k != "name"} for cam in plan["cameras"]]
    return out
