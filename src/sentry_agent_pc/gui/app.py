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
from sentry_agent_pc.gui.floor_plan import FloorPlanPage
from sentry_agent_pc.gui.scan_dialog import ScanDialog
from sentry_agent_pc.gui.tray import TrayController
from sentry_agent_pc.gui.update_dialog import (
    UpdateDialog,
    auto_update_in_background,
    check_in_background,
)
from sentry_agent_pc.gui.widgets import BRAND_ORANGE, BRAND_ORANGE_HOVER
from sentry_agent_pc.gui.zone_editor import ZoneEditorDialog
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

# The agent-pc edge behaviour catalog — every signal the Stage-1 gate scores,
# with the EdgeConfig weight key that controls it (tuned per store from
# superadmin's «Edge тохиргоо»). Drives BOTH the «Сэжигтэй» score breakdown and
# the «Зан үйл» menu table. `weight_key` is None for non-scored signals.
_EDGE_BEHAVIORS: tuple[dict[str, str], ...] = (
    {
        "key": "item_pickup",
        "label": "Эд зүйл барих",
        "desc": "Гар бараа дээр ойртож «барьсан» гэж тооцогдох",
        "weight_key": "w_holding",
    },
    {
        "key": "wrist_to_torso",
        "label": "Гар бие рүү",
        "desc": "Гар бэлхүүс/хармаан руу татагдсан (нуух хөдөлгөөн)",
        "weight_key": "w_wrist_torso",
    },
    {
        "key": "conceal",
        "label": "Эд зүйл нуух",
        "desc": "Бараа барьсан гар бие рүү ойртвол — нуух поз (хамгийн хүчтэй)",
        "weight_key": "w_conceal",
    },
    {
        "key": "repeated_shelf_visit",
        "label": "Тавиур давтан зочлох",
        "desc": "Нэг тавиурын бүс рүү олон удаа эргэж очих (зон шаардана)",
        "weight_key": "w_repeated_shelf",
    },
    {
        "key": "exit_after_concealment",
        "label": "Нуусны дараа гарц руу",
        "desc": "Нуусан хүн гарцын бүс рүү орох — хулгайн хүчтэй дохио (зон шаардана)",
        "weight_key": "w_exit_after_conceal",
    },
)
# Movement key → Mongolian label (the «Сэжигтэй» gallery + clip detail use this).
_EDGE_BEHAVIOR_LABELS = {b["key"]: b["label"] for b in _EDGE_BEHAVIORS}
# Movement key → its EdgeConfig weight field, so the clip detail can show
# "+5 × N удаа" from an aggregate-only (old) clip.
_BEHAVIOR_WEIGHT_KEY = {b["key"]: b["weight_key"] for b in _EDGE_BEHAVIORS if b.get("weight_key")}

# «Сэжигтэй» table column widths (px) — header + every row share these so the
# columns line up; the «Зан үйл» column takes the slack (expand).
_CLIP_COL_CAM = 150
_CLIP_COL_WHEN = 150
_CLIP_COL_RISK = 72
_CLIP_COL_DUR = 64
_CLIP_ROW_BG = "gray16"
_CLIP_ROW_HOVER = "gray25"


