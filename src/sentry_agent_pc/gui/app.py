"""Main desktop window — camera list + Scan/Add + Settings.

CustomTkinter native window. Long-running ops (ONVIF scan, RTSP probe,
backend calls) run on background threads and post results back to the UI
thread via `self.after(...)` to avoid freezing the window.
"""

from __future__ import annotations

import contextlib
import os
import platform
import threading
import tkinter as tk
import urllib.parse
from collections.abc import Callable
from typing import Any

import customtkinter as ctk
from PIL import Image

from sentry_agent_pc import __version__, resources, updater
from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.config_file import (
    DEFAULT_BACKEND_URL,
    DEFAULT_FRONTEND_URL,
    read_config,
    write_config,
)
from sentry_agent_pc.edge.recorder import ClipRecord, ClipStore
from sentry_agent_pc.gui import widgets
from sentry_agent_pc.gui.add_dialog import AddCameraDialog
from sentry_agent_pc.gui.edit_dialog import EditCameraDialog
from sentry_agent_pc.gui.scan_dialog import ScanDialog
from sentry_agent_pc.gui.tray import TrayController
from sentry_agent_pc.gui.update_dialog import (
    UpdateDialog,
    auto_update_in_background,
    check_in_background,
)
from sentry_agent_pc.gui.widgets import BRAND_ORANGE, BRAND_ORANGE_HOVER
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.settings import get_settings
from sentry_agent_pc.state import CameraRecord, load_state, save_state
from sentry_agent_pc.streaming.controller import get_stream_controller

log = get_logger("sentry_agent_pc.gui.app")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Brand palette (Chipmo): navy + orange. CustomTkinter's "blue" theme colors
# every unstyled button a bright generic blue that clashed with our orange CTAs.
# Override the default button colour to brand navy so the app reads as one
# coherent navy + orange scheme; explicit orange stays the primary-action accent.
# The orange accent + hover live in widgets.py so every gui file shares one value
# (the header used to carry its own slightly-different #E68425 tone).
BRAND_NAVY = "#2A4A73"
BRAND_NAVY_HOVER = "#36598A"
CHIPMO_ORANGE = BRAND_ORANGE  # module-wide alias (kept for the existing call sites)


def _theme(widget: str, key: str, value: object) -> None:
    """Set one ThemeManager colour, ignoring keys a CTk version doesn't have."""
    with contextlib.suppress(KeyError, TypeError):
        ctk.ThemeManager.theme[widget][key] = value


_navy = [BRAND_NAVY, BRAND_NAVY]
_navy_hover = [BRAND_NAVY_HOVER, BRAND_NAVY_HOVER]
_orange = [BRAND_ORANGE, BRAND_ORANGE]
# Buttons → navy (orange stays the explicit primary-action accent).
_theme("CTkButton", "fg_color", _navy)
_theme("CTkButton", "hover_color", _navy_hover)
# Dropdowns (the camera-brand picker) were bright blue → navy + orange hover.
_theme("CTkOptionMenu", "fg_color", _navy)
_theme("CTkOptionMenu", "button_color", _navy_hover)
_theme("CTkOptionMenu", "button_hover_color", _orange)
_theme("CTkComboBox", "button_color", _navy)
_theme("CTkComboBox", "button_hover_color", _navy_hover)
_theme("CTkComboBox", "border_color", _navy)
# Inputs: orange focus ring (brand) instead of the default blue.
_theme("CTkEntry", "border_color", _navy)
# Toggles / progress / sliders → brand orange so nothing reads generic-blue.
_theme("CTkCheckBox", "fg_color", _orange)
_theme("CTkCheckBox", "hover_color", _orange)
_theme("CTkSwitch", "progress_color", _orange)
_theme("CTkProgressBar", "progress_color", _orange)
_theme("CTkSlider", "progress_color", _orange)
_theme("CTkSlider", "button_color", _orange)
_theme("CTkSlider", "button_hover_color", _orange)


def creds_from_rtsp(rtsp_url: str) -> tuple[str | None, str]:
    """Pull (user, password) out of an ``rtsp://user:pass@host/...`` URL so a
    camera can be re-resolved from its IP when its stored path goes stale.
    ``user`` is None when the URL carries no credentials; password is always a
    string (empty when absent)."""
    try:
        parts = urllib.parse.urlsplit(rtsp_url)
        if parts.username is None:
            return None, ""
        return (
            urllib.parse.unquote(parts.username),
            urllib.parse.unquote(parts.password or ""),
        )
    except ValueError:
        return None, ""


class AgentApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sentry — Камерын агент")
        self.geometry("960x640")
        self.minsize(820, 520)

        # Per-camera push-status labels, keyed by mediamtx_path (rebuilt each
        # refresh). Updated by the periodic _tick_push_status loop.
        self._push_labels: dict[str, ctk.CTkLabel] = {}

        # Teardown guard: set in quit_app() so a pending self-rescheduling `after`
        # tick (or a background done() callback) can't touch a destroyed widget
        # after destroy() (TclError). Each periodic loop stores its pending id here
        # so quit_app() can cancel the in-flight tick too.
        self._closing = False
        self._after_ids: dict[str, str] = {}

        self._set_window_icon()

        self._build_header()
        self._build_statusbar()  # bottom
        # Body: a left sidebar (page nav) + a right content area holding the
        # switchable pages. The camera table keeps its exact logic — it's just
        # reparented into the "cameras" page.
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True)
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._build_sidebar(body)
        self._content = ctk.CTkFrame(body, fg_color="transparent")
        self._content.pack(side="left", fill="both", expand=True)

        self._pages: dict[str, ctk.CTkFrame] = {}
        self._page_cameras = ctk.CTkFrame(self._content, fg_color="transparent")
        self._build_toolbar(self._page_cameras)
        self._build_camera_list(self._page_cameras)
        self._pages["cameras"] = self._page_cameras
        self._pages["alerts"] = self._build_alerts_page(self._content)
        self._pages["settings"] = self._build_settings_page(self._content)
        self._show_page("cameras")

        self.refresh_cameras()
        self._check_backend_async()
        # First run / unpaired → guide the user straight to the pairing screen.
        # Routed through _schedule so quit_app() cancels it — quitting within the
        # 400ms must not pop a pairing dialog after the window is destroyed.
        if not load_state().is_paired:
            self._schedule("open_pairing", 400, self.open_pairing)
        # Self-update: silent check shortly after launch, then on a periodic timer.
        # With auto_update on (default) a newer release is downloaded + applied
        # automatically; this flag stops overlapping checks from stacking it.
        self._update_in_progress = False
        self._schedule("auto_check_update", 2500, self._auto_check_update)
        # Live push status indicators (5s) + periodic stream-config reconcile (30s).
        self._schedule("push_status", 5000, self._tick_push_status)
        self._schedule("periodic_refresh", 30000, self._tick_periodic_refresh)
        # Periodic heartbeat (30s) — keeps the computer "online" in the cloud. The
        # web UI marks it offline after 120s without a beat, so a one-time beat at
        # startup made the PC flip offline a couple minutes after launch.
        self._schedule("heartbeat", 30000, self._tick_heartbeat)

        # System tray icon + minimize-to-tray (closing hides instead of quitting).
        self._tray = TrayController(self)
        self._tray.start()
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    # === Layout ===

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, height=56, corner_radius=0)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        # Brand lockup: the Chipmo "C" mark + "Sentry" wordmark (replaces the
        # placeholder shield emoji). The CTkImage ref is kept on self so Tk
        # doesn't garbage-collect it.
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left", padx=16)
        try:
            _logo = Image.open(resources.logo_header_png())
            self._logo_img = ctk.CTkImage(light_image=_logo, dark_image=_logo, size=(26, 26))
            ctk.CTkLabel(brand, image=self._logo_img, text="").pack(side="left", padx=(0, 9))
        except Exception as e:  # noqa: BLE001 — logo is cosmetic; fall back to text
            log.debug("header.logo_failed", error=str(e))
        ctk.CTkLabel(
            brand,
            text="Sentry",
            font=ctk.CTkFont(size=19, weight="bold"),
            text_color="#FFFFFF",
        ).pack(side="left")

        self.backend_label = ctk.CTkLabel(
            header,
            text="Backend: шалгаж байна…",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
        )
        self.backend_label.pack(side="left", padx=8)

        ctk.CTkButton(
            header,
            text="🔗 Холболт",
            width=110,
            command=self.open_pairing,
        ).pack(side="right", padx=(8, 16))

        ctk.CTkButton(
            header,
            text="⬆ Шинэчлэл",
            width=110,
            fg_color="transparent",
            border_width=1,
            command=self.open_update,
        ).pack(side="right", padx=4)

        ctk.CTkLabel(
            header,
            text=f"v{__version__}",
            font=ctk.CTkFont(size=11),
            text_color="gray50",
        ).pack(side="right", padx=4)

    def _build_toolbar(self, parent: ctk.CTkBaseClass) -> None:
        bar = ctk.CTkFrame(parent, height=56, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(12, 4))

        ctk.CTkButton(
            bar,
            text="🔍  Камер хайх (Scan)",
            width=180,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.open_scan,
        ).pack(side="left")

        ctk.CTkButton(
            bar,
            text="➕  Камер нэмэх (Add)",
            width=180,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=CHIPMO_ORANGE,
            hover_color=BRAND_ORANGE_HOVER,
            command=self.open_add,
        ).pack(side="left", padx=(10, 0))

        ctk.CTkButton(
            bar,
            text="↻ Сэргээх",
            width=110,
            height=40,
            fg_color="transparent",
            border_width=1,
            command=self.refresh_cameras,
        ).pack(side="right")
        # NB: "Шууд харах" lives in the left sidebar nav (a global action) — no
        # duplicate button here. The toolbar holds only camera-list actions
        # (Scan / Add / Refresh).

    # Fluid data columns: (title, weight, minsize). Columns expand to fill the
    # window width on a wide screen and shrink (to minsize) on a narrow one —
    # the same weights are applied to the header AND every row so they line up.
    _COLUMNS: tuple[tuple[str, int, int], ...] = (
        ("Нэр", 3, 120),
        ("IP", 2, 90),
        ("Path", 2, 80),
        ("Codec", 1, 55),
        ("Чанар", 2, 80),
        ("Push", 1, 70),
    )
    _ACTIONS_MINSIZE = 280  # fixed column for the 3 per-row action buttons

    def _configure_grid(self, frame: ctk.CTkBaseClass) -> None:
        """Apply the shared column weights/minsizes to a header or row frame."""
        for i, (_t, weight, minsize) in enumerate(self._COLUMNS):
            frame.grid_columnconfigure(i, weight=weight, minsize=minsize)
        frame.grid_columnconfigure(len(self._COLUMNS), weight=0, minsize=self._ACTIONS_MINSIZE)

    def _build_camera_list(self, parent: ctk.CTkBaseClass) -> None:
        # Column headers — grid with the shared weights. Extra right pad ≈ the
        # scrollable-frame scrollbar so the header lines up with the rows below.
        head = ctk.CTkFrame(parent, fg_color="gray20", height=34)
        head.pack(fill="x", padx=16, pady=(8, 0))
        head.pack_propagate(False)
        self._configure_grid(head)
        for i, (text, _w, _m) in enumerate(self._COLUMNS):
            ctk.CTkLabel(
                head,
                text=text,
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="gray80",
            ).grid(row=0, column=i, sticky="w", padx=6)
        ctk.CTkLabel(
            head,
            text="Үйлдэл",
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="gray80",
        ).grid(row=0, column=len(self._COLUMNS), sticky="w", padx=6)

        self.list_frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=28, corner_radius=0)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_label = ctk.CTkLabel(
            bar,
            text="Бэлэн",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
        )
        self.status_label.pack(side="left", padx=16)

    # === Sidebar navigation + pages ===

    _NAV: tuple[tuple[str, str, str], ...] = (
        ("cameras", "📷  Камерууд", "page"),
        ("live", "📺  Шууд харах", "action"),
        ("alerts", "⚠  Сэжигтэй", "page"),
        ("settings", "⚙  Тохиргоо", "page"),
    )

    def _build_sidebar(self, parent: ctk.CTkBaseClass) -> None:
        side = ctk.CTkFrame(parent, width=170, corner_radius=0, fg_color="gray17")
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        for key, label, kind in self._NAV:
            cmd = self.open_live_view if kind == "action" else self._page_cmd(key)
            btn = ctk.CTkButton(
                side, text=label, anchor="w", height=40, corner_radius=8,
                fg_color="transparent", text_color="gray85", hover_color="gray25",
                font=ctk.CTkFont(size=14), command=cmd,
            )
            btn.pack(fill="x", padx=10, pady=(10 if key == "cameras" else 2, 2))
            if kind == "page":
                self._nav_buttons[key] = btn
        # Edge-AI status pinned to the bottom so "is the AI running" is always visible.
        self._edge_status_label = ctk.CTkLabel(
            side, text=self._edge_status_text(), anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color="gray60", wraplength=148,
        )
        self._edge_status_label.pack(side="bottom", fill="x", padx=12, pady=12)

    def _page_cmd(self, key: str) -> Callable[[], None]:
        """A nav-button command that shows `key`'s page (binds key, no loop closure bug)."""
        return lambda: self._show_page(key)

    def _show_page(self, name: str) -> None:
        for page in self._pages.values():
            page.pack_forget()
        self._pages[name].pack(fill="both", expand=True)
        for key, btn in self._nav_buttons.items():
            btn.configure(fg_color=CHIPMO_ORANGE if key == name else "transparent")
        if name == "alerts":
            self._refresh_alerts()

    def _edge_status_text(self) -> str:
        """Human-readable edge-AI readiness for the sidebar / settings."""
        if not get_settings().edge_ai_enabled:
            return "AI: унтраалттай"
        try:
            import openvino  # noqa: F401

            from sentry_agent_pc.edge.ov_lean import bundled_model_xml

            if bundled_model_xml("yolo11n-pose_openvino_model") is None:
                return "⚠ AI: модель алга"
        except Exception:  # noqa: BLE001 — openvino not bundled → AI off, not a crash
            return "⚠ AI: OpenVINO алга"
        # A runtime failure recorded by a live-view reader thread (build/infer
        # gave up) — surface it instead of a misleading "ready" so a silently
        # dark edge AI is visible.
        from sentry_agent_pc.gui.local_view import last_edge_error

        err = last_edge_error()
        if err:
            return f"⚠ AI алдаа: {err[:60]}"
        return "🟢 AI: OpenVINO бэлэн"

    def _clip_store(self) -> ClipStore:
        from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR

        return ClipStore(DEFAULT_CONFIG_DIR / "edge" / "clips.json")

    def _build_alerts_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            bar, text="Сэжигтэй бичлэгүүд", font=ctk.CTkFont(size=16, weight="bold")
        ).pack(side="left")
        ctk.CTkButton(
            bar, text="↻ Сэргээх", width=100, height=32, fg_color="transparent",
            border_width=1, command=self._refresh_alerts,
        ).pack(side="right")
        self._alerts_frame = ctk.CTkScrollableFrame(page, fg_color="transparent")
        self._alerts_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        return page

    def _refresh_alerts(self) -> None:
        for w in self._alerts_frame.winfo_children():
            w.destroy()
        try:
            clips = self._clip_store().records()
        except Exception:  # noqa: BLE001 — a corrupt index must not break the page
            clips = []
        if not clips:
            ctk.CTkLabel(
                self._alerts_frame,
                text="Сэжигтэй бичлэг алга.\n\nAI сэжигтэй үйлдэл илрүүлбэл\n[−3с … +3с] бичлэг энд гарч ирнэ.",
                text_color="gray60", justify="center",
            ).pack(pady=50)
            return
        for clip in sorted(clips, key=lambda r: r.created_at, reverse=True):
            self._render_clip_row(clip)

    def _render_clip_row(self, clip: ClipRecord) -> None:
        import datetime

        row = ctk.CTkFrame(self._alerts_frame, fg_color="gray17", corner_radius=8)
        row.pack(fill="x", pady=4)
        when = datetime.datetime.fromtimestamp(clip.started_at).strftime("%m-%d %H:%M:%S")
        color = "#FF6B6B" if clip.risk_pct >= 70 else (CHIPMO_ORANGE if clip.risk_pct >= 40 else "gray70")
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        ctk.CTkLabel(
            info, text=f"{clip.camera_id} · {when}", anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w")
        beh = ", ".join(clip.behaviors) or "—"
        ctk.CTkLabel(
            info, text=f"Risk {clip.risk_pct:.0f}%  ·  {beh}  ·  {clip.duration:.0f}с",
            anchor="w", font=ctk.CTkFont(size=11), text_color=color,
        ).pack(anchor="w")
        ctk.CTkButton(
            row, text="▶ Нээх", width=72, height=28, fg_color="transparent",
            border_width=1, command=lambda p=clip.path: self._open_clip(p),
        ).pack(side="right", padx=(4, 12), pady=8)

    def _open_clip(self, path: str) -> None:
        opener = getattr(os, "startfile", None)  # Windows default player (None elsewhere)
        if opener is not None:
            with contextlib.suppress(Exception):
                opener(path)
                self.set_status(f"Бичлэг нээж байна: {path}")
                return
        self.set_status(f"Бичлэг: {path}")

    def _build_settings_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(
            page, text="Тохиргоо", font=ctk.CTkFont(size=16, weight="bold")
        ).pack(anchor="w", padx=16, pady=(14, 8))
        card = ctk.CTkFrame(page, fg_color="gray17", corner_radius=10)
        card.pack(fill="x", padx=16, pady=4)

        def _row(label: str, value: str) -> None:
            r = ctk.CTkFrame(card, fg_color="transparent")
            r.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(r, text=label, anchor="w", text_color="gray60", width=140).pack(side="left")
            ctk.CTkLabel(r, text=value, anchor="w").pack(side="left")

        st = load_state()
        _row("Хувилбар", f"v{__version__}")
        _row("Холболт", "холбогдсон" if st.is_paired else "холбогдоогүй")
        _row("Edge AI", self._edge_status_text())
        btns = ctk.CTkFrame(page, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=10)
        ctk.CTkButton(btns, text="🔗 Холболт", width=120, command=self.open_pairing).pack(side="left")
        ctk.CTkButton(
            btns, text="⬆ Шинэчлэл", width=120, fg_color="transparent",
            border_width=1, command=self.open_update,
        ).pack(side="left", padx=8)
        return page

    # === Camera list rendering ===

    def refresh_cameras(self) -> None:
        # Render the local list immediately (fast), then reconcile with the
        # backend in the background — so a camera deleted on the web disappears
        # here too, and the desktop list always matches the web.
        self._render_camera_list(load_state().cameras)
        self._reconcile_in_bg()

    def _render_camera_list(self, cameras: list[CameraRecord]) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._push_labels = {}  # rows (and their labels) are being recreated
        if not cameras:
            ctk.CTkLabel(
                self.list_frame,
                text="Камер бүртгэгдээгүй байна.\n\n"
                "'Камер хайх' дарж автоматаар олох, эсвэл 'Камер нэмэх' дарж гараар нэмнэ үү.",
                font=ctk.CTkFont(size=13),
                text_color="gray60",
                justify="center",
            ).pack(pady=60)
            self.set_status("0 камер")
            return

        for cam in cameras:
            self._render_camera_row(cam)
        self.set_status(f"{len(cameras)} камер бүртгэлтэй")
        self._refresh_streaming()

    def _reconcile_in_bg(self) -> None:
        """Sync the camera list with the backend off the UI thread; re-render
        only if it changed (web-side delete / a camera registered elsewhere)."""
        if not load_state().is_paired:
            return

        def work() -> None:
            cameras, changed = svc.reconcile_with_backend()
            if not changed:
                return
            try:
                if self.winfo_exists():
                    self.after(0, lambda: self._render_camera_list(cameras))
            except tk.TclError:
                pass

        threading.Thread(target=work, name="camera-reconcile", daemon=True).start()

    def _refresh_streaming(self) -> None:
        """Reconcile cloud stream-push relays with the current camera list.

        Runs on a background thread (network + ffmpeg supervision). No-op when
        unpaired or when the backend reports pull/on-LAN topology."""
        if not load_state().is_paired:
            return
        threading.Thread(
            target=get_stream_controller().refresh,
            name="stream-refresh",
            daemon=True,
        ).start()

    def _schedule(self, key: str, delay_ms: int, fn: Callable[[], None]) -> None:
        """Record a self-rescheduling `after` so quit_app() can cancel the pending
        tick. No-op once closing — keeps a final tick from re-arming itself."""
        if self._closing:
            return
        self._after_ids[key] = self.after(delay_ms, fn)

    def _tick_push_status(self) -> None:
        """Refresh the per-camera push indicators from the StreamPusher state."""
        if self._closing:
            return
        try:
            self._update_push_indicators()
        finally:
            self._schedule("push_status", 5000, self._tick_push_status)

    def _tick_periodic_refresh(self) -> None:
        """Periodically reconcile relays so backend stream-config changes
        (push toggled, creds rotated) propagate without a manual refresh."""
        if self._closing:
            return
        try:
            self._refresh_streaming()
            self._refresh_edge_status()
        finally:
            self._schedule("periodic_refresh", 30000, self._tick_periodic_refresh)

    def _refresh_edge_status(self) -> None:
        """Repaint the sidebar edge-AI badge so a live-view runtime failure (or a
        recovery) shows without restarting the app."""
        label = getattr(self, "_edge_status_label", None)
        if label is not None:
            with contextlib.suppress(Exception):
                label.configure(text=self._edge_status_text())

    def _tick_heartbeat(self) -> None:
        """Periodic heartbeat so the cloud keeps this computer marked online.
        _check_backend_async() POSTs /api/v1/agent/heartbeat (and refreshes the
        header label). Runs while minimized to tray — only quitting stops it."""
        if self._closing:
            return
        try:
            if load_state().is_paired:
                self._check_backend_async()
        finally:
            self._schedule("heartbeat", 30000, self._tick_heartbeat)

    def _update_push_indicators(self) -> None:
        ctrl = get_stream_controller()
        status_by_path = {s["path"]: s for s in ctrl.status()}
        for path, lbl in self._push_labels.items():
            try:
                if not ctrl.push_enabled:
                    lbl.configure(text="—", text_color="gray50")
                    continue
                st = status_by_path.get(path)
                if st is None:
                    lbl.configure(text="⏳ хүлээж", text_color="gray60")
                elif st.get("running"):
                    lbl.configure(text="🟢 дамжуулж", text_color="#4ADE80")
                else:
                    lbl.configure(text="🔴 тасарсан", text_color="#FF6B6B")
            except Exception:  # noqa: BLE001 — label may have been destroyed mid-refresh
                continue

    def _render_camera_row(self, cam: CameraRecord) -> None:
        row = ctk.CTkFrame(self.list_frame, fg_color="gray17", corner_radius=8)
        row.pack(fill="x", pady=3)
        self._configure_grid(row)

        res = f"{cam.resolution[0]}×{cam.resolution[1]}" if cam.resolution else "—"
        cells = [
            cam.name,
            cam.ip or "—",
            cam.mediamtx_path or "—",
            (cam.codec or "—").upper(),
            res,
        ]
        for i, text in enumerate(cells):
            ctk.CTkLabel(
                row,
                text=text,
                anchor="w",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=i, sticky="w", padx=6, pady=8)

        # Push status (cloud topology) — updated live by _tick_push_status.
        push_lbl = ctk.CTkLabel(
            row,
            text="—",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color="gray50",
        )
        push_lbl.grid(row=0, column=5, sticky="w", padx=6, pady=8)
        if cam.mediamtx_path:
            self._push_labels[cam.mediamtx_path] = push_lbl

        # Action buttons — packed into one cell so they group at the row's end.
        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=0, column=len(self._COLUMNS), sticky="e", padx=(6, 8), pady=6)

        ctk.CTkButton(
            actions,
            text="✎ Засах",
            width=70,
            height=26,
            fg_color="transparent",
            border_width=1,
            text_color=CHIPMO_ORANGE,
            border_color=CHIPMO_ORANGE,
            hover_color="gray25",
            command=lambda c=cam: self._edit_camera(c),
        ).pack(side="left", padx=2)

        # Manual repair: re-probe the camera and restart its relay. Recovers a
        # camera that was unplugged + came back (the auto-reconnect backoff can
        # be slow) or whose RTSP path drifted, without deleting + re-adding it.
        ctk.CTkButton(
            actions,
            text="↻ Холбох",
            width=78,
            height=26,
            fg_color="transparent",
            border_width=1,
            text_color=CHIPMO_ORANGE,
            border_color=CHIPMO_ORANGE,
            hover_color="gray25",
            command=lambda c=cam: self._reconnect_camera(c),
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            actions,
            text="🗑 Устгах",
            width=78,
            height=26,
            fg_color="transparent",
            border_width=1,
            text_color="#FF6B6B",
            border_color="#FF6B6B",
            hover_color="gray25",
            command=lambda c=cam: self._delete_camera(c),
        ).pack(side="left", padx=2)

    def _reconnect_camera(self, cam: CameraRecord) -> None:
        def work() -> dict[str, Any]:
            from sentry_agent_pc.discovery import rtsp_probe

            # 1. Is the stored URL still alive?
            alive = rtsp_probe.probe(cam.rtsp_url).ok
            changed = False
            if not alive:
                # 2. Stale path — re-resolve from the camera's IP + credentials.
                user, pwd = creds_from_rtsp(cam.rtsp_url)
                if user is not None:
                    try:
                        rs = svc.resolve_stream(cam.ip, user, pwd)
                    except Exception as e:  # noqa: BLE001 — resolve raises many types
                        log.info("reconnect.resolve_error", ip=cam.ip, error=str(e))
                        rs = None
                    if rs is not None and rs.rtsp_url:
                        state = load_state()
                        for c in state.cameras:
                            if c.uuid == cam.uuid:
                                c.rtsp_url = rs.rtsp_url
                                c.codec = rs.codec or c.codec
                                if rs.width and rs.height:
                                    c.resolution = (rs.width, rs.height)
                                changed = True
                        if changed:
                            save_state(state)
                            alive = True
            # 3. Restart the push relay (if any) from the latest state.
            get_stream_controller().refresh()
            return {"ok": True, "alive": alive, "changed": changed}

        def done(result: Any) -> None:
            # Refresh FIRST: refresh_cameras() → _render_camera_list() resets the
            # status to "{n} камер бүртгэлтэй", so the confirmation must be set
            # AFTER it or the user never sees it.
            self.refresh_cameras()
            if isinstance(result, dict) and not result.get("ok", True):
                self.set_status(f"⚠ Дахин холбож чадсангүй: {str(result.get('error', ''))[:60]}")
            elif result.get("changed"):
                self.set_status("Камерын шинэ урсгал олдож, дахин холбогдлоо ✓")
            elif result.get("alive"):
                self.set_status("Камер амьд байна — дахин холбогдлоо ✓")
            else:
                self.set_status(
                    "⚠ Камер хариу өгсөнгүй — IP/тэжээл/сүлжээгээ шалгаад дахин оролдоно уу"
                )

        self._run_bg(work, done, status="Дахин холбож байна…")

    def _delete_camera(self, cam: CameraRecord) -> None:
        dlg = ctk.CTkInputDialog(
            text=f"'{cam.name}' камерыг устгахдаа итгэлтэй байна уу?\n"
            "Баталгаажуулахын тулд 'устга' гэж бичнэ үү:",
            title="Камер устгах",
        )
        if (dlg.get_input() or "").strip().lower() != "устга":
            return

        def work() -> dict[str, Any]:
            # Backend delete FIRST — if it fails we keep the local record so the
            # list stays consistent with the server (no orphaned local rows).
            # Use the AGENT-scoped endpoint: the legacy /api/v1/cameras/{id}
            # needs a user session, so an agent token gets a 403 there.
            if cam.uuid:
                BackendClient().agent_delete_camera(cam.uuid)  # raises on real failure
            # Backend ok (or no uuid) → drop from local state
            from sentry_agent_pc.state import save_state

            state = load_state()
            state.cameras = [x for x in state.cameras if x.uuid != cam.uuid]
            save_state(state)
            return {"ok": True}

        def done(result: Any) -> None:
            # Refresh FIRST so the deleted row disappears, THEN set the
            # confirmation — refresh_cameras() resets the status text otherwise.
            self.refresh_cameras()
            if isinstance(result, dict) and not result.get("ok", True):
                self.set_status(f"⚠ Устгаж чадсангүй: {result.get('error', '')[:60]}")
            else:
                self.set_status("Камер устгагдлаа")

        self._run_bg(work, done, status="Устгаж байна…")

    # === Dialogs ===

    def open_scan(self) -> None:
        if not self._require_paired():
            return
        ScanDialog(self, on_done=self.refresh_cameras)

    def open_add(self) -> None:
        if not self._require_paired():
            return
        AddCameraDialog(self, on_done=self.refresh_cameras)

    def _edit_camera(self, cam: CameraRecord) -> None:
        """Open the edit dialog for a camera's connection / name.

        Backend-only cameras (surfaced by reconcile with no local rtsp_url —
        e.g. registered from another PC) carry no connection to edit here."""
        if not self._require_paired():
            return
        if not cam.rtsp_url:
            self.set_status(
                "⚠ Энэ камер өөр компьютероос бүртгэгдсэн — холболтыг эндээс засах боломжгүй."
            )
            return
        EditCameraDialog(self, cam, on_done=self.refresh_cameras)

    def open_pairing(self) -> None:
        PairingDialog(self, on_saved=self._on_pairing_saved)

    def open_update(self) -> None:
        """Manual update check from the header button (dialog runs the check)."""
        UpdateDialog(self, info=None)

    def open_live_view(self) -> None:
        """Open the OFFLINE LAN live view — decodes the cameras' RTSP directly in
        a window inside this app. Works with no internet, no MediaMTX, no login;
        the cloud sentry-ai pipeline + web /live (with AI overlay) run separately
        when online."""
        if not load_state().cameras:
            self.set_status("Камер бүртгэгдээгүй — эхлээд камер нэмнэ үү.")
            return
        from sentry_agent_pc.gui.local_view import open_local_view

        open_local_view(self)
        self.set_status("Шууд харах цонх нээгдэж байна (LAN-аас шууд)…")

    # === Window / tray lifecycle ===

    def _set_window_icon(self) -> None:
        """Set the title-bar / taskbar icon (best-effort; Windows .ico)."""
        try:
            ico = resources.icon_ico()
            if ico.exists():
                self.iconbitmap(default=str(ico))
        except Exception as e:  # noqa: BLE001 — icon is cosmetic
            log.debug("window.icon_failed", error=str(e))

    def hide_to_tray(self) -> None:
        """Hide the window to the system tray (used by the close button)."""
        if getattr(self, "_tray", None) is not None and self._tray.active:
            self.withdraw()
            self.set_status("Tray-д жижигрэв — taskbar-ийн tray icon-оос нээнэ")
        else:
            # No tray available → closing really exits.
            self.quit_app()

    def show_window(self) -> None:
        """Restore the window from the tray."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_app(self) -> None:
        """Fully exit: stop tray + stream relays, then destroy the window.

        Sets _closing FIRST and cancels every pending self-rescheduling `after`
        tick so none fire after destroy() and touch a dead widget (TclError)."""
        self._closing = True
        for after_id in self._after_ids.values():
            with contextlib.suppress(tk.TclError):
                self.after_cancel(after_id)
        self._after_ids.clear()
        try:
            get_stream_controller().stop()
        except Exception as e:  # noqa: BLE001
            log.debug("quit.stop_streams_failed", error=str(e))
        if getattr(self, "_tray", None) is not None:
            self._tray.stop()
        self.destroy()

    def _auto_check_update(self) -> None:
        """Background self-update check. With `auto_update` on (and a frozen build)
        a newer release is silently downloaded and applied — the app restarts into
        it with only a tray toast, no click. Otherwise it falls back to the update
        dialog (manual apply / dev "download manually" link). Re-arms itself to
        re-check every `update_check_interval_hours`."""
        if self._closing:
            return
        # Re-arm the periodic check first, so a failure anywhere below can't stop
        # future checks. Floor the interval so a misconfig can't hammer GitHub.
        interval_h = max(0.25, get_settings().update_check_interval_hours)
        self._schedule(
            "auto_check_update", int(interval_h * 3600 * 1000), self._auto_check_update
        )

        if self._update_in_progress:
            return  # a download/apply from a previous check is still underway

        def on_available(info: updater.UpdateInfo) -> None:
            if get_settings().auto_update and updater.is_frozen():
                self._update_in_progress = True
                self.set_status(f"Шинэ хувилбар v{info.version} — автоматаар суулгаж байна…")

                def _done(ok: bool) -> None:
                    # Success restarts the process; only a failed download returns
                    # here, so clear the flag and let the next check retry.
                    if not ok:
                        self._update_in_progress = False

                auto_update_in_background(self, info, on_done=_done)
            else:
                # Auto-update off OR dev (non-frozen) build → prompt via the dialog.
                self.set_status(f"Шинэ хувилбар бэлэн: v{info.version}")
                UpdateDialog(self, info=info)

        check_in_background(self, on_available)

    def _on_pairing_saved(self) -> None:
        self.set_status("Холболт шинэчлэгдсэн")
        self._check_backend_async()
        self.refresh_cameras()

    # === Backend status ===

    def _require_paired(self) -> bool:
        if not load_state().is_paired:
            self.set_status("⚠ Эхлээд дэлгүүртэйгээ холбоно уу ('🔗 Холболт')")
            self.open_pairing()
            return False
        return True

    def _check_backend_async(self) -> None:
        state = load_state()
        if not state.is_paired:
            self.backend_label.configure(
                text="Холбогдоогүй — '🔗 Холболт' дарна уу",
                text_color="#FBBF24",
            )
            return
        store = state.store_name or "дэлгүүр"

        def work() -> dict[str, Any]:
            try:
                BackendClient().heartbeat()
                return {"ok": True}
            except BackendError as e:
                return {"ok": False, "error": str(e)}

        def done(result: dict[str, Any]) -> None:
            # The heartbeat tick runs this on a thread; if the window was quit
            # while it was in flight, configuring backend_label raises TclError.
            if self._closing or not self.winfo_exists():
                return
            if result.get("ok"):
                self.backend_label.configure(
                    text=f"✅ {store}",
                    text_color="#4ADE80",
                )
            else:
                self.backend_label.configure(
                    text=f"⚠ {store} — холбогдсонгүй",
                    text_color="#FF6B6B",
                )

        self._run_bg(work, done)

    # === Threading helper ===

    def _run_bg(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        status: str | None = None,
    ) -> None:
        """Run `work` on a thread; call `on_done(result)` on the UI thread."""
        if status:
            self.set_status(status)

        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001
                log.exception("bg_task_failed")
                result = {"ok": False, "error": str(e)}
            # The window may have been quit while this ran on a daemon thread —
            # scheduling onto a destroyed widget raises TclError.
            if self._closing:
                return
            with contextlib.suppress(tk.TclError):
                self.after(0, lambda: on_done(result))

        threading.Thread(target=runner, daemon=True).start()

    def set_status(self, text: str) -> None:
        if self._closing:
            return
        with contextlib.suppress(tk.TclError):
            self.status_label.configure(text=text)


class PairingDialog(ctk.CTkToplevel):
    """Connect this PC to a store using a 6-digit code from the web app.

    The admin opens app.sentry.chipmo.mn → Дэлгүүр → 'Компьютер холбох',
    generates a code, and types it here. On success we store the returned
    agent JWT (+ store name) in the encrypted state file.
    """

    def __init__(self, master: AgentApp, on_saved: Callable[[], None]) -> None:
        super().__init__(master)
        self.on_saved = on_saved
        self.title("Дэлгүүртэй холбох")
        self.transient(master)
        self.grab_set()
        widgets.setup_dialog(self, 540, 460, min_width=480, min_height=420)

        state = load_state()
        cfg = read_config()

        # Bottom button bar FIRST so it's never clipped, then status above it.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=20, pady=16)
        ctk.CTkButton(
            btn_row,
            text="Хаах",
            fg_color="transparent",
            border_width=1,
            command=self._save_and_close,
        ).pack(side="right", padx=(8, 0))
        self.connect_btn = ctk.CTkButton(
            btn_row,
            text="Холбох",
            fg_color=CHIPMO_ORANGE,
            hover_color=BRAND_ORANGE_HOVER,
            command=self._pair,
        )
        self.connect_btn.pack(side="right")
        if state.is_paired:
            ctk.CTkButton(
                btn_row,
                text="Салгах",
                fg_color="transparent",
                border_width=1,
                text_color="#FF6B6B",
                border_color="#FF6B6B",
                command=self._unpair,
            ).pack(side="left")

        self.status_lbl = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
            wraplength=470,
            anchor="w",
        )
        self.status_lbl.pack(side="bottom", fill="x", padx=20, pady=(6, 0))

        # Scrollable body.
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True)

        ctk.CTkLabel(
            body,
            text="Дэлгүүртэй холбох",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(8, 2), padx=20, anchor="w")

        if state.is_paired:
            ctk.CTkLabel(
                body,
                text=f"✅ Одоо холбогдсон дэлгүүр: {state.store_name or '—'}",
                font=ctk.CTkFont(size=13),
                text_color="#4ADE80",
                anchor="w",
            ).pack(fill="x", padx=20, pady=(2, 8))

        ctk.CTkLabel(
            body,
            text="Веб апп → Дэлгүүр → 'Компьютер холбох' дарж 6 оронтой код аваад доор оруулна уу.",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
            anchor="w",
            wraplength=470,
            justify="left",
        ).pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkLabel(body, text="6 оронтой код:", anchor="w").pack(fill="x", padx=20)
        self.code_entry = ctk.CTkEntry(
            body,
            placeholder_text="123456",
            font=ctk.CTkFont(size=22, weight="bold"),
            justify="center",
        )
        self.code_entry.pack(fill="x", padx=20, pady=(2, 12))

        # Backend/Web URLs are for power users on custom deployments only. Hide
        # them behind an "Advanced" toggle so the normal flow is just the code.
        self._adv_open = False
        self._adv_btn = ctk.CTkButton(
            body,
            text="⚙ Нэмэлт тохиргоо (Backend / Веб хаяг)  ▾",
            fg_color="transparent",
            border_width=1,
            text_color="gray60",
            anchor="w",
            command=self._toggle_advanced,
        )
        self._adv_btn.pack(fill="x", padx=20, pady=(10, 0))

        self._adv = ctk.CTkFrame(body, fg_color="transparent")  # packed on toggle
        ctk.CTkLabel(
            self._adv,
            text="Backend URL (default-ыг хэвээр үлдээж болно):",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
        ).pack(fill="x", padx=20, pady=(8, 0))
        self.url_entry = ctk.CTkEntry(self._adv, placeholder_text=DEFAULT_BACKEND_URL)
        self.url_entry.pack(fill="x", padx=20, pady=(2, 8))
        self.url_entry.insert(0, cfg.get("BACKEND_URL") or DEFAULT_BACKEND_URL)
        ctk.CTkLabel(
            self._adv,
            text="Веб хаяг (Шууд харах цонхонд ачаална):",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
        ).pack(fill="x", padx=20)
        self.frontend_entry = ctk.CTkEntry(self._adv, placeholder_text=DEFAULT_FRONTEND_URL)
        self.frontend_entry.pack(fill="x", padx=20, pady=(2, 4))
        self.frontend_entry.insert(0, cfg.get("FRONTEND_URL") or DEFAULT_FRONTEND_URL)

    def _toggle_advanced(self) -> None:
        self._adv_open = not self._adv_open
        arrow = "▴" if self._adv_open else "▾"
        self._adv_btn.configure(text=f"⚙ Нэмэлт тохиргоо (Backend / Веб хаяг)  {arrow}")
        if self._adv_open:
            self._adv.pack(fill="x", pady=(4, 0))
        else:
            self._adv.pack_forget()

    def _save_and_close(self) -> None:
        """Persist edited Backend/Web URLs (no pairing needed), then close.

        Lets an already-paired user change the live-view web address without
        re-entering a pairing code."""
        url = self.url_entry.get().strip() or DEFAULT_BACKEND_URL
        frontend = self.frontend_entry.get().strip() or DEFAULT_FRONTEND_URL
        write_config(url, frontend)
        self.destroy()

    def _pair(self) -> None:
        code = self.code_entry.get().strip()
        url = self.url_entry.get().strip() or DEFAULT_BACKEND_URL
        frontend = self.frontend_entry.get().strip() or DEFAULT_FRONTEND_URL
        if not code.isdigit() or len(code) != 6:
            self.status_lbl.configure(
                text="Код 6 оронтой тоо байх ёстой.",
                text_color="#FF6B6B",
            )
            return
        self.connect_btn.configure(state="disabled")
        self.status_lbl.configure(text="Холбож байна…", text_color="gray60")
        write_config(url, frontend)

        def runner() -> None:
            try:
                result = BackendClient(base_url=url).pair(code, name=platform.node())
                state = load_state()
                state.agent_jwt = result["agent_token"]
                state.paired_org_id = result.get("organization_id")
                state.default_store_id = result.get("store_id")
                state.store_name = result.get("store_name")
                save_state(state)
                out: dict[str, Any] = {"ok": True, "store": result.get("store_name")}
            except (BackendError, KeyError) as e:
                out = {"ok": False, "error": str(e)}
            self.after(0, lambda: self._pair_done(out))

        threading.Thread(target=runner, daemon=True).start()

    def _pair_done(self, result: dict[str, Any]) -> None:
        self.connect_btn.configure(state="normal")
        if result.get("ok"):
            self.status_lbl.configure(
                text=f"✅ '{result.get('store')}' дэлгүүртэй холбогдлоо!",
                text_color="#4ADE80",
            )
            self.on_saved()
            self.after(1200, self.destroy)
        else:
            self.status_lbl.configure(
                text=f"❌ {result.get('error', 'алдаа')[:120]}",
                text_color="#FF6B6B",
            )

    def _unpair(self) -> None:
        state = load_state()
        state.agent_jwt = None
        state.paired_org_id = None
        state.store_name = None
        save_state(state)
        self.on_saved()
        self.destroy()


# Stable per-app taskbar identity. Without an explicit AppUserModelID, Windows
# derives one from the host process and shows a BLANK/generic taskbar icon for a
# Tk window even though iconbitmap() sets the title-bar icon. Set once, before
# any window is created, so Windows uses the window icon on the taskbar and
# groups our windows (main + live-view child) under one button.
_APP_USER_MODEL_ID = "Chipmo.Sentry.Agent"


def set_app_user_model_id() -> None:
    """Bind a stable Windows taskbar AppUserModelID (no-op off Windows)."""
    import sys

    if not sys.platform.startswith("win"):
        return
    import ctypes

    with contextlib.suppress(Exception):  # cosmetic — never block startup
        # getattr avoids a platform-dependent mypy ignore: ctypes.windll only
        # exists on Windows, but this branch is only reached there.
        windll = getattr(ctypes, "windll")  # noqa: B009
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)


def run(minimized: bool = False) -> None:
    """GUI entry point. `minimized=True` (auto-start) launches hidden in the tray."""
    set_app_user_model_id()  # before the first Tk window → taskbar icon binds
    app = AgentApp()
    if minimized:
        app.after(0, app.hide_to_tray)
    app.mainloop()
