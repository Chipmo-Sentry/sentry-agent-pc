"""Zone editor — draw detection polygons on a camera's freeze-frame (docs/29 P1a).

Flow: grab ONE still frame off-thread → show it on a tk.Canvas → the user clicks
to drop polygon vertices, double-clicks (or "Зон дуусгах") to close a polygon, and
picks a type (Гарц/Тавиур/Касс/Орц) per zone. Save normalizes every polygon to
0-1 image space and PATCHes the backend (camera-CRUD), which the cloud/edge
behavior engine then consumes (exit_after_concealment / repeated_shelf_visit).

Coordinates are stored NORMALIZED against the displayed image rect (zone_geometry),
so they survive a window resize and any pull/draw resolution mismatch. All zone
state lives in normalized space; the canvas is just a projection of it.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from sentry_agent_pc.gui import widgets
from sentry_agent_pc.gui.widgets import BRAND_PRIMARY, BRAND_PRIMARY_HOVER
from sentry_agent_pc.gui.zone_geometry import (
    ZONE_TYPES,
    FitRect,
    fit_rect,
    to_norm,
    to_px,
    zone_color,
    zone_label,
)
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.state import CameraRecord

if TYPE_CHECKING:
    import tkinter as tk

    from PIL import Image

    from sentry_agent_pc.discovery.frame_grab import StillResult

log = get_logger("sentry_agent_pc.gui.zone_editor")

_MIN_POLYGON_POINTS = 3
# Mirror the backend caps (schemas/camera.py MAX_ZONE_POINTS / MAX_ZONES_PER_CAMERA)
# so the editor refuses to exceed them with a clear message instead of letting the
# user draw past the limit and hit an opaque 422 only on Save.
_MAX_POLYGON_POINTS = 512
_MAX_ZONES = 64
# Below this normalized distance two consecutive vertices are treated as the same
# point — collapses the duplicate vertices a double-click leaves (Tk fires two
# <Button-1> before <Double-Button-1>, both at the close location).
_DEDUP_EPS = 0.004
_CANVAS_BG = "#0E0E10"
_VERTEX_R = 4  # vertex handle radius (canvas px)


def _dedup_consecutive(
    pts: list[tuple[float, float]], eps: float = _DEDUP_EPS
) -> list[tuple[float, float]]:
    """Drop consecutive near-identical vertices (within `eps` in normalized space).

    Pure + testable. Collapses the duplicate point(s) a double-click leaves (Tk
    fires <Button-1> twice at the close location before <Double-Button-1>), and
    guards against zero-area degenerate edges in general."""
    out: list[tuple[float, float]] = []
    for x, y in pts:
        if not out or abs(out[-1][0] - x) > eps or abs(out[-1][1] - y) > eps:
            out.append((x, y))
    return out


class ZoneEditorDialog(ctk.CTkToplevel):
    """Draw + edit a camera's detection zones on a captured still frame."""

    def __init__(self, master: ctk.CTk, camera: CameraRecord, on_done: Callable[[], None]) -> None:
        super().__init__(master)
        self.cam = camera
        self.on_done = on_done

        # All zone state is in NORMALIZED (0-1) image space.
        self._zones: list[dict[str, Any]] = []  # completed: {id?, type, points:[(nx,ny)]}
        self._current: list[tuple[float, float]] = []  # in-progress polygon vertices
        self._cur_type: str = ZONE_TYPES[0].key

        self._img: Image.Image | None = None
        self._photo: object | None = None  # ImageTk.PhotoImage ref (Tk GC guard)
        self._rect: FitRect = FitRect(0.0, 0.0, 0.0, 0.0)
        self._dirty = False

        self.title(f"Зон засах — {camera.name}")
        self.transient(master)
        self.grab_set()
        widgets.setup_dialog(self, 1080, 760, min_width=820, min_height=560)
        # Closing the window (X) routes through the unsaved-changes guard too.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_buttons()
        self._build_body()
        self._load_existing_zones()
        self._start_grab()

    # ── layout ───────────────────────────────────────────────────────────
    def _build_buttons(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=20, pady=14)
        ctk.CTkButton(
            bar,
            text="Болих",
            fg_color="transparent",
            border_width=1,
            command=self._on_close,
        ).pack(side="right", padx=(8, 0))
        self.save_btn = ctk.CTkButton(
            bar,
            text="Хадгалах",
            fg_color=BRAND_PRIMARY,
            hover_color=BRAND_PRIMARY_HOVER,
            command=self._submit,
        )
        self.save_btn.pack(side="right")
        self.status_lbl = ctk.CTkLabel(
            bar,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
            anchor="w",
        )
        self.status_lbl.pack(side="left", padx=4)
        self.spinner = widgets.Spinner(bar)
        self.spinner.pack(side="left")

    def _build_body(self) -> None:
        import tkinter as tk

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=14, pady=(12, 0))

        # Canvas (left) — fixed-pixel holder so it never collapses before a frame.
        holder = ctk.CTkFrame(body, fg_color=_CANVAS_BG, corner_radius=10)
        holder.pack(side="left", fill="both", expand=True, padx=(0, 12))
        holder.pack_propagate(False)
        self.canvas = tk.Canvas(
            holder, bg=_CANVAS_BG, bd=0, highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)  # undo last vertex
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # Controls (right).
        side = ctk.CTkFrame(body, fg_color="transparent", width=280)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        ctk.CTkLabel(
            side,
            text="Зоны төрөл",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(2, 4))
        self._type_picker = ctk.CTkSegmentedButton(
            side,
            values=[z.label for z in ZONE_TYPES],
            command=self._on_type_change,
        )
        self._type_picker.set(ZONE_TYPES[0].label)
        self._type_picker.pack(fill="x")
        self._swatch = ctk.CTkLabel(
            side,
            text="●  идэвхтэй өнгө",
            anchor="w",
            text_color=zone_color(self._cur_type),
            font=ctk.CTkFont(size=12),
        )
        self._swatch.pack(fill="x", pady=(4, 10))

        ctk.CTkLabel(
            side,
            text="Зураг дээр дарж булангуудыг тэмдэглэ. 3+ цэг тэмдэглээд "
            "давхар дарж эсвэл доорх товчоор зоныг хаа.",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
            justify="left",
            wraplength=260,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        row = ctk.CTkFrame(side, fg_color="transparent")
        row.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(
            row,
            text="✓ Зон дуусгах",
            height=30,
            command=self._finish_current,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            row,
            text="↶ Цэг",
            width=64,
            height=30,
            fg_color="transparent",
            border_width=1,
            command=self._undo_point,
        ).pack(side="left")

        ctk.CTkLabel(
            side,
            text="Зонууд",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(fill="x", pady=(12, 2))
        self._zone_list = ctk.CTkScrollableFrame(side, fg_color="#161618", height=240)
        self._zone_list.pack(fill="both", expand=True)

        ctk.CTkButton(
            side,
            text="Бүгдийг арилгах",
            height=28,
            fg_color="transparent",
            border_width=1,
            text_color="#FF6B6B",
            border_color="#FF6B6B",
            hover_color="gray25",
            command=self._clear_all,
        ).pack(fill="x", pady=(8, 2))

    # ── still-frame grab ─────────────────────────────────────────────────
    def _start_grab(self) -> None:
        self.save_btn.configure(state="disabled")
        self.spinner.start()
        self._status("Камераас зураг авч байна…", "gray60")

        def work() -> StillResult:
            from sentry_agent_pc.discovery import frame_grab

            return frame_grab.grab_still(self.cam)

        self._run_bg(work, self._on_grabbed)

    def _on_grabbed(self, result: Any) -> None:
        self.spinner.stop()
        if not getattr(result, "ok", False) or result.image is None:
            self._status(f"❌ {getattr(result, 'error', 'Зураг аваагүй')}", "#FF6B6B")
            return
        self._img = result.image
        self.save_btn.configure(state="normal")
        src = "HTTP снапшот" if result.source == "snapshot" else "RTSP"
        self._status(
            f"✓ Зураг авлаа ({result.width}×{result.height}, {src}). Зоноо зураарай.",
            "#4ADE80",
        )
        self._recompute_rect()
        self._redraw()

    # ── canvas geometry ──────────────────────────────────────────────────
    def _recompute_rect(self) -> None:
        if self._img is None:
            return
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        self._rect = fit_rect(self._img.width, self._img.height, w, h)

    def _on_canvas_resize(self, _e: object) -> None:
        # <Configure> fires many times/sec during a drag; rebuilding the
        # full-res PhotoImage each time janks the UI thread. Skip when the fit
        # rect is effectively unchanged (matches LocalLiveView._on_resize).
        if self._img is None:
            return
        prev = (round(self._rect.disp_w), round(self._rect.disp_h))
        self._recompute_rect()
        if (round(self._rect.disp_w), round(self._rect.disp_h)) == prev:
            return
        self._redraw()

    # ── drawing ──────────────────────────────────────────────────────────
    def _redraw(self) -> None:
        from PIL import ImageTk

        c = self.canvas
        c.delete("all")
        if self._img is None or self._rect.disp_w <= 0:
            return
        disp = self._img.resize((max(1, int(self._rect.disp_w)), max(1, int(self._rect.disp_h))))
        photo = ImageTk.PhotoImage(disp)
        self._photo = photo  # keep ref
        c.create_image(self._rect.off_x, self._rect.off_y, anchor="nw", image=photo)

        for z in self._zones:
            self._draw_polygon(
                z["points"], zone_color(z["type"]), closed=True, label=zone_label(z["type"])
            )
        if self._current:
            self._draw_polygon(self._current, zone_color(self._cur_type), closed=False)

    def _draw_polygon(
        self,
        pts_norm: list[tuple[float, float]],
        color: str,
        *,
        closed: bool,
        label: str | None = None,
    ) -> None:
        c = self.canvas
        px = [to_px(nx, ny, self._rect) for nx, ny in pts_norm]
        if closed and len(px) >= _MIN_POLYGON_POINTS:
            flat = [coord for p in px for coord in p]
            c.create_polygon(
                *flat,
                outline=color,
                width=2,
                fill=color,
                stipple="gray12",
            )
            if label:
                lx, ly = px[0]
                c.create_text(
                    lx + 4,
                    ly - 8,
                    text=label,
                    fill=color,
                    anchor="w",
                    font=("Segoe UI", 10, "bold"),
                )
        else:
            for i in range(len(px) - 1):
                c.create_line(*px[i], *px[i + 1], fill=color, width=2)
        for x, y in px:
            c.create_oval(
                x - _VERTEX_R,
                y - _VERTEX_R,
                x + _VERTEX_R,
                y + _VERTEX_R,
                fill=color,
                outline="white",
                width=1,
            )

    # ── canvas events ────────────────────────────────────────────────────
    def _on_click(self, event: tk.Event) -> None:
        if self._img is None:
            return
        if len(self._current) >= _MAX_POLYGON_POINTS:
            self._status(f"Нэг зон дээд тал {_MAX_POLYGON_POINTS} цэгтэй.", "#FFB454")
            return
        self._current.append(to_norm(event.x, event.y, self._rect))
        self._redraw()

    def _on_double_click(self, _e: object) -> None:
        self._finish_current()

    def _on_right_click(self, _e: object) -> None:
        self._undo_point()

    def _undo_point(self) -> None:
        if self._current:
            self._current.pop()
            self._redraw()

    def _finish_current(self) -> None:
        pts = _dedup_consecutive(self._current)  # drop double-click duplicate vertices
        if len(pts) < _MIN_POLYGON_POINTS:
            self._status("Зон дор хаяж 3 цэгтэй байх ёстой.", "#FFB454")
            return
        if len(self._zones) >= _MAX_ZONES:
            self._status(f"Нэг камер дээд тал {_MAX_ZONES} зонтой.", "#FFB454")
            return
        self._zones.append({"type": self._cur_type, "points": pts})
        self._current = []
        self._dirty = True
        self._refresh_zone_list()
        self._redraw()
        self._status("Зон нэмэгдлээ. Дараагийн зоноо зураарай эсвэл хадгална уу.", "gray60")

    def _on_type_change(self, label: str) -> None:
        for z in ZONE_TYPES:
            if z.label == label:
                self._cur_type = z.key
                break
        self._swatch.configure(text_color=zone_color(self._cur_type))
        self._redraw()

    def _clear_all(self) -> None:
        if not self._zones and not self._current:
            return
        self._zones = []
        self._current = []
        self._dirty = True
        self._refresh_zone_list()
        self._redraw()
        self._status("Бүх зон арилгагдлаа. Хадгалбал серверээс ч устана.", "gray60")

    # ── zone list panel ──────────────────────────────────────────────────
    def _refresh_zone_list(self) -> None:
        for child in self._zone_list.winfo_children():
            child.destroy()
        if not self._zones:
            ctk.CTkLabel(
                self._zone_list,
                text="Зон алга",
                text_color="gray50",
                font=ctk.CTkFont(size=12),
            ).pack(pady=10)
            return
        for idx, z in enumerate(self._zones):
            r = ctk.CTkFrame(self._zone_list, fg_color="transparent")
            r.pack(fill="x", pady=2, padx=2)
            ctk.CTkLabel(
                r,
                text=f"●  {zone_label(z['type'])}  ·  {len(z['points'])} цэг",
                text_color=zone_color(z["type"]),
                anchor="w",
                font=ctk.CTkFont(size=12),
            ).pack(side="left")
            ctk.CTkButton(
                r,
                text="✕",
                width=28,
                height=24,
                fg_color="transparent",
                border_width=1,
                text_color="#FF6B6B",
                border_color="#FF6B6B",
                hover_color="gray25",
                command=lambda i=idx: self._delete_zone(i),
            ).pack(side="right")

    def _delete_zone(self, idx: int) -> None:
        if 0 <= idx < len(self._zones):
            self._zones.pop(idx)
            self._dirty = True
            self._refresh_zone_list()
            self._redraw()

    # ── load + save ──────────────────────────────────────────────────────
    def _load_existing_zones(self) -> None:
        """Hydrate the editor from the camera's stored zones (normalized already)."""
        for z in self.cam.zones or []:
            pts = z.get("points") or []
            norm = [(float(p[0]), float(p[1])) for p in pts if len(p) >= 2]
            if len(norm) >= _MIN_POLYGON_POINTS:
                self._zones.append(
                    {"id": z.get("id"), "type": z.get("type", "shelf"), "points": norm}
                )
        self._refresh_zone_list()

    def _payload(self) -> list[dict[str, Any]]:
        """Build the backend zones payload (points as [[nx,ny],...] lists)."""
        out: list[dict[str, Any]] = []
        for z in self._zones:
            entry: dict[str, Any] = {
                "type": z["type"],
                "points": [[round(nx, 5), round(ny, 5)] for nx, ny in z["points"]],
            }
            if z.get("id"):
                entry["id"] = z["id"]
            out.append(entry)
        return out

    def _submit(self) -> None:
        if not self.cam.uuid:
            self._status("Энэ камер бүртгэлгүй тул зон хадгалах боломжгүй.", "#FF6B6B")
            return
        # Auto-finish a started-but-unclosed polygon if it's already valid.
        if len(self._current) >= _MIN_POLYGON_POINTS:
            self._finish_current()
        elif self._current:
            self._status("Дуусгаагүй зон байна — 3+ цэг тэмдэглэ эсвэл цэгийг устга.", "#FFB454")
            return

        payload = self._payload()
        self.save_btn.configure(state="disabled")
        self.spinner.start()
        self._status("Хадгалж байна…", "gray60")

        def work() -> svc.RegisterResult:
            return svc.save_camera_zones(camera_uuid=self.cam.uuid or "", zones=payload)

        self._run_bg(work, self._on_saved)

    def _on_saved(self, result: Any) -> None:
        self.spinner.stop()
        self.save_btn.configure(state="normal")
        if getattr(result, "ok", False):
            n = len(self._zones)
            self._status(f"✅ {n} зон хадгалагдлаа.", "#4ADE80")
            self.cam.zones = self._payload() or None
            self._dirty = False
            with contextlib.suppress(Exception):
                self.on_done()
            self.after(1100, self.destroy)
        else:
            self._status(f"❌ {getattr(result, 'error', 'алдаа')}", "#FF6B6B")

    # ── close ────────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        """Confirm before discarding unsaved drawn zones (data-loss guard)."""
        unsaved = self._dirty or len(self._current) >= _MIN_POLYGON_POINTS
        if unsaved:
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Хадгалаагүй зон",
                "Хадгалаагүй өөрчлөлт байна. Хадгалахгүйгээр хаах уу?",
                parent=self,
            ):
                return
        self.destroy()

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, text: str, color: str = "gray60") -> None:
        with contextlib.suppress(Exception):  # label may be gone mid-close
            self.status_lbl.configure(text=text, text_color=color)

    def _run_bg(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("zone_editor_bg_failed")
                result = svc.RegisterResult(ok=False, error=str(e))
            with contextlib.suppress(Exception):  # window closed mid-task
                self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()