def _bind_row_click(widget: Any, handler: Callable[[], None]) -> None:
    """Make a whole row (frame + every child label) clickable + hand-cursor, so a
    click anywhere on the row opens the detail — Tk doesn't bubble child clicks."""
    stack = [widget]
    while stack:
        w = stack.pop()
        with contextlib.suppress(Exception):
            w.bind("<Button-1>", lambda _e: handler())
            w.configure(cursor="hand2")
        with contextlib.suppress(Exception):
            stack.extend(w.winfo_children())


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
        self._plan_page = FloorPlanPage(self._content, get_cameras=lambda: load_state().cameras)
        self._pages["plan"] = self._plan_page
        self._pages["alerts"] = self._build_alerts_page(self._content)
        self._pages["behaviors"] = self._build_behaviors_page(self._content)
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
        ("plan", "🗺  Plan зураг", "page"),
        ("live", "📺  Шууд харах", "action"),
        ("alerts", "⚠  Сэжигтэй", "page"),
        ("behaviors", "🎯  Зан үйл", "page"),
        ("settings", "⚙  Тохиргоо", "page"),
    )

    def _build_sidebar(self, parent: ctk.CTkBaseClass) -> None:
        side = ctk.CTkFrame(parent, width=170, corner_radius=0, fg_color="gray17")
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        for key, label, kind in self._NAV:
            cmd = self.open_live_view if kind == "action" else self._page_cmd(key)
            btn = ctk.CTkButton(
                side,
                text=label,
                anchor="w",
                height=40,
                corner_radius=8,
                fg_color="transparent",
                text_color="gray85",
                hover_color="gray25",
                font=ctk.CTkFont(size=14),
                command=cmd,
            )
            btn.pack(fill="x", padx=10, pady=(10 if key == "cameras" else 2, 2))
            if kind == "page":
                self._nav_buttons[key] = btn
        # Edge-AI status pinned to the bottom so "is the AI running" is always visible.
        self._edge_status_label = ctk.CTkLabel(
            side,
            text=self._edge_status_text(),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
            wraplength=148,
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
        elif name == "behaviors":
            self._refresh_behaviors()
        elif name == "plan":
            self._plan_page.on_show()

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
        ctk.CTkLabel(bar, text="Сэжигтэй бичлэгүүд", font=ctk.CTkFont(size=16, weight="bold")).pack(
            side="left"
        )
        ctk.CTkButton(
            bar,
            text="↻ Сэргээх",
            width=100,
            height=32,
            fg_color="transparent",
            border_width=1,
            command=self._refresh_alerts,
        ).pack(side="right")
        ctk.CTkLabel(
            page,
            text="Мөр дээр дарж тухайн тохиолдлын дэлгэрэнгүй (зан үйл·оноо·цаг) хараарай.",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="gray55",
        ).pack(anchor="w", padx=16, pady=(0, 6))
        # Column header — shares the row cell widths so columns line up.
        hdr = ctk.CTkFrame(page, fg_color="gray22", corner_radius=6)
        hdr.pack(fill="x", padx=16, pady=(0, 2))
        for text, w, anchor in (
            ("Камер", _CLIP_COL_CAM, "w"),
            ("Огноо · цаг", _CLIP_COL_WHEN, "w"),
            ("Эрсдэл", _CLIP_COL_RISK, "e"),
            ("Зан үйл", 0, "w"),
            ("Хугацаа", _CLIP_COL_DUR, "e"),
        ):
            lbl = ctk.CTkLabel(
                hdr,
                text=text,
                anchor=anchor,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="gray70",
                **({"width": w} if w else {}),
            )
            lbl.pack(side="left", fill=("x" if not w else None), expand=(not w), padx=8, pady=5)
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
                text_color="gray60",
                justify="center",
            ).pack(pady=50)
            return
        for clip in sorted(clips, key=lambda r: r.created_at, reverse=True):
            self._render_clip_row(clip)

    def _render_clip_row(self, clip: ClipRecord) -> None:
        """One clip as a clickable table row — whole row opens the detail."""
        import datetime

        row = ctk.CTkFrame(self._alerts_frame, fg_color=_CLIP_ROW_BG, corner_radius=8)
        row.pack(fill="x", pady=2)
        when = datetime.datetime.fromtimestamp(clip.started_at).strftime("%Y-%m-%d %H:%M:%S")
        color = (
            "#FF6B6B"
            if clip.risk_pct >= 70
            else (CHIPMO_ORANGE if clip.risk_pct >= 40 else "gray70")
        )
        labels = [_EDGE_BEHAVIOR_LABELS.get(b, b) for b in clip.behaviors]
        beh = " · ".join(labels) or "—"

        ctk.CTkLabel(
            row,
            text=clip.camera_id,
            width=_CLIP_COL_CAM,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=8, pady=9)
        ctk.CTkLabel(
            row,
            text=when,
            width=_CLIP_COL_WHEN,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="gray75",
        ).pack(side="left", padx=8)
        ctk.CTkLabel(
            row,
            text=f"{clip.risk_pct:.0f}%",
            width=_CLIP_COL_RISK,
            anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=color,
        ).pack(side="left", padx=8)
        ctk.CTkLabel(
            row,
            text=beh,
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="gray80",
        ).pack(side="left", fill="x", expand=True, padx=8)
        ctk.CTkLabel(
            row,
            text=f"{clip.duration:.0f}с",
            width=_CLIP_COL_DUR,
            anchor="e",
            font=ctk.CTkFont(size=11),
            text_color="gray65",
        ).pack(side="left", padx=8)
        ctk.CTkLabel(
            row,
            text="›",
            width=18,
            anchor="e",
            font=ctk.CTkFont(size=16),
            text_color="gray55",
        ).pack(side="left", padx=(0, 8))

        _bind_row_click(row, lambda: self._open_clip_detail(clip))
        # Hover highlight for the whole row.
        row.bind("<Enter>", lambda _e: row.configure(fg_color=_CLIP_ROW_HOVER))
        row.bind("<Leave>", lambda _e: row.configure(fg_color=_CLIP_ROW_BG))

    def _open_clip_detail(self, clip: ClipRecord) -> None:
        """Modal: the suspicious episode's full per-fire timeline — one row per
        banking with wall-clock time, behaviour, +оноо and the resulting risk%."""
        import datetime

        from sentry_agent_pc.gui import widgets

        win = ctk.CTkToplevel(self)
        win.title("Сэжигтэй тохиолдол — дэлгэрэнгүй")
        win.transient(self)
        widgets.setup_dialog(win, 560, 560, min_width=440, min_height=360)
        with contextlib.suppress(Exception):
            win.grab_set()

        started = datetime.datetime.fromtimestamp(clip.started_at)
        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(
            head,
            text=f"{clip.camera_id}",
            anchor="w",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text=(
                f"{started.strftime('%Y-%m-%d %H:%M:%S')}  ·  "
                f"Дээд эрсдэл {clip.risk_pct:.0f}%  ·  {clip.duration:.0f}с  ·  "
                "⚙ Edge behaviour engine"
            ),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="gray65",
        ).pack(anchor="w")

        from sentry_agent_pc.edge.config import EdgeConfig

        # Footer pinned to the bottom FIRST (setup_dialog convention) so it never
        # gets clipped by the expanding event list.
        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=16, pady=(2, 12))

        # Column header — meaning depends on the view (per-fire timeline vs the
        # aggregated count for an older clip), so it's built per branch below.
        hdr = ctk.CTkFrame(win, fg_color="gray20", corner_radius=6)
        hdr.pack(fill="x", padx=16, pady=(8, 0))

        def _hdr(cols: tuple[tuple[str, int, str], ...]) -> None:
            for text, w, anchor in cols:
                ctk.CTkLabel(
                    hdr, text=text, width=w, anchor=anchor,
                    font=ctk.CTkFont(size=11, weight="bold"), text_color="gray80",
                ).pack(side="left", padx=6, pady=4)

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(2, 4))
        total = 0.0
        if clip.events:
            # Per-fire timeline: one row per banking, time RELATIVE to the episode
            # start (so the cadence — "+5 every 0.2с" — is obvious at a glance).
            _hdr((("Хугацаа", 96, "w"), ("Зан үйл", 210, "w"), ("Оноо", 64, "e"), ("Эрсдэл", 64, "e")))
            for ev in clip.events:
                key = str(ev.get("key", ""))
                label = _EDGE_BEHAVIOR_LABELS.get(key, key)
                amount = float(ev.get("amount", 0) or 0)
                risk = float(ev.get("risk", 0) or 0)
                offset = float(ev.get("offset_sec", 0) or 0)
                total += amount
                tcol = "#FF6B6B" if risk >= 70 else (CHIPMO_ORANGE if risk >= 40 else "gray75")
                line = ctk.CTkFrame(body, fg_color="transparent")
                line.pack(fill="x", pady=1)
                ctk.CTkLabel(line, text=f"+{offset:.1f}с", width=96, anchor="w",
                            font=ctk.CTkFont(size=11), text_color="gray70").pack(side="left", padx=6)
                ctk.CTkLabel(line, text=label, width=210, anchor="w",
                            font=ctk.CTkFont(size=11)).pack(side="left", padx=6)
                ctk.CTkLabel(line, text=f"+{amount:.0f}", width=64, anchor="e",
                            font=ctk.CTkFont(size=11, weight="bold"),
                            text_color="#7CD992").pack(side="left", padx=6)
                ctk.CTkLabel(line, text=f"{risk:.0f}%", width=64, anchor="e",
                            font=ctk.CTkFont(size=11), text_color=tcol).pack(side="left", padx=6)
            span = max(0.0, clip.duration)
            note = (
                f"Нийт {len(clip.events)} дохио · {span:.0f}с дотор · цугларсан +{total:.0f} оноо. "
                "«Хугацаа» = эхэлснээс хойшхи секунд; дохио хооронд эрсдэл буурдаг (decay)."
            )
        elif clip.behavior_detail:
            # Older clip (no per-fire log): can't show the timeline, but we CAN say
            # HOW MANY times each behaviour fired — count ≈ total ÷ the per-hit
            # weight — which is the "+5 vs +165" the operator was confused by.
            defaults = EdgeConfig()
            _hdr((("Зан үйл", 240, "w"), ("Нэг удаад", 80, "e"), ("Удаа", 70, "e"), ("Нийт", 70, "e")))
            for d in clip.behavior_detail:
                key = str(d.get("key", ""))
                label = _EDGE_BEHAVIOR_LABELS.get(key, key)
                score = float(d.get("score", 0) or 0)
                total += score
                wkey = _BEHAVIOR_WEIGHT_KEY.get(key, "")
                unit = float(getattr(defaults, wkey, 0.0)) if wkey else 0.0
                count = round(score / unit) if unit > 0 else 0
                line = ctk.CTkFrame(body, fg_color="transparent")
                line.pack(fill="x", pady=1)
                ctk.CTkLabel(line, text=label, width=240, anchor="w",
                            font=ctk.CTkFont(size=11)).pack(side="left", padx=6)
                ctk.CTkLabel(line, text=(f"+{unit:.0f}" if unit else "—"), width=80, anchor="e",
                            font=ctk.CTkFont(size=11), text_color="gray70").pack(side="left", padx=6)
                ctk.CTkLabel(line, text=(f"~{count}" if count else "—"), width=70, anchor="e",
                            font=ctk.CTkFont(size=11), text_color="gray75").pack(side="left", padx=6)
                ctk.CTkLabel(line, text=f"+{score:.0f}", width=70, anchor="e",
                            font=ctk.CTkFont(size=11, weight="bold"),
                            text_color="#7CD992").pack(side="left", padx=6)
            note = (
                f"Цугларсан +{total:.0f} оноо. «Удаа» = хэдэн удаа давтагдсаны ОЙРОЛЦООГ "
                "(нийт ÷ нэг удаагийн оноо). Хугацааны нарийн задаргаа шинэ бичлэгүүдэд гарна."
            )
        else:
            _hdr((("Зан үйл", 240, "w"), ("Оноо", 70, "e")))
            ctk.CTkLabel(body, text="Онооны задаргаа алга.", text_color="gray60").pack(pady=20)
            note = "Энэ бичлэгт зан үйлийн задаргаа бүртгэгдээгүй."

        ctk.CTkLabel(
            foot,
            text=note,
            anchor="w",
            font=ctk.CTkFont(size=10),
            text_color="gray60",
            wraplength=380,
        ).pack(side="left")
        ctk.CTkButton(
            foot,
            text="▶ Видео",
            width=80,
            height=30,
            command=lambda p=clip.path: self._open_clip(p),
        ).pack(side="right")

    def _open_clip(self, path: str) -> None:
        opener = getattr(os, "startfile", None)  # Windows default player (None elsewhere)
        if opener is not None:
            with contextlib.suppress(Exception):
                opener(path)
                self.set_status(f"Бичлэг нээж байна: {path}")
                return
        self.set_status(f"Бичлэг: {path}")

    # === «Зан үйл» page — the edge behaviour catalog + per-store weights ===

    def _build_behaviors_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        """The behaviours the agent-pc Stage-1 gate scores, with the weight each
        adds (tuned per store from superadmin's «Edge тохиргоо», fetched live)."""
        page = ctk.CTkFrame(parent, fg_color="transparent")
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            bar, text="Зан үйлийн жагсаалт", font=ctk.CTkFont(size=16, weight="bold")
        ).pack(side="left")
        ctk.CTkButton(
            bar,
            text="↻ Сэргээх",
            width=100,
            height=32,
            fg_color="transparent",
            border_width=1,
            command=self._refresh_behaviors,
        ).pack(side="right")
        ctk.CTkLabel(
            page,
            text=(
                "Энэ компьютер дээрх AI хөдөлгүүр доорх зан үйлүүдийг хардаг. «Жин» нь "
                "тухайн хөдөлгөөн илрэхэд суспиц оноонд хэдэн оноо нэмэхийг заана — "
                "superadmin-аас дэлгүүр тус бүрээр тааруулна."
            ),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
            wraplength=640,
        ).pack(anchor="w", padx=16, pady=(0, 8))
        self._behaviors_version = ctk.CTkLabel(
            page, text="", anchor="w", font=ctk.CTkFont(size=10), text_color="gray55"
        )
        self._behaviors_version.pack(anchor="w", padx=16)
        self._behaviors_frame = ctk.CTkScrollableFrame(page, fg_color="transparent")
        self._behaviors_frame.pack(fill="both", expand=True, padx=16, pady=(4, 10))
        self._behavior_weight_labels: dict[str, ctk.CTkLabel] = {}
        return page

    def _refresh_behaviors(self) -> None:
        from sentry_agent_pc.edge.config import EdgeConfig

        for w in self._behaviors_frame.winfo_children():
            w.destroy()
        self._behavior_weight_labels = {}
        # Header
        hdr = ctk.CTkFrame(self._behaviors_frame, fg_color="gray20", corner_radius=6)
        hdr.pack(fill="x", pady=(0, 4))
        for text, width, anchor in (("Зан үйл", 190, "w"), ("Тайлбар", 360, "w"), ("Жин", 70, "e")):
            ctk.CTkLabel(
                hdr,
                text=text,
                width=width,
                anchor=anchor,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="gray80",
            ).pack(side="left", padx=8, pady=5)
        # Rows — seed weights from local defaults; the live fetch overrides below.
        defaults = EdgeConfig()
        for b in _EDGE_BEHAVIORS:
            row = ctk.CTkFrame(self._behaviors_frame, fg_color="gray17", corner_radius=8)
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row,
                text=b["label"],
                width=190,
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(side="left", padx=8, pady=8)
            ctk.CTkLabel(
                row,
                text=b["desc"],
                width=360,
                anchor="w",
                justify="left",
                font=ctk.CTkFont(size=11),
                text_color="gray70",
                wraplength=350,
            ).pack(side="left", padx=8, pady=8)
            wkey = b.get("weight_key") or ""
            seed = getattr(defaults, wkey, None) if wkey else None
            lbl = ctk.CTkLabel(
                row,
                text=(f"+{float(seed):.0f}" if seed is not None else "—"),
                width=70,
                anchor="e",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=CHIPMO_ORANGE,
            )
            lbl.pack(side="left", padx=8, pady=8)
            if wkey:
                self._behavior_weight_labels[wkey] = lbl
        self._behaviors_version.configure(text="Анхдагч утга харуулж байна…")
        # Fetch the live per-store weights off the UI thread.
        threading.Thread(target=self._fetch_behavior_weights, daemon=True).start()

    def _fetch_behavior_weights(self) -> None:
        try:
            cfg = BackendClient().agent_edge_config()
        except Exception as e:  # noqa: BLE001 — offline → keep the seeded defaults
            self._post_behavior_weights(None, str(e)[:80])
            return
        self._post_behavior_weights(cfg, None)

    def _post_behavior_weights(self, cfg: dict[str, Any] | None, err: str | None) -> None:
        """Apply fetched weights on the UI thread (guarded against teardown)."""
        if self._closing:
            return
        with contextlib.suppress(Exception):
            self.after(0, lambda: self._apply_behavior_weights(cfg, err))

    def _apply_behavior_weights(self, cfg: dict[str, Any] | None, err: str | None) -> None:
        if self._closing or not self._behavior_weight_labels:
            return
        if cfg is None:
            self._behaviors_version.configure(
                text=f"Серверээс татаж чадсангүй ({err}). Анхдагч утга харагдаж байна."
            )
            return
        for wkey, lbl in self._behavior_weight_labels.items():
            val = cfg.get(wkey)
            with contextlib.suppress(Exception):
                if val is not None:
                    lbl.configure(text=f"+{float(val):.0f}")
        ver = cfg.get("version")
        self._behaviors_version.configure(
            text=f"Серверээс татсан жин (тохиргоо v{ver}) · superadmin-аас тааруулна."
        )

    def _build_settings_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(page, text="Тохиргоо", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=16, pady=(14, 8)
        )
        card = ctk.CTkFrame(page, fg_color="gray17", corner_radius=10)
        card.pack(fill="x", padx=16, pady=4)

        def _row(label: str, value: str) -> None:
            r = ctk.CTkFrame(card, fg_color="transparent")
            r.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(r, text=label, anchor="w", text_color="gray60", width=140).pack(
                side="left"
            )
            ctk.CTkLabel(r, text=value, anchor="w").pack(side="left")

        st = load_state()
        _row("Хувилбар", f"v{__version__}")
        _row("Холболт", "холбогдсон" if st.is_paired else "холбогдоогүй")
        _row("Edge AI", self._edge_status_text())
        btns = ctk.CTkFrame(page, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=10)
        ctk.CTkButton(btns, text="🔗 Холболт", width=120, command=self.open_pairing).pack(
            side="left"
        )
        ctk.CTkButton(
            btns,
            text="⬆ Шинэчлэл",
            width=120,
            fg_color="transparent",
            border_width=1,
            command=self.open_update,
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

        # Draw detection zones (exit/shelf/…) on a freeze-frame (docs/29 P1a).
        ctk.CTkButton(
            actions,
            text="▦ Зон",
            width=64,
            height=26,
            fg_color="transparent",
            border_width=1,
            text_color=CHIPMO_ORANGE,
            border_color=CHIPMO_ORANGE,
            hover_color="gray25",
            command=lambda c=cam: self._edit_zones(c),
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
                        from sentry_agent_pc.state import mutate_state

                        hit = {"matched": False}

                        def _apply(s: Any, rs: Any = rs, hit: Any = hit) -> None:
                            for c in s.cameras:
                                if c.matches(cam):
                                    c.rtsp_url = rs.rtsp_url
                                    c.codec = rs.codec or c.codec
                                    if rs.width and rs.height:
                                        c.resolution = (rs.width, rs.height)
                                    hit["matched"] = True

                        mutate_state(_apply)
                        changed = hit["matched"]
                        alive = changed
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
            # Backend ok (or no uuid) → drop from local state. mutate_state keeps
            # the read-modify-write atomic vs the heartbeat/reconcile writers, and
            # cam.matches() avoids wiping every uuid=None camera.
            from sentry_agent_pc.state import mutate_state

            mutate_state(
                lambda s: setattr(s, "cameras", [x for x in s.cameras if not x.matches(cam)])
            )
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

    def _edit_zones(self, cam: CameraRecord) -> None:
        """Open the zone editor (docs/29 P1a) — draw exit/shelf polygons on a
        freeze-frame. Needs a paired agent (to PATCH) + a local stream to grab a
        frame; a camera registered on another PC has no rtsp_url here."""
        if not self._require_paired():
            return
        if not cam.uuid:
            self.set_status("⚠ Энэ камер бүртгэгдээгүй тул зон хадгалах боломжгүй.")
            return
        if not cam.rtsp_url:
            self.set_status("⚠ Энэ камер өөр компьютероос бүртгэгдсэн — зураг авах стрим алга.")
            return
        ZoneEditorDialog(self, cam, on_done=self.refresh_cameras)

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
        if self._closing:
            return  # re-entrant (tray "Гарах" + window-close race) → no double destroy()
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
        self._schedule("auto_check_update", int(interval_h * 3600 * 1000), self._auto_check_update)

        if self._update_in_progress:
            return  # a download/apply from a previous check is still underway

        def on_available(info: updater.UpdateInfo) -> None:
            # This fires from a background thread via after(0); the user may have
            # quit in the meantime — don't build a dialog on a dead root.
            if self._closing or not self.winfo_exists():
                return
            if get_settings().auto_update and updater.is_frozen():
                self._update_in_progress = True
                self.set_status(f"Шинэ хувилбар v{info.version} — автоматаар суулгаж байна…")

                def _done(ok: bool) -> None:
                    # Success restarts the process; only a failed download returns
                    # here, so clear the flag and let the next check retry.
                    if not ok:
                        self._update_in_progress = False

                # If the launch itself throws (before its worker thread starts),
                # clear the flag so a stuck True can't freeze all future updates.
                try:
                    auto_update_in_background(self, info, on_done=_done)
                except Exception:  # noqa: BLE001
                    self._update_in_progress = False
                    raise
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
            # The dialog can be closed mid-request (pairing hits the network and
            # can take seconds) — guard against after() on a destroyed Toplevel.
            with contextlib.suppress(tk.TclError):
                if self.winfo_exists():
                    self.after(0, lambda: self._pair_done(out))

        threading.Thread(target=runner, daemon=True).start()

    def _pair_done(self, result: dict[str, Any]) -> None:
        if not self.winfo_exists():
            return
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
        state.clear_pairing()  # nulls jwt + org + store_id + store_name together
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
