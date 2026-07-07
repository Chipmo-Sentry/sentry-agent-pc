"""Main desktop window — camera list + Scan/Add + Settings.

CustomTkinter native window. Long-running ops (ONVIF scan, RTSP probe,
backend calls) run on background threads and post results back to the UI
thread via `self.after(...)` to avoid freezing the window.
"""

from __future__ import annotations

import contextlib
import math
import os
import platform
import threading
import tkinter as tk
import urllib.parse
from collections.abc import Callable
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw

from sentry_agent_pc import __version__, resources, updater
from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.config_file import (
    DEFAULT_BACKEND_URL,
    DEFAULT_FRONTEND_URL,
    read_config,
    write_config,
)
from sentry_agent_pc.edge.controller import get_edge_controller
from sentry_agent_pc.edge.recorder import ClipRecord, ClipStore
from sentry_agent_pc.gui import widgets
from sentry_agent_pc.gui.add_dialog import AddCameraDialog
from sentry_agent_pc.gui.edit_dialog import EditCameraDialog
from sentry_agent_pc.gui.floor_plan_web import open_floor_plan
from sentry_agent_pc.gui.scan_dialog import ScanDialog
from sentry_agent_pc.gui.tray import TrayController
from sentry_agent_pc.gui.update_dialog import (
    UpdateDialog,
    auto_update_in_background,
    check_in_background,
)
from sentry_agent_pc.gui.widgets import (
    BRAND_PRIMARY,
    BRAND_PRIMARY_HOVER,
    UI_BG,
    UI_BORDER,
    UI_DANGER,
    UI_FG,
    UI_LINE_SOFT,
    UI_MUTED,
    UI_MUTED_FG,
    UI_MUTED_HOVER,
    UI_SUCCESS,
    UI_SURFACE,
    UI_SURFACE_2,
    Panel,
    StatusPill,
    dark_menu,
)
from sentry_agent_pc.gui.zone_editor import ZoneEditorDialog
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.settings import get_settings
from sentry_agent_pc.state import CameraRecord, load_state, save_state
from sentry_agent_pc.streaming.controller import get_stream_controller

log = get_logger("sentry_agent_pc.gui.app")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# The palette tokens live in widgets.py (single source of truth, mirrors
# sentry-ui-kit). The theme overrides below paint every default CustomTkinter
# widget with those tokens so the whole app — including dialogs — reads as the
# ui-kit dark theme (тас хар surface + royal-blue accent) without per-call_site
# colours.

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
        "interval_key": "interval_holding",
        "mindur_key": "mindur_holding",
    },
    {
        "key": "wrist_to_torso",
        "label": "Гар бие рүү",
        "desc": "Гар бэлхүүс/хармаан руу татагдсан (нуух хөдөлгөөн)",
        "weight_key": "w_wrist_torso",
        "interval_key": "interval_wrist_torso",
        "mindur_key": "mindur_wrist_torso",
    },
    {
        "key": "conceal",
        "label": "Эд зүйл нуух",
        "desc": "Бараа барьсан гар бие рүү ойртвол — нуух байрлал (хамгийн хүчтэй)",
        "weight_key": "w_conceal",
        "interval_key": "interval_conceal",
        "mindur_key": "mindur_conceal",
    },
    {
        "key": "repeated_shelf_visit",
        "label": "Тавиур давтан зочлох",
        "desc": "Нэг тавиурын бүс рүү олон удаа эргэж очих (зон шаардана)",
        "weight_key": "w_repeated_shelf",
        "interval_key": "interval_repeated_shelf",
        "mindur_key": "mindur_repeated_shelf",
    },
    {
        "key": "exit_after_concealment",
        "label": "Нуусны дараа гарц руу",
        "desc": "Нуусан хүн гарцын бүс рүү орох — хулгайн хүчтэй дохио (зон шаардана)",
        "weight_key": "w_exit_after_conceal",
        "interval_key": "interval_exit_after_conceal",
        "mindur_key": "mindur_exit_after_conceal",
    },
)
# EdgeConfig keys that belong to a behaviour row (weight + timing) — excluded from
# the «Бусад тохиргоо» table so they aren't shown twice.
_BEHAVIOR_FIELD_KEYS: frozenset[str] = frozenset(
    k for b in _EDGE_BEHAVIORS for k in (b["weight_key"], b["interval_key"], b["mindur_key"])
)
# Movement key → Mongolian label (the «Сэжигтэй» gallery + clip detail use this).
_EDGE_BEHAVIOR_LABELS = {b["key"]: b["label"] for b in _EDGE_BEHAVIORS}

# The FULL effective edge config shown (read-only) in the «Зан үйл» menu — what
# the YOLO + behaviour engine ACTUALLY runs with on this PC, grouped + labelled.
# (group, EdgeConfig key, Mongolian label, unit). Values come live from
# /agent/edge-config (superadmin sets them globally); tuning stays in superadmin.
_EDGE_CONFIG_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("Зан үйлийн оноо", "w_holding", "Эд зүйл барих", "оноо"),
    ("Зан үйлийн оноо", "w_conceal", "Эд зүйл нуух", "оноо"),
    ("Зан үйлийн оноо", "w_wrist_torso", "Гар бие рүү", "оноо"),
    ("Зан үйлийн оноо", "w_repeated_shelf", "Тавиур давтан зочлох", "оноо"),
    ("Зан үйлийн оноо", "w_exit_after_conceal", "Нуусны дараа гарц руу", "оноо"),
    ("Хугацаа — давтамж", "interval_holding", "Эд зүйл барих — давтамж", "сек"),
    ("Хугацаа — давтамж", "interval_conceal", "Эд зүйл нуух — давтамж", "сек"),
    ("Хугацаа — давтамж", "interval_wrist_torso", "Гар бие рүү — давтамж", "сек"),
    ("Хугацаа — давтамж", "interval_repeated_shelf", "Тавиур давтан — давтамж", "сек"),
    ("Хугацаа — давтамж", "interval_exit_after_conceal", "Гарц руу — давтамж", "сек"),
    ("Хугацаа — үргэлжлэх", "mindur_holding", "Эд зүйл барих — үргэлжлэх", "сек"),
    ("Хугацаа — үргэлжлэх", "mindur_conceal", "Эд зүйл нуух — үргэлжлэх", "сек"),
    ("Хугацаа — үргэлжлэх", "mindur_wrist_torso", "Гар бие рүү — үргэлжлэх", "сек"),
    ("Хугацаа — үргэлжлэх", "mindur_repeated_shelf", "Тавиур давтан — үргэлжлэх", "сек"),
    ("Хугацаа — үргэлжлэх", "mindur_exit_after_conceal", "Гарц руу — үргэлжлэх", "сек"),
    ("Зон", "repeated_shelf_threshold", "Тавиур давтахын босго", "удаа"),
    ("Эрсдэл → эпизод", "open_risk", "Эпизод нээх босго", "оноо"),
    ("Эрсдэл → эпизод", "close_risk", "Эпизод хаах босго", "оноо"),
    ("Эрсдэл → эпизод", "decay", "Оноо бууралт", "×/сек"),
    ("Эрсдэл → эпизод", "post_quiet_sec", "Намжих хугацаа", "сек"),
    ("Эрсдэл → эпизод", "band_yellow", "Шар туяа", "оноо"),
    ("Эрсдэл → эпизод", "band_red", "Улаан туяа", "оноо"),
    ("Илрүүлэлт (YOLO)", "person_conf", "Хүн илрүүлэх итгэл", "0–1"),
    ("Илрүүлэлт (YOLO)", "item_conf", "Бараа илрүүлэх итгэл", "0–1"),
    ("Илрүүлэлт (YOLO)", "frame_skip", "Кадр алгасалт", "кадр"),
    ("Геометр", "reach_frac", "Барих радиус", "× өндөр"),
    ("Геометр", "near_frac", "Нуух радиус", "× өндөр"),
    ("Геометр", "min_kp_conf", "Цэгийн итгэл", "0–1"),
    ("Геометр", "iou_match", "Track тааруулалт", "0–1"),
    ("Геометр", "drop_after_sec", "Track хаях", "сек"),
    ("Бичлэг", "pre_sec", "Өмнөх (pre-roll)", "сек"),
    ("Бичлэг", "post_sec", "Дараах (post-roll)", "сек"),
    ("Бичлэг", "keep_sec", "Завсрын хадгалалт", "сек"),
    ("Бичлэг", "max_clips", "Бичлэгийн дээд тоо", "ш"),
    ("Бичлэг", "upload_clips", "Cloud руу илгээх", ""),
)

# «Сэжигтэй» table column widths (px) — header + every row share these so the
# columns line up; the «Зан үйл» column takes the slack (expand).
_CLIP_COL_CAM = 150
_CLIP_COL_WHEN = 150
_CLIP_COL_RISK = 72
_CLIP_COL_DUR = 64
_CLIP_COL_STATUS = 110
# Edge clip id (`{camera_id}_{epoch}`) — the SAME string the backend stores as the
# alert's `edge_clip_id`, so this row matches its frontend «Сэжигтэй үйлдэл» alert.
_CLIP_COL_ID = 210
_CLIP_ROW_BG = UI_SURFACE
_CLIP_ROW_HOVER = UI_MUTED_HOVER


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


_primary = [BRAND_PRIMARY, BRAND_PRIMARY]
_primary_hover = [BRAND_PRIMARY_HOVER, BRAND_PRIMARY_HOVER]
_bg = [UI_BG, UI_BG]
_surface = [UI_SURFACE, UI_SURFACE]
_muted = [UI_MUTED, UI_MUTED]
_border = [UI_BORDER, UI_BORDER]
_fg = [UI_FG, UI_FG]
# Window + dialog base = near-black; default frames = the elevated surface so
# cards/sidebar/header read one shade above the page (transparent frames stay
# transparent and show the тас хар base through).
_theme("CTk", "fg_color", _bg)
_theme("CTkToplevel", "fg_color", _bg)
_theme("CTkFrame", "fg_color", _surface)
_theme("CTkFrame", "top_fg_color", _surface)
_theme("CTkFrame", "border_color", _border)
_theme("CTkScrollableFrame", "fg_color", _surface)
# Buttons → royal-blue primary; white label; subtle border for ghost buttons.
_theme("CTkButton", "fg_color", _primary)
_theme("CTkButton", "hover_color", _primary_hover)
_theme("CTkButton", "text_color", _fg)
_theme("CTkButton", "border_color", _border)
# Labels default to near-white foreground.
_theme("CTkLabel", "text_color", _fg)
# Dropdowns + combos → muted fill with a blue active/hover.
_theme("CTkOptionMenu", "fg_color", _muted)
_theme("CTkOptionMenu", "button_color", _primary)
_theme("CTkOptionMenu", "button_hover_color", _primary_hover)
_theme("CTkOptionMenu", "text_color", _fg)
_theme("CTkComboBox", "fg_color", _muted)
_theme("CTkComboBox", "button_color", _primary)
_theme("CTkComboBox", "button_hover_color", _primary_hover)
_theme("CTkComboBox", "border_color", _border)
# Inputs: muted fill + subtle border (CTk's default focus ring is already blue).
_theme("CTkEntry", "fg_color", _muted)
_theme("CTkEntry", "border_color", _border)
_theme("CTkTextbox", "fg_color", _muted)
_theme("CTkTextbox", "border_color", _border)
# Toggles / progress / sliders → blue accent so nothing reads generic.
_theme("CTkCheckBox", "fg_color", _primary)
_theme("CTkCheckBox", "hover_color", _primary_hover)
_theme("CTkCheckBox", "text_color", _fg)
_theme("CTkSwitch", "progress_color", _primary)
_theme("CTkProgressBar", "progress_color", _primary)
_theme("CTkSlider", "progress_color", _primary)
_theme("CTkSlider", "button_color", _primary)
_theme("CTkSlider", "button_hover_color", _primary_hover)
_theme("CTkSegmentedButton", "selected_color", _primary)
_theme("CTkSegmentedButton", "selected_hover_color", _primary_hover)


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


# Fixture fill colours mirror the editor (assets/floorplan/app.js FIX map).
_FIX_RGB = {
    "shelf": (61, 213, 109),
    "exit": (229, 72, 77),
    "entrance": (229, 72, 77),
    "checkout": (224, 168, 46),
}
_WALL_RGB = (156, 163, 175)
_CAM_RGB = (37, 99, 235)


def render_plan_image(plan: dict[str, Any], size: tuple[int, int] = (900, 560)) -> Image.Image | None:
    """Render a saved floor plan (walls / fixtures / cameras) to a read-only
    schematic image for the in-app preview. Returns None if the plan is empty
    (nothing drawn), so the caller can show an empty-state hint instead.

    Pure (no I/O) so it's unit-testable; mirrors the editor's colours."""
    walls = plan.get("walls") or []
    fixtures = plan.get("fixtures") or []
    cameras = plan.get("cameras") or []
    if not (walls or fixtures or cameras):
        return None

    # Fit the DRAWN content (not the whole canvas) so a small store inside a big
    # 200×200 canvas still previews large and centred.
    allpts = [p for wall in walls for p in (wall.get("points") or [])]
    allpts += [p for f in fixtures for p in (f.get("points") or [])]
    allpts += [c.get("pos") or [0, 0] for c in cameras]
    xs = [float(p[0]) for p in allpts]
    ys = [float(p[1]) for p in allpts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    cw = max(maxx - minx, 1.0)
    ch = max(maxy - miny, 1.0)
    w, h = size
    pad = 28
    scale = min((w - 2 * pad) / cw, (h - 2 * pad) / ch)
    ox = (w - cw * scale) / 2 - minx * scale
    oy = (h - ch * scale) / 2 - miny * scale

    def tx(p: Any) -> tuple[float, float]:
        return (ox + float(p[0]) * scale, oy + float(p[1]) * scale)

    img = Image.new("RGB", (w, h), (14, 14, 14))
    d = ImageDraw.Draw(img, "RGBA")
    # fixtures (filled, faint) — drawn first so walls/cameras sit on top
    for f in fixtures:
        pts = [tx(p) for p in (f.get("points") or [])]
        if len(pts) >= 3:
            col = _FIX_RGB.get(f.get("type"), (150, 150, 150))
            d.polygon(pts, fill=(*col, 48), outline=col)
    # walls (grey polylines)
    for wall in walls:
        pts = [tx(p) for p in (wall.get("points") or [])]
        if len(pts) >= 2:
            d.line(pts, fill=_WALL_RGB, width=2, joint="curve")
    # cameras (blue dot + facing tick)
    for c in cameras:
        cx, cy = tx(c.get("pos") or [0, 0])
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=_CAM_RGB)
        a = math.radians(float(c.get("dir_deg") or 0))
        d.line([(cx, cy), (cx + math.cos(a) * 20, cy + math.sin(a) * 20)], fill=_CAM_RGB, width=2)
    return img


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
        self._selected_cam: CameraRecord | None = None
        self._cam_rows: dict[str, ctk.CTkFrame] = {}
        self._selected_clip: ClipRecord | None = None
        self._clip_rows: dict[str, ctk.CTkFrame] = {}
        self._page_cameras = self._build_cameras_page(self._content)
        self._pages["cameras"] = self._page_cameras
        self._pages["plan"] = self._build_plan_page(self._content)
        self._pages["alerts"] = self._build_alerts_page(self._content)
        self._pages["behaviors"] = self._build_behaviors_page(self._content)
        self._pages["settings"] = self._build_settings_page(self._content)
        # «Шууд харах» — an empty container; the live grid is created lazily when
        # the page is shown and torn down when leaving (so RTSP is only held while
        # the page is on screen). See _ensure_live / _teardown_live.
        self._pages["live"] = ctk.CTkFrame(self._content, fg_color="transparent")
        self._live_view: Any = None
        self._live_empty: Any = None
        self._live_container: Any = None
        self._live_events_list: Any = None
        self._live_events_detail: Any = None
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

    # Page-key → topbar title. Kept separate from the sidebar labels (which carry
    # icons) so the topbar reads as a clean operations-console header.
    _PAGE_TITLES: dict[str, str] = {
        "cameras": "Камерууд",
        "plan": "Plan зураг",
        "live": "Шууд хяналт",
        "alerts": "Сэжигтэй event",
        "behaviors": "Зан үйл",
        "settings": "Тохиргоо",
    }

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color=UI_SURFACE)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        # Hairline under the topbar so it reads as a distinct band over the page.
        ctk.CTkFrame(self, height=1, fg_color=UI_BORDER, corner_radius=0).pack(fill="x", side="top")

        # Brand lockup: the Chipmo "C" mark + "Sentry" wordmark. The CTkImage ref
        # is kept on self so Tk doesn't garbage-collect it.
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left", padx=(16, 10))
        try:
            _logo = Image.open(resources.logo_header_png())
            self._logo_img = ctk.CTkImage(light_image=_logo, dark_image=_logo, size=(24, 24))
            ctk.CTkLabel(brand, image=self._logo_img, text="").pack(side="left", padx=(0, 8))
        except Exception as e:  # noqa: BLE001 — logo is cosmetic; fall back to text
            log.debug("header.logo_failed", error=str(e))
        ctk.CTkLabel(
            brand,
            text="Sentry",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=UI_FG,
        ).pack(side="left")

        # Vertical divider, then the current-page title — the header names the
        # page the operator is on (updated by _show_page), console-style.
        ctk.CTkFrame(header, width=1, height=26, fg_color=UI_BORDER, corner_radius=0).pack(
            side="left", padx=6
        )
        self._page_title = ctk.CTkLabel(
            header,
            text="",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=UI_FG,
        )
        self._page_title.pack(side="left", padx=(6, 0))

        # Right side: live status pills. The backend/pairing pill replaces the old
        # plain "Backend: …" label; camera + AI pills give an at-a-glance health
        # line. All three are updated live (heartbeat / refresh / edge ticks).
        self._pill_ai = StatusPill(header, "AI…", "neutral", dot=True)
        self._pill_ai.pack(side="right", padx=(0, 16))
        self._pill_cameras = StatusPill(header, "0 камер", "neutral", dot=True)
        self._pill_cameras.pack(side="right", padx=(0, 8))
        self._pill_backend = StatusPill(header, "шалгаж байна…", "neutral", dot=True)
        self._pill_backend.pack(side="right", padx=(0, 8))

    # Camera data-grid columns: (key, title, weight, minsize). The same weights
    # are applied to the header AND every row so cells line up; pill columns
    # (Статус/AI/Push) and the ⋯ column carry weight 0 (fixed).
    _COLUMNS: tuple[tuple[str, str, int, int], ...] = (
        ("name", "Нэр", 3, 120),
        ("ip", "IP", 2, 90),
        ("status", "Статус", 0, 96),
        ("ai", "AI", 0, 96),
        ("quality", "Чанар", 2, 80),
        ("push", "Push", 0, 92),
        ("menu", "", 0, 40),
    )

    def _configure_grid(self, frame: ctk.CTkBaseClass) -> None:
        """Apply the shared column weights/minsizes to a header or row frame."""
        for i, (_k, _t, weight, minsize) in enumerate(self._COLUMNS):
            frame.grid_columnconfigure(i, weight=weight, minsize=minsize)

    def _build_cameras_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        """Table-first camera management: a searchable data grid on the left, a
        camera detail panel on the right. Row actions live in the ⋯ menu + the
        detail panel (rows stay clean)."""
        page = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar = self._page_head(
            page, "Камерууд",
            "Мөр сонгоход баруун талд дэлгэрэнгүй нээгдэнэ · ⋯ дээр үйлдлүүд.",
        )
        # Search box filters the rendered rows live.
        self._cam_search = ctk.CTkEntry(
            toolbar, placeholder_text="Камер, IP хайх…", width=200, height=32
        )
        self._cam_search.pack(side="left", padx=(0, 8))
        self._cam_search.bind("<KeyRelease>", lambda _e: self._apply_camera_filter())
        ctk.CTkButton(
            toolbar, text="🔍 Scan", width=90, height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=self.open_scan,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            toolbar, text="➕ Нэмэх", width=100, height=32,
            fg_color=BRAND_PRIMARY, hover_color=BRAND_PRIMARY_HOVER,
            command=self.open_add,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            toolbar, text="↻ Сэргээх", width=100, height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=self.refresh_cameras,
        ).pack(side="left")

        # Split: data grid (expands) | fixed-width detail panel.
        split = ctk.CTkFrame(page, fg_color="transparent")
        split.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        grid_panel = Panel(split, "Камерын жагсаалт", pad=0)
        grid_panel.pack(side="left", fill="both", expand=True, padx=(0, 12))
        head = ctk.CTkFrame(grid_panel.body, fg_color="transparent", height=32)
        head.pack(fill="x", padx=12, pady=(6, 0))
        head.pack_propagate(False)
        self._configure_grid(head)
        for i, (_k, text, _w, _m) in enumerate(self._COLUMNS):
            ctk.CTkLabel(
                head, text=text, anchor="w",
                font=ctk.CTkFont(size=11, weight="bold"), text_color=UI_MUTED_FG,
            ).grid(row=0, column=i, sticky="w", padx=6)
        ctk.CTkFrame(grid_panel.body, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
            fill="x", padx=12, pady=(6, 0)
        )
        self.list_frame = ctk.CTkScrollableFrame(grid_panel.body, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        detail_panel = Panel(split, "Камерын дэлгэрэнгүй")
        detail_panel.configure(width=320)
        detail_panel.pack(side="left", fill="y")
        detail_panel.pack_propagate(False)
        self._cam_detail = detail_panel.body
        self._render_camera_detail(None)
        return page

    def _build_statusbar(self) -> None:
        # Hairline above the status bar, then the bar itself.
        ctk.CTkFrame(self, height=1, fg_color=UI_BORDER, corner_radius=0).pack(
            fill="x", side="bottom"
        )
        bar = ctk.CTkFrame(self, height=26, corner_radius=0, fg_color=UI_SURFACE)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        # Left: transient status message (set_status). Right: fixed system segments
        # (Store · Camera · AI · Version) — the console's always-on health line.
        self.status_label = ctk.CTkLabel(
            bar,
            text="Бэлэн",
            font=ctk.CTkFont(size=11),
            text_color=UI_MUTED_FG,
            anchor="w",
        )
        self.status_label.pack(side="left", padx=14)

        self._sys_segments: dict[str, ctk.CTkLabel] = {}

        def _seg(key: str, initial: str) -> None:
            lbl = ctk.CTkLabel(
                bar, text=initial, font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG
            )
            lbl.pack(side="right", padx=(0, 14))
            self._sys_segments[key] = lbl

        # Packed right-to-left, so declare in reverse of visual order.
        _seg("version", f"v{__version__}")
        _seg("ai", "AI: —")
        _seg("camera", "Камер: —")
        _seg("store", "Дэлгүүр: —")
        self._refresh_statusbar()

    def _refresh_statusbar(self) -> None:
        """Repaint the fixed right-hand system segments from current state."""
        segs = getattr(self, "_sys_segments", None)
        if not segs:
            return
        st = load_state()
        n = len(st.cameras)
        ai_text, _ = self._edge_status_pill()
        with contextlib.suppress(Exception):
            segs["store"].configure(text=f"Дэлгүүр: {st.store_name or '—'}")
            segs["camera"].configure(text=f"Камер: {n}")
            segs["ai"].configure(text=f"AI: {ai_text}")

    # === Sidebar navigation + pages ===

    _NAV: tuple[tuple[str, str, str], ...] = (
        ("cameras", "📷  Камерууд", "page"),
        ("plan", "🗺  Plan зураг", "page"),
        ("live", "📺  Шууд харах", "page"),
        ("alerts", "⚠  Сэжигтэй", "page"),
        ("behaviors", "🎯  Зан үйл", "page"),
        ("settings", "⚙  Тохиргоо", "page"),
    )

    def _build_sidebar(self, parent: ctk.CTkBaseClass) -> None:
        side = ctk.CTkFrame(parent, width=170, corner_radius=0, fg_color=UI_SURFACE)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        for key, label, kind in self._NAV:
            cmd = self._action_cmd(key) if kind == "action" else self._page_cmd(key)
            btn = ctk.CTkButton(
                side,
                text=label,
                anchor="w",
                height=40,
                corner_radius=8,
                fg_color="transparent",
                text_color=UI_FG,
                hover_color=UI_MUTED_HOVER,
                font=ctk.CTkFont(size=14),
                command=cmd,
            )
            btn.pack(fill="x", padx=10, pady=(10 if key == "cameras" else 2, 2))
            if kind == "page":
                self._nav_buttons[key] = btn
        # Sidebar footer — a compact mini-status block (mockup: AI Engine · Stream ·
        # Version). Pinned to the bottom so "is the AI running / are we connected"
        # is always visible without opening «Тохиргоо».
        footer = ctk.CTkFrame(side, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=10, pady=10)
        ctk.CTkFrame(footer, height=1, fg_color=UI_BORDER, corner_radius=0).pack(
            fill="x", pady=(0, 10)
        )

        def _mini_row(label: str) -> ctk.CTkFrame:
            r = ctk.CTkFrame(footer, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(
                r, text=label, anchor="w", font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG
            ).pack(side="left")
            return r

        ai_row = _mini_row("AI хөдөлгүүр")
        self._mini_ai = StatusPill(ai_row, "…", "neutral", dot=True)
        self._mini_ai.pack(side="right")
        stream_row = _mini_row("Урсгал")
        self._mini_stream = StatusPill(stream_row, "RTSP", "neutral", dot=False)
        self._mini_stream.pack(side="right")
        ver_row = _mini_row("Хувилбар")
        ctk.CTkLabel(
            ver_row, text=f"v{__version__}", anchor="e", font=ctk.CTkFont(size=11, weight="bold")
        ).pack(side="right")
        self._refresh_edge_status()

    def _page_cmd(self, key: str) -> Callable[[], None]:
        """A nav-button command that shows `key`'s page (binds key, no loop closure bug)."""
        return lambda: self._show_page(key)

    def _action_cmd(self, _key: str) -> Callable[[], None]:
        """Nav-button command for an 'action' item — opens a separate window
        (the floor-plan webview, which can't embed in the Tk app)."""
        return self.open_floor_plan

    def _show_page(self, name: str) -> None:
        # Leaving «Шууд харах» releases its RTSP sessions (the grid is recreated
        # on return), so cameras aren't decoded while another page is open.
        if name != "live":
            self._teardown_live()
        for page in self._pages.values():
            page.pack_forget()
        self._pages[name].pack(fill="both", expand=True)
        for key, btn in self._nav_buttons.items():
            btn.configure(fg_color=BRAND_PRIMARY if key == name else "transparent")
        # Topbar title tracks the visible page (console header).
        if getattr(self, "_page_title", None) is not None:
            self._page_title.configure(text=self._PAGE_TITLES.get(name, ""))
        if name == "live":
            self._ensure_live()
        elif name == "alerts":
            self._refresh_alerts()
        elif name == "behaviors":
            self._refresh_behaviors()
        elif name == "plan":
            self._refresh_plan_preview()
        elif name == "settings":
            self._refresh_settings()

    def _ensure_live(self) -> None:
        """Build the live monitoring page: the camera grid (left) + a Live Events
        panel (right) inside a split. Empty-state when no cameras. Idempotent.

        The events panel reads the shared clip store — it does NOT touch the
        LocalLiveView threading; LocalLiveView is just reparented into the left
        column of the split."""
        if self._live_view is not None or self._live_empty is not None:
            return
        if not load_state().cameras:
            self._live_empty = ctk.CTkLabel(
                self._pages["live"],
                text="Камер бүртгэгдээгүй — эхлээд «Камерууд» хуудаснаас камер нэмнэ үү.",
                font=ctk.CTkFont(size=14),
                text_color=UI_MUTED_FG,
                justify="center",
            )
            self._live_empty.pack(expand=True)
            return
        from sentry_agent_pc.gui.local_view import LocalLiveView

        container = ctk.CTkFrame(self._pages["live"], fg_color="transparent")
        container.pack(fill="both", expand=True)
        self._live_container = container
        self._live_view = LocalLiveView(container)
        self._live_view.pack(side="left", fill="both", expand=True)
        self._build_live_events(container)
        self.set_status("Шууд харах (LAN-аас шууд)…")

    def _teardown_live(self) -> None:
        """Stop + remove the live grid (releases RTSP) and the events panel. Safe
        to call any time."""
        if self._live_view is not None:
            with contextlib.suppress(Exception):
                self._live_view.stop()  # releases RTSP + destroys its frame
            self._live_view = None
        if getattr(self, "_live_container", None) is not None:
            with contextlib.suppress(Exception):
                self._live_container.destroy()
            self._live_container = None
        self._live_events_list = None
        self._live_events_detail = None
        if self._live_empty is not None:
            with contextlib.suppress(Exception):
                self._live_empty.destroy()
            self._live_empty = None

    def _build_live_events(self, parent: ctk.CTkBaseClass) -> None:
        """Right-hand «Live Events» panel: a compact scrollable clip queue (top) +
        an inline detail card (bottom). Mirrors the cloud /live console."""
        panel = Panel(parent, "Live Events")
        panel.configure(width=340)
        panel.pack(side="left", fill="y", padx=(12, 0))
        panel.pack_propagate(False)
        self._live_events_count = StatusPill(panel.head, "0", "neutral", dot=False)
        self._live_events_count.pack(side="right")
        # Top: scrollable list of recent clips.
        self._live_events_list = ctk.CTkScrollableFrame(panel.body, fg_color="transparent")
        self._live_events_list.pack(fill="both", expand=True)
        # Bottom: inline detail card for the selected clip.
        ctk.CTkFrame(panel.body, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
            fill="x", pady=8
        )
        self._live_events_detail = ctk.CTkFrame(panel.body, fg_color="transparent", height=150)
        self._live_events_detail.pack(fill="x")
        self._live_events_detail.pack_propagate(False)
        self._refresh_live_events()

    def _refresh_live_events(self) -> None:
        holder = getattr(self, "_live_events_list", None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        try:
            clips = self._clip_store().records()
        except Exception:  # noqa: BLE001 — a corrupt index must not break the page
            clips = []
        with contextlib.suppress(Exception):
            self._live_events_count.set(str(len(clips)), "warn" if clips else "neutral")
        if not clips:
            ctk.CTkLabel(
                holder, text="Одоогоор event алга.\nAI сэжигтэй үйлдэл илрүүлбэл энд гарна.",
                text_color=UI_MUTED_FG, justify="center", font=ctk.CTkFont(size=11),
            ).pack(pady=30)
            self._render_live_event_detail(None)
            return
        recent = sorted(clips, key=lambda r: r.created_at, reverse=True)[:40]
        for clip in recent:
            self._render_live_event_row(clip)
        self._render_live_event_detail(recent[0])

    def _render_live_event_row(self, clip: ClipRecord) -> None:
        import datetime

        row = ctk.CTkFrame(self._live_events_list, fg_color=_CLIP_ROW_BG, corner_radius=8)
        row.pack(fill="x", pady=2)
        when = datetime.datetime.fromtimestamp(clip.started_at).strftime("%H:%M:%S")
        ctk.CTkLabel(
            row, text=when, width=64, anchor="w", font=ctk.CTkFont(size=11),
            text_color=UI_MUTED_FG,
        ).pack(side="left", padx=(8, 4), pady=7)
        StatusPill(row, f"{clip.risk_pct:.0f}%", self._risk_variant(clip.risk_pct), dot=False).pack(
            side="left", padx=4
        )
        labels = [_EDGE_BEHAVIOR_LABELS.get(b, b) for b in clip.behaviors]
        ctk.CTkLabel(
            row, text=(" · ".join(labels) or "—"), anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color=UI_FG,
        ).pack(side="left", fill="x", expand=True, padx=4)
        _bind_row_click(row, lambda: self._render_live_event_detail(clip))

    def _render_live_event_detail(self, clip: ClipRecord | None) -> None:
        holder = getattr(self, "_live_events_detail", None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        if clip is None:
            ctk.CTkLabel(
                holder, text="Event сонгоно уу.", text_color=UI_MUTED_FG,
                font=ctk.CTkFont(size=11),
            ).pack(expand=True)
            return
        import datetime

        ctk.CTkLabel(
            holder, text=clip.camera_id, anchor="w", font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w")
        top = ctk.CTkFrame(holder, fg_color="transparent")
        top.pack(fill="x", pady=(3, 2))
        StatusPill(top, f"Эрсдэл {clip.risk_pct:.0f}%", self._risk_variant(clip.risk_pct), dot=False).pack(
            side="left"
        )
        ctk.CTkLabel(
            top, text=datetime.datetime.fromtimestamp(clip.started_at).strftime("%H:%M:%S")
            + f" · {clip.duration:.0f}с",
            text_color=UI_MUTED_FG, font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=8)
        labels = [_EDGE_BEHAVIOR_LABELS.get(b, b) for b in clip.behaviors]
        ctk.CTkLabel(
            holder, text=(" · ".join(labels) or "—"), anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG, wraplength=300,
        ).pack(anchor="w", pady=(2, 6))
        btns = ctk.CTkFrame(holder, fg_color="transparent")
        btns.pack(fill="x")
        ctk.CTkButton(
            btns, text="▶ Видео", height=30,
            fg_color=BRAND_PRIMARY, hover_color=BRAND_PRIMARY_HOVER,
            command=lambda p=clip.path: self._open_clip(p),
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            btns, text="⛶ Дэлгэрэнгүй", height=30,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=lambda c=clip: self._open_clip_detail(c),
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

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
        """Incident queue (left) + review panel (right). A row selects a clip and
        shows its full per-fire timeline inline; «▶ Видео» opens the recording."""
        page = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar = self._page_head(
            page, "Сэжигтэй event",
            "AI сэжигтэй үйлдэл илрүүлбэл [−3с…+3с] бичлэг энд орж ирнэ. Мөр сонгож шалгана.",
        )
        ctk.CTkButton(
            toolbar, text="↻ Сэргээх", width=100, height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=self._refresh_alerts,
        ).pack(side="right")

        split = ctk.CTkFrame(page, fg_color="transparent")
        split.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        queue = Panel(split, "Incident queue", pad=0)
        queue.pack(side="left", fill="both", expand=True, padx=(0, 12))
        self._alerts_count = StatusPill(queue.head, "0", "neutral", dot=False)
        self._alerts_count.pack(side="right")
        # Column header — shares the row cell widths so columns line up.
        hdr = ctk.CTkFrame(queue.body, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(6, 0))
        for text, w, anchor in (
            ("Камер", _CLIP_COL_CAM, "w"),
            ("Огноо · цаг", _CLIP_COL_WHEN, "w"),
            ("Эрсдэл", _CLIP_COL_RISK, "w"),
            ("Зан үйл", 0, "w"),
            ("Хугацаа", _CLIP_COL_DUR, "e"),
            # Edge clip id — matches the frontend «Сэжигтэй үйлдэл» alert's edge id.
            ("ID", _CLIP_COL_ID, "w"),
            ("Төлөв", _CLIP_COL_STATUS, "w"),
        ):
            ctk.CTkLabel(
                hdr, text=text, anchor=anchor,
                font=ctk.CTkFont(size=11, weight="bold"), text_color=UI_MUTED_FG,
                **({"width": w} if w else {}),
            ).pack(side="left", fill=("x" if not w else None), expand=(not w), padx=8)
        ctk.CTkFrame(queue.body, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
            fill="x", padx=12, pady=(6, 0)
        )
        self._alerts_frame = ctk.CTkScrollableFrame(queue.body, fg_color="transparent")
        self._alerts_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        review = Panel(split, "Review")
        review.configure(width=360)
        review.pack(side="left", fill="y")
        review.pack_propagate(False)
        self._alerts_review = review.body
        self._render_clip_review(None)
        return page

    def _refresh_alerts(self) -> None:
        for w in self._alerts_frame.winfo_children():
            w.destroy()
        self._clip_rows = {}
        try:
            clips = self._clip_store().records()
        except Exception:  # noqa: BLE001 — a corrupt index must not break the page
            clips = []
        with contextlib.suppress(Exception):
            self._alerts_count.set(str(len(clips)), "warn" if clips else "neutral")
        if not clips:
            ctk.CTkLabel(
                self._alerts_frame,
                text="Сэжигтэй бичлэг алга.\n\nAI сэжигтэй үйлдэл илрүүлбэл\n[−3с … +3с] бичлэг энд гарч ирнэ.",
                text_color=UI_MUTED_FG,
                justify="center",
            ).pack(pady=50)
            self._render_clip_review(None)
            return
        for clip in sorted(clips, key=lambda r: r.created_at, reverse=True):
            self._render_clip_row(clip)

    @staticmethod
    def _risk_variant(risk_pct: float) -> str:
        """Low = green, Medium = amber, High = red (mockup risk bands)."""
        return "danger" if risk_pct >= 70 else ("warn" if risk_pct >= 40 else "good")

    @staticmethod
    def _clip_status_meta(status: str) -> tuple[str, str]:
        """(label, pill variant) for a clip's operator-triage status."""
        return {
            "confirmed": ("Батлагдсан", "danger"),
            "dismissed": ("Няцаагдсан", "neutral"),
            "escalated": ("☁ Дээшлүүлсэн", "danger"),
        }.get(status, ("Нээлттэй", "blue"))

    def _set_clip_status(self, clip: ClipRecord, status: str) -> None:
        """Operator triage: persist a clip's status locally, then repaint the
        queue + review panel so the change is immediate."""
        try:
            self._clip_store().set_status(clip.clip_id, status)
        except Exception as e:  # noqa: BLE001 — a triage write must not crash the page
            self.set_status(f"⚠ Төлөв хадгалж чадсангүй: {str(e)[:60]}")
            return
        clip.status = status  # reflect in the in-memory record we still hold
        label, _ = self._clip_status_meta(status)
        self.set_status(f"Тохиолдол «{label}» болголоо")
        self._refresh_alerts()
        self._select_clip(clip)

    def _escalate_clip(self, clip: ClipRecord) -> None:
        """Operator escalation: push this local clip up to the cloud so it becomes
        an org-wide alert (server re-scores + runs the VLM). Useful when a clip
        stayed local — its auto-upload failed, or the camera is edge-only. Reuses
        the existing `/agent/edge/clips` upload (no backend change). Runs on a
        background thread (network + retry backoff); on success marks the clip
        «escalated»."""
        from pathlib import Path

        cam = next((c for c in load_state().cameras if c.name == clip.camera_id), None)
        if cam is None or not cam.uuid:
            self.set_status("⚠ Энэ камер бүртгэлгүй тул cloud руу дээшлүүлэх боломжгүй")
            return
        if not Path(clip.path).exists():
            self.set_status("⚠ Бичлэгийн файл олдсонгүй — дээшлүүлэх боломжгүй")
            return
        camera_uuid = cam.uuid

        def work() -> dict[str, Any]:
            from sentry_agent_pc.edge.uploader import upload_clip

            ok = upload_clip(BackendClient(), clip, camera_uuid)
            return {"ok": ok}

        def done(result: Any) -> None:
            if isinstance(result, dict) and result.get("ok"):
                self._set_clip_status(clip, "escalated")
                self.set_status("☁ Тохиолдол cloud руу дээшлүүлэгдэж, alert үүслээ")
            else:
                self.set_status("⚠ Cloud руу илгээж чадсангүй — сүлжээ/холболтоо шалгаад дахина уу")

        self._run_bg(work, done, status="☁ Cloud руу дээшлүүлж байна…")

    def _render_clip_row(self, clip: ClipRecord) -> None:
        """One clip as a clickable incident-queue row — click fills the review panel."""
        import datetime

        selected = self._selected_clip is not None and clip.clip_id == self._selected_clip.clip_id
        base = UI_SURFACE_2 if selected else _CLIP_ROW_BG
        row = ctk.CTkFrame(self._alerts_frame, fg_color=base, corner_radius=8)
        row.pack(fill="x", pady=2)
        self._clip_rows[clip.clip_id] = row
        when = datetime.datetime.fromtimestamp(clip.started_at).strftime("%Y-%m-%d %H:%M:%S")
        labels = [_EDGE_BEHAVIOR_LABELS.get(b, b) for b in clip.behaviors]
        beh = " · ".join(labels) or "—"

        ctk.CTkLabel(
            row, text=clip.camera_id, width=_CLIP_COL_CAM, anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=8, pady=8)
        ctk.CTkLabel(
            row, text=when, width=_CLIP_COL_WHEN, anchor="w",
            font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG,
        ).pack(side="left", padx=8)
        risk_pill = StatusPill(
            row, f"{clip.risk_pct:.0f}%", self._risk_variant(clip.risk_pct), dot=False
        )
        risk_pill.pack(side="left", padx=8)
        ctk.CTkLabel(
            row, text=beh, anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color=UI_FG,
        ).pack(side="left", fill="x", expand=True, padx=8)
        ctk.CTkLabel(
            row, text=f"{clip.duration:.0f}с", width=_CLIP_COL_DUR, anchor="e",
            font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG,
        ).pack(side="left", padx=8)
        # Edge clip id — the SAME string as the frontend alert's `edge_clip_id`,
        # so staff can cross-reference a desktop clip with its cloud alert.
        ctk.CTkLabel(
            row, text=clip.clip_id, width=_CLIP_COL_ID, anchor="w",
            font=ctk.CTkFont(size=11, family="Consolas"), text_color=UI_MUTED_FG,
        ).pack(side="left", padx=8)
        st_label, st_variant = self._clip_status_meta(clip.status)
        # Fixed-width holder (with an explicit height so the row stays compact —
        # a propagate-off frame with no height would inflate the row).
        st_holder = ctk.CTkFrame(row, fg_color="transparent", width=_CLIP_COL_STATUS, height=26)
        st_holder.pack(side="left", padx=8, pady=6)
        st_holder.pack_propagate(False)
        StatusPill(st_holder, st_label, st_variant, dot=False).pack(side="left")

        _bind_row_click(row, lambda: self._select_clip(clip))
        for child in row.winfo_children():
            with contextlib.suppress(Exception):
                child.bind("<Button-1>", lambda _e: self._select_clip(clip))

    def _select_clip(self, clip: ClipRecord) -> None:
        """Highlight the incident row + populate the review panel."""
        self._selected_clip = clip
        for cid, row in self._clip_rows.items():
            with contextlib.suppress(Exception):
                row.configure(fg_color=UI_SURFACE_2 if cid == clip.clip_id else _CLIP_ROW_BG)
        self._render_clip_review(clip)

    def _render_clip_review(self, clip: ClipRecord | None) -> None:
        """Fill the right-side review panel: header, per-fire timeline table, note
        and the «▶ Видео» / «⛶ Дэлгэрэнгүй» actions (or an empty-state hint)."""
        import datetime

        holder = getattr(self, "_alerts_review", None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        if clip is None:
            ctk.CTkLabel(
                holder, text="Тохиолдол сонгоно уу.\n\nЗүүн талын жагсаалтаас мөр\nдээр дарж шалгана.",
                text_color=UI_MUTED_FG, justify="center", font=ctk.CTkFont(size=12),
            ).pack(expand=True, pady=40)
            return
        started = datetime.datetime.fromtimestamp(clip.started_at)
        ctk.CTkLabel(
            holder, text=clip.camera_id, anchor="w", font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")
        head_row = ctk.CTkFrame(holder, fg_color="transparent")
        head_row.pack(fill="x", pady=(4, 2))
        StatusPill(
            head_row, f"Эрсдэл {clip.risk_pct:.0f}%", self._risk_variant(clip.risk_pct), dot=False
        ).pack(side="left")
        ctk.CTkLabel(
            head_row, text=f"{clip.duration:.0f}с", text_color=UI_MUTED_FG,
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=8)
        st_label, st_variant = self._clip_status_meta(clip.status)
        StatusPill(head_row, st_label, st_variant, dot=True).pack(side="right")
        ctk.CTkLabel(
            holder, text=started.strftime("%Y-%m-%d %H:%M:%S"), anchor="w",
            font=ctk.CTkFont(size=11), text_color=UI_MUTED_FG,
        ).pack(anchor="w")
        ctk.CTkLabel(
            holder, text=f"ID: {clip.clip_id}", anchor="w",
            font=ctk.CTkFont(size=10, family="Consolas"), text_color=UI_MUTED_FG,
        ).pack(anchor="w", pady=(2, 8))

        from sentry_agent_pc.gui.datatable import DataTable

        rows, note = self._clip_timeline_rows(clip)
        table = DataTable(
            holder,
            columns=(
                ("time", "Цаг", 92, "w"),
                ("beh", "Зан үйл", 0, "w"),
                ("score", "Оноо", 56, "e"),
            ),
            height=9,
        )
        table.pack(fill="both", expand=True, pady=(0, 6))
        # Review panel is narrow — collapse the (date, time, beh, score, risk)
        # timeline to (time, beh, score) here; the full grid is in «⛶ Дэлгэрэнгүй».
        table.set_rows([(t, b, s) for (_d, t, b, s, _r) in rows])
        ctk.CTkLabel(
            holder, text=note, anchor="w", justify="left",
            font=ctk.CTkFont(size=10), text_color=UI_MUTED_FG, wraplength=320,
        ).pack(anchor="w", pady=(0, 8))
        # Escalate — push the clip up to the cloud (org-wide alert + VLM). Full
        # width + danger accent since it's the strongest operator action.
        ctk.CTkButton(
            holder, text="⤴ Cloud руу дээшлүүлэх", height=32,
            fg_color=UI_DANGER, hover_color="#DC2626", text_color="#0A0A0A",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda c=clip: self._escalate_clip(c),
        ).pack(fill="x", pady=(0, 6))
        # Operator triage — Confirm (real incident) / Dismiss (false alarm). Writes
        # the clip's local status; the queue + this panel repaint immediately.
        ops = ctk.CTkFrame(holder, fg_color="transparent")
        ops.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(
            ops, text="✓ Батлах", height=32,
            fg_color="transparent", border_width=1, border_color=UI_DANGER, text_color=UI_DANGER,
            hover_color=UI_MUTED_HOVER,
            command=lambda c=clip: self._set_clip_status(c, "confirmed"),
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            ops, text="✕ Няцаах", height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            hover_color=UI_MUTED_HOVER,
            command=lambda c=clip: self._set_clip_status(c, "dismissed"),
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))
        btns = ctk.CTkFrame(holder, fg_color="transparent")
        btns.pack(fill="x")
        ctk.CTkButton(
            btns, text="▶ Видео", height=32,
            fg_color=BRAND_PRIMARY, hover_color=BRAND_PRIMARY_HOVER,
            command=lambda p=clip.path: self._open_clip(p),
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            btns, text="⛶ Дэлгэрэнгүй", height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=lambda c=clip: self._open_clip_detail(c),
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _clip_timeline_rows(
        self, clip: ClipRecord
    ) -> tuple[list[tuple[str, str, str, str, str]], str]:
        """Build the per-fire timeline rows (date, time, beh, score, risk) + a
        summary note. Shared by the inline review panel and the detail modal."""
        import datetime

        rows: list[tuple[str, str, str, str, str]] = []
        if clip.events:
            total = 0.0
            for ev in clip.events:
                key = str(ev.get("key", ""))
                label = _EDGE_BEHAVIOR_LABELS.get(key, key)
                amount = float(ev.get("amount", 0) or 0)
                risk = float(ev.get("risk", 0) or 0)
                ts = float(ev.get("ts", 0) or 0)
                total += amount
                if ts > 0:
                    dt = datetime.datetime.fromtimestamp(ts)
                    date_s = dt.strftime("%Y-%m-%d")
                    time_s = dt.strftime("%H:%M:%S") + f".{int((ts % 1) * 1000):03d}"
                else:
                    date_s, time_s = "—", "—"
                rows.append((date_s, time_s, label, f"+{amount:.0f}", f"{risk:.0f}%"))
            note = (
                f"Нийт {len(rows)} дохио · {clip.duration:.0f}с дотор · цугларсан +{total:.0f} оноо. "
                "Толгойн багана дээр дарж эрэмбэлнэ."
            )
        elif clip.behavior_detail:
            total = 0.0
            for d in clip.behavior_detail:
                label = _EDGE_BEHAVIOR_LABELS.get(str(d.get("key", "")), str(d.get("key", "")))
                score = float(d.get("score", 0) or 0)
                total += score
                rows.append(("—", "—", label, f"+{score:.0f}", "—"))
            note = (
                "Энэ бичлэг хуучин хувилбараар бичигдсэн тул хугацааны задаргаа алга "
                f"— зөвхөн нийт +{total:.0f} оноо."
            )
        else:
            note = "Энэ бичлэгт зан үйлийн задаргаа бүртгэгдээгүй."
        return rows, note

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
            text_color=UI_MUTED_FG,
        ).pack(anchor="w")
        # Full edge clip id — matches the frontend «Сэжигтэй үйлдэл» alert's "ID".
        ctk.CTkLabel(
            head,
            text=f"ID: {clip.clip_id}",
            anchor="w",
            font=ctk.CTkFont(size=11, family="Consolas"),
            text_color=UI_MUTED_FG,
        ).pack(anchor="w", pady=(2, 0))

        from sentry_agent_pc.gui.datatable import DataTable

        # Footer pinned to the bottom FIRST (setup_dialog convention) so it never
        # gets clipped by the expanding table.
        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=16, pady=(2, 12))

        # One sortable datagrid: Огноо · Цаг · Зан үйл · Оноо · Эрсдэл. The «Цаг»
        # carries milliseconds so the founder sees the exact moment + cadence.
        table = DataTable(
            win,
            columns=(
                ("date", "Огноо", 100, "w"),
                ("time", "Цаг", 116, "w"),
                ("beh", "Зан үйл", 0, "w"),
                ("score", "Оноо", 70, "e"),
                ("risk", "Эрсдэл", 72, "e"),
            ),
        )
        table.pack(fill="both", expand=True, padx=16, pady=(8, 4))

        rows, note = self._clip_timeline_rows(clip)
        table.set_rows(rows)

        ctk.CTkLabel(
            foot,
            text=note,
            anchor="w",
            font=ctk.CTkFont(size=10),
            text_color=UI_MUTED_FG,
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

    # === «Зан үйл» page — the FULL effective edge config (read-only, global) ===

    def _build_behaviors_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        """The FULL effective edge config this PC runs YOLO + the behaviour engine
        with — read-only, live from superadmin's «Edge тохиргоо» (global). Two
        tables: each BEHAVIOUR is ONE ROW with its score + timing as COLUMNS
        (оноо · давтамж · үргэлжлэх), then a second table for the remaining
        single-value settings (episode FSM, detection, geometry, recording)."""
        page = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar = self._page_head(
            page, "Зан үйл",
            "AI хөдөлгүүр (YOLO + зан үйл) ЯГ доорх тохиргоогоор ажиллана.",
        )
        # Read-only truth: these values are managed globally from superadmin's «Edge
        # тохиргоо» — the desktop only DISPLAYS them. A pill makes that explicit so
        # nobody hunts for a Save button that shouldn't exist.
        StatusPill(toolbar, "🔒 Зөвхөн харах", "neutral", dot=False).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            toolbar, text="↻ Сэргээх", width=100, height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=self._refresh_behaviors,
        ).pack(side="right")

        body = ctk.CTkScrollableFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self._behaviors_version = ctk.CTkLabel(
            body, text="", anchor="w", font=ctk.CTkFont(size=10), text_color=UI_MUTED_FG
        )
        self._behaviors_version.pack(anchor="w", pady=(0, 8))

        from sentry_agent_pc.gui.datatable import DataTable

        # Panel 1 — one row per behaviour, score + timing + meaning as COLUMNS.
        p1 = Panel(body, "Зан үйлүүд")
        p1.pack(fill="x", pady=(0, 12))
        self._behaviors_table = DataTable(
            p1.body,
            columns=(
                ("beh", "Зан үйл", 170, "w"),
                ("desc", "Тайлбар", 0, "w"),
                ("score", "Оноо", 70, "e"),
                ("interval", "Давтамж (с)", 110, "e"),
                ("mindur", "Үргэлжлэх (с)", 120, "e"),
            ),
            height=len(_EDGE_BEHAVIORS) + 1,
        )
        self._behaviors_table.pack(fill="x")

        # Panel 2 — the remaining single-value settings (episode FSM / detection /
        # geometry / recording).
        p2 = Panel(body, "Бусад тохиргоо")
        p2.pack(fill="both", expand=True)
        self._other_table = DataTable(
            p2.body,
            columns=(
                ("group", "Бүлэг", 170, "w"),
                ("label", "Тохиргоо", 0, "w"),
                ("value", "Утга", 100, "e"),
                ("unit", "Нэгж", 90, "w"),
            ),
            height=15,
        )
        self._other_table.pack(fill="both", expand=True)
        self.after(0, self._refresh_behaviors)
        return page

    @staticmethod
    def _merged_edge_config(override: dict[str, Any] | None) -> dict[str, Any]:
        """EdgeConfig() defaults for every displayed field, overlaid with a live
        fetch (only keys the server actually returned win)."""
        from sentry_agent_pc.edge.config import EdgeConfig

        base = EdgeConfig()
        keys = set(_BEHAVIOR_FIELD_KEYS) | {k for _, k, _, _ in _EDGE_CONFIG_ROWS}
        merged = {k: getattr(base, k, None) for k in keys}
        if override:
            for k in merged:
                if override.get(k) is not None:
                    merged[k] = override[k]
        return merged

    @staticmethod
    def _behavior_table_rows(cfg: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
        """One row per behaviour: label · meaning · score (+N) · interval ·
        min-duration. A zero interval/duration shows «—» (no gate / one-shot)."""

        def num(raw: Any, *, dash_zero: bool = False) -> str:
            if raw is None:
                return "—"
            try:
                f = float(raw)
            except (TypeError, ValueError):
                return str(raw)
            if dash_zero and f == 0:
                return "—"
            return f"{f:g}"

        rows: list[tuple[str, str, str, str, str]] = []
        for b in _EDGE_BEHAVIORS:
            w = cfg.get(b["weight_key"])
            rows.append(
                (
                    b["label"],
                    b.get("desc", ""),
                    f"+{num(w)}" if w is not None else "—",
                    num(cfg.get(b["interval_key"]), dash_zero=True),
                    num(cfg.get(b["mindur_key"]), dash_zero=True),
                )
            )
        return rows

    @staticmethod
    def _other_config_rows(cfg: dict[str, Any]) -> list[tuple[str, str, str, str]]:
        """(group, label, value, unit) for every NON-behaviour field, registry order."""

        def fmt(raw: Any) -> str:
            if isinstance(raw, bool):
                return "Тийм" if raw else "Үгүй"
            if raw is None:
                return "—"
            try:
                f = float(raw)
            except (TypeError, ValueError):
                return str(raw)
            return f"{f:g}"

        return [
            (group, label, fmt(cfg.get(key)), unit)
            for (group, key, label, unit) in _EDGE_CONFIG_ROWS
            if key not in _BEHAVIOR_FIELD_KEYS
        ]

    def _set_behavior_tables(self, cfg: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._behaviors_table.set_rows(self._behavior_table_rows(cfg))
            self._other_table.set_rows(self._other_config_rows(cfg))

    def _refresh_behaviors(self) -> None:
        # Seed from the local defaults; the live fetch overrides below.
        self._set_behavior_tables(self._merged_edge_config(None))
        self._behaviors_version.configure(text="Анхдагч утга харуулж байна…")
        threading.Thread(target=self._fetch_behavior_weights, daemon=True).start()

    def _fetch_behavior_weights(self) -> None:
        try:
            cfg = BackendClient().agent_edge_config()
        except Exception as e:  # noqa: BLE001 — offline → keep the seeded defaults
            self._post_behavior_weights(None, str(e)[:80])
            return
        self._post_behavior_weights(cfg, None)

    def _post_behavior_weights(self, cfg: dict[str, Any] | None, err: str | None) -> None:
        """Apply fetched config on the UI thread (guarded against teardown)."""
        if self._closing:
            return
        with contextlib.suppress(Exception):
            self.after(0, lambda: self._apply_behavior_weights(cfg, err))

    def _apply_behavior_weights(self, cfg: dict[str, Any] | None, err: str | None) -> None:
        if self._closing:
            return
        if cfg is None:
            self._behaviors_version.configure(
                text=f"Серверээс татаж чадсангүй ({err}). Анхдагч утга харагдаж байна."
            )
            return
        self._set_behavior_tables(self._merged_edge_config(cfg))
        ver = cfg.get("version")
        self._behaviors_version.configure(
            text=f"Серверээс татсан тохиргоо (v{ver}) · superadmin-аас тааруулна."
        )

    # === Shared page scaffolding ===

    def _page_head(
        self, page: ctk.CTkBaseClass, title: str, subtitle: str
    ) -> ctk.CTkFrame:
        """A consistent console page header: title + subtitle on the left, an
        (empty) toolbar frame returned for the caller to pack right-aligned
        actions into. Every page uses this so headers line up identically."""
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 10))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(
            titles, text=title, anchor="w", font=ctk.CTkFont(size=18, weight="bold"),
            text_color=UI_FG,
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles, text=subtitle, anchor="w", font=ctk.CTkFont(size=12),
            text_color=UI_MUTED_FG,
        ).pack(anchor="w")
        toolbar = ctk.CTkFrame(head, fg_color="transparent")
        toolbar.pack(side="right")
        return toolbar

    def _build_settings_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar = self._page_head(
            page, "Тохиргоо", "Desktop console системийн тохиргоо ба холболт."
        )
        ctk.CTkButton(
            toolbar, text="🔗 Холболт", width=110, height=32,
            fg_color=BRAND_PRIMARY, hover_color=BRAND_PRIMARY_HOVER,
            command=self.open_pairing,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            toolbar, text="⬆ Шинэчлэл", width=110, height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=self.open_update,
        ).pack(side="right")

        panel = Panel(page, "Системийн тохиргоо", pad=0)
        panel.pack(fill="x", padx=18, pady=(0, 12))
        self._settings_body = panel.body
        # Column header for the settings grid.
        hdr = ctk.CTkFrame(panel.body, fg_color="transparent", height=30)
        hdr.pack(fill="x", padx=14, pady=(6, 0))
        for text, w in (("Бүлэг", 150), ("Тохиргоо", 200), ("Утга", 0), ("Төлөв", 120)):
            ctk.CTkLabel(
                hdr, text=text, anchor="w", font=ctk.CTkFont(size=11, weight="bold"),
                text_color=UI_MUTED_FG, **({"width": w} if w else {}),
            ).pack(side="left", fill=("x" if not w else None), expand=(not w))
        ctk.CTkFrame(panel.body, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
            fill="x", padx=14, pady=(6, 0)
        )
        self._settings_rows = ctk.CTkFrame(panel.body, fg_color="transparent")
        self._settings_rows.pack(fill="x", padx=14, pady=(0, 10))
        self._refresh_settings()
        return page

    def _refresh_settings(self) -> None:
        """(Re)paint the settings grid from current state — version, connection,
        AI engine, stream. Called on build and when the settings page is shown."""
        rows = getattr(self, "_settings_rows", None)
        if rows is None:
            return
        for w in rows.winfo_children():
            w.destroy()
        st = load_state()
        ai_text, ai_variant = self._edge_status_pill()
        specs: tuple[tuple[str, str, str, str, str], ...] = (
            ("AI", "Хөдөлгүүр", ai_text, ai_text, ai_variant),
            ("Урсгал", "Протокол", "RTSP", "Идэвхтэй", "good"),
            (
                "Холболт", "Дэлгүүр",
                st.store_name or "—",
                "Холбогдсон" if st.is_paired else "Холбогдоогүй",
                "good" if st.is_paired else "warn",
            ),
            ("Апп", "Хувилбар", f"v{__version__}", "Одоогийн", "blue"),
            ("Бичлэг", "Хадгалалт", "Local (энэ PC)", "Local", "blue"),
        )
        for i, (group, name, value, state_text, variant) in enumerate(specs):
            r = ctk.CTkFrame(rows, fg_color="transparent")
            r.pack(fill="x")
            if i:
                ctk.CTkFrame(rows, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
                    fill="x", before=r
                )
            ctk.CTkLabel(r, text=group, anchor="w", width=150, text_color=UI_MUTED_FG,
                        font=ctk.CTkFont(size=12)).pack(side="left", pady=8)
            ctk.CTkLabel(r, text=name, anchor="w", width=200,
                        font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkLabel(r, text=value, anchor="w",
                        font=ctk.CTkFont(size=12)).pack(side="left", fill="x", expand=True)
            pill = StatusPill(r, state_text, variant, dot=True)
            pill.pack(side="right")

    # === Camera list rendering ===

    def refresh_cameras(self) -> None:
        # Render the local list immediately (fast), then reconcile with the
        # backend in the background — so a camera deleted on the web disappears
        # here too, and the desktop list always matches the web.
        self._render_camera_list(load_state().cameras)
        self._update_camera_pill()
        self._refresh_statusbar()
        self._reconcile_in_bg()

    def _update_camera_pill(self) -> None:
        """Topbar camera chip: connected/total cameras. Green only when every
        camera has a stream configured (a bare count shouldn't imply health);
        amber when some are still in setup, neutral when none registered."""
        pill = getattr(self, "_pill_cameras", None)
        if pill is None:
            return
        cams = load_state().cameras
        n = len(cams)
        online = sum(1 for c in cams if c.rtsp_url)
        with contextlib.suppress(Exception):
            if not n:
                pill.set("0 камер", "neutral")
            elif online == n:
                pill.set(f"{n} камер", "good")
            else:
                pill.set(f"{online}/{n} камер", "warn")

    def _render_camera_list(self, cameras: list[CameraRecord]) -> None:
        # Keep the full list so the search box can filter without a re-fetch, then
        # paint through the active filter. A backend reconcile drops a stale
        # selection that no longer exists.
        self._all_cameras = list(cameras)
        if self._selected_cam is not None and not any(
            c.matches(self._selected_cam) for c in cameras
        ):
            self._selected_cam = None
            self._render_camera_detail(None)
        self._paint_camera_rows()
        if cameras:
            self._refresh_streaming()

    def _apply_camera_filter(self) -> None:
        """Re-paint rows through the search box (name / IP substring)."""
        self._paint_camera_rows()

    def _paint_camera_rows(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._push_labels = {}  # rows (and their labels) are being recreated
        self._cam_rows = {}
        cameras = getattr(self, "_all_cameras", [])
        query = ""
        entry = getattr(self, "_cam_search", None)
        if entry is not None:
            query = entry.get().strip().lower()
        if query:
            cameras = [
                c for c in cameras
                if query in (c.name or "").lower() or query in (c.ip or "").lower()
            ]
        if not cameras:
            msg = (
                "Хайлтад тохирох камер алга."
                if query
                else "Камер бүртгэгдээгүй байна.\n\n"
                "'🔍 Scan' дарж автоматаар олох, эсвэл '➕ Нэмэх' дарж гараар нэмнэ үү."
            )
            ctk.CTkLabel(
                self.list_frame, text=msg, font=ctk.CTkFont(size=13),
                text_color=UI_MUTED_FG, justify="center",
            ).pack(pady=60)
            self.set_status(f"{len(getattr(self, '_all_cameras', []))} камер бүртгэлтэй")
            return
        for cam in cameras:
            self._render_camera_row(cam)
        self.set_status(f"{len(getattr(self, '_all_cameras', []))} камер бүртгэлтэй")

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
        # Headless edge Stage-1: starts/stops a 24/7 decode+analysis worker per
        # edge_pc camera, independent of any GUI window (founder requirement). A
        # no-op for cloud-tier cameras. Heavy (OpenVINO load) → its own thread.
        threading.Thread(
            target=get_edge_controller().refresh,
            name="edge-refresh",
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

    def _edge_status_pill(self) -> tuple[str, str]:
        """(short label, pill variant) for the AI-engine chips (topbar + sidebar).

        Derived from the same readiness probe as `_edge_status_text()` but trimmed
        to a chip and mapped to a colour band."""
        text = self._edge_status_text()
        if text.startswith("🟢"):
            return "OpenVINO", "good"
        if text.startswith("⚠"):
            return "AI алдаа", "danger"
        return "AI унтарсан", "neutral"

    def _refresh_edge_status(self) -> None:
        """Repaint the AI-engine chips (topbar pill + sidebar mini-status) so a
        live-view runtime failure (or a recovery) shows without a restart."""
        label, variant = self._edge_status_pill()
        for pill in (getattr(self, "_pill_ai", None), getattr(self, "_mini_ai", None)):
            if pill is not None:
                with contextlib.suppress(Exception):
                    pill.set(label, variant)

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
        for path, pill in self._push_labels.items():
            try:
                if not ctrl.push_enabled:
                    pill.set("—", "neutral")
                    continue
                st = status_by_path.get(path)
                if st is None:
                    pill.set("хүлээж", "neutral")
                elif st.get("running"):
                    pill.set("дамжуулж", "good")
                else:
                    pill.set("тасарсан", "danger")
            except Exception:  # noqa: BLE001 — pill may have been destroyed mid-refresh
                continue

    def _render_camera_row(self, cam: CameraRecord) -> None:
        selected = self._selected_cam is not None and cam.matches(self._selected_cam)
        base = UI_SURFACE_2 if selected else UI_SURFACE
        row = ctk.CTkFrame(self.list_frame, fg_color=base, corner_radius=8)
        row.pack(fill="x", pady=2)
        self._configure_grid(row)
        key = cam.uuid or cam.mediamtx_path or cam.ip or cam.name
        self._cam_rows[key] = row

        res = f"{cam.resolution[0]}×{cam.resolution[1]}" if cam.resolution else "—"
        # col 0 — name (bold), col 1 — IP.
        ctk.CTkLabel(
            row, text=cam.name, anchor="w", font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=6, pady=8)
        ctk.CTkLabel(
            row, text=cam.ip or "—", anchor="w", font=ctk.CTkFont(size=12),
            text_color=UI_MUTED_FG,
        ).grid(row=0, column=1, sticky="w", padx=6, pady=8)

        # col 2 — connection status pill (has a stored stream ⇒ configured).
        online = bool(cam.rtsp_url)
        status_pill = StatusPill(
            row, "Online" if online else "Setup", "good" if online else "warn", dot=True
        )
        status_pill.grid(row=0, column=2, sticky="w", padx=6, pady=6)

        # col 3 — AI engine pill (edge tier ⇒ OpenVINO runs on this PC).
        is_edge = getattr(cam, "compute_tier", "") == "edge_pc"
        ai_pill = StatusPill(
            row, "OpenVINO" if is_edge else "Cloud", "blue" if is_edge else "neutral", dot=False
        )
        ai_pill.grid(row=0, column=3, sticky="w", padx=6, pady=6)

        # col 4 — quality (resolution + codec as a hint).
        qual = res if not cam.codec else f"{res} · {cam.codec.upper()}"
        ctk.CTkLabel(
            row, text=qual, anchor="w", font=ctk.CTkFont(size=12), text_color=UI_MUTED_FG,
        ).grid(row=0, column=4, sticky="w", padx=6, pady=8)

        # col 5 — push status pill (cloud topology) — updated by _tick_push_status.
        push_pill = StatusPill(row, "—", "neutral", dot=True)
        push_pill.grid(row=0, column=5, sticky="w", padx=6, pady=6)
        if cam.mediamtx_path:
            self._push_labels[cam.mediamtx_path] = push_pill

        # col 6 — the ⋯ action menu (all row actions live here + the detail panel).
        menu_btn = ctk.CTkButton(
            row, text="⋯", width=30, height=26,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            hover_color=UI_MUTED_HOVER, font=ctk.CTkFont(size=15, weight="bold"),
            command=lambda c=cam, r=row: self._open_camera_menu(c, r),
        )
        menu_btn.grid(row=0, column=6, sticky="e", padx=(2, 8), pady=6)

        # Whole-row click selects the camera (populates the detail panel). The ⋯
        # button keeps its own command (it doesn't propagate to the row handler).
        # `cam` is a stable method arg (not a loop var) so a plain closure is safe.
        _bind_row_click(row, lambda: self._select_camera(cam))
        for child in row.winfo_children():
            if child is not menu_btn:
                with contextlib.suppress(Exception):
                    child.bind("<Button-1>", lambda _e: self._select_camera(cam))

    def _select_camera(self, cam: CameraRecord) -> None:
        """Mark a camera selected: highlight its row + fill the detail panel."""
        self._selected_cam = cam
        for key, row in self._cam_rows.items():
            with contextlib.suppress(Exception):
                is_sel = (cam.uuid or cam.mediamtx_path or cam.ip or cam.name) == key
                row.configure(fg_color=UI_SURFACE_2 if is_sel else UI_SURFACE)
        self._render_camera_detail(cam)

    def _open_camera_menu(self, cam: CameraRecord, anchor: ctk.CTkBaseClass) -> None:
        """Popup the row's ⋯ action menu (Засах / Зон / Холбох / Устгах)."""
        self._select_camera(cam)
        menu = dark_menu(self)
        menu.add_command(label="✎  Засах", command=lambda: self._edit_camera(cam))
        menu.add_command(label="▦  Зон тохируулах", command=lambda: self._edit_zones(cam))
        menu.add_command(label="↻  Дахин холбох", command=lambda: self._reconnect_camera(cam))
        # AI engine tier — edge_pc ⇒ this PC runs YOLO + behaviour and uploads
        # suspicious clips to the cloud VLM; cloud ⇒ central Stage-1 (ADR-0029).
        is_edge = getattr(cam, "compute_tier", "") == "edge_pc"
        menu.add_command(
            label="🧠  Cloud AI руу буцаах" if is_edge else "🧠  Edge AI (энэ PC) асаах",
            command=lambda: self._toggle_edge_mode(cam),
        )
        menu.add_separator()
        menu.add_command(label="🗑  Устгах", command=lambda: self._delete_camera(cam))
        try:
            x = anchor.winfo_rootx() + anchor.winfo_width() - 40
            y = anchor.winfo_rooty() + anchor.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _render_camera_detail(self, cam: CameraRecord | None) -> None:
        """Fill the right-side detail panel for the selected camera (or an
        empty-state hint when nothing is selected). All row actions live here."""
        holder = getattr(self, "_cam_detail", None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        if cam is None:
            ctk.CTkLabel(
                holder, text="Камер сонгоно уу.\n\nЖагсаалтаас мөр дээр дарж\nдэлгэрэнгүйг харна.",
                text_color=UI_MUTED_FG, justify="center", font=ctk.CTkFont(size=12),
            ).pack(expand=True, pady=40)
            return
        ctk.CTkLabel(
            holder, text=cam.name, anchor="w", font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", pady=(0, 8))
        res = f"{cam.resolution[0]}×{cam.resolution[1]}" if cam.resolution else "—"
        is_edge = getattr(cam, "compute_tier", "") == "edge_pc"
        fields: tuple[tuple[str, str], ...] = (
            ("IP", cam.ip or "—"),
            ("Path", cam.mediamtx_path or "—"),
            ("Урсгал", "RTSP холбогдсон" if cam.rtsp_url else "тохируулаагүй"),
            ("Codec", (cam.codec or "—").upper()),
            ("Чанар", res),
            ("AI", "OpenVINO (edge PC)" if is_edge else "Cloud"),
        )
        for k, v in fields:
            r = ctk.CTkFrame(holder, fg_color="transparent")
            r.pack(fill="x", pady=3)
            ctk.CTkLabel(
                r, text=k, anchor="w", width=90, text_color=UI_MUTED_FG,
                font=ctk.CTkFont(size=12),
            ).pack(side="left")
            ctk.CTkLabel(
                r, text=v, anchor="w", font=ctk.CTkFont(size=12), justify="left",
                wraplength=170,
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkFrame(holder, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
            fill="x", pady=10
        )
        ctk.CTkButton(
            holder, text="✎ Засах", height=32,
            fg_color=BRAND_PRIMARY, hover_color=BRAND_PRIMARY_HOVER,
            command=lambda c=cam: self._edit_camera(c),
        ).pack(fill="x", pady=2)
        ctk.CTkButton(
            holder, text="▦ Зон тохируулах", height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=lambda c=cam: self._edit_zones(c),
        ).pack(fill="x", pady=2)
        ctk.CTkButton(
            holder, text="↻ Дахин холбох", height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=lambda c=cam: self._reconnect_camera(c),
        ).pack(fill="x", pady=2)
        ctk.CTkButton(
            holder, text="🗑 Устгах", height=32,
            fg_color="transparent", border_width=1, border_color=UI_DANGER,
            text_color=UI_DANGER, hover_color=UI_MUTED_HOVER,
            command=lambda c=cam: self._delete_camera(c),
        ).pack(fill="x", pady=2)

    def _toggle_edge_mode(self, cam: CameraRecord) -> None:
        """Flip a camera between cloud Stage-1 and edge_pc. In edge_pc mode THIS
        PC runs YOLO + behaviour 24/7 and uploads each suspicious clip to the
        cloud VLM (→ it shows up in the web «Сэжигтэй үйлдэл»). Needs the camera
        to be registered with the backend (a uuid)."""
        if not cam.uuid:
            self.set_status("⚠ Камер cloud-д бүртгэгдээгүй байна — эхлээд холбоно уу")
            return
        to_edge = getattr(cam, "compute_tier", "") != "edge_pc"
        new_tier = "edge_pc" if to_edge else "cloud"

        def work() -> dict[str, Any]:
            from sentry_agent_pc.state import mutate_state

            # 1. Persist the tier on the backend (source of truth; syncs back on
            #    the next discovery poll too).
            BackendClient().agent_update_camera(cam.uuid, compute_tier=new_tier)

            # 2. Reflect it locally NOW so the upload gate + engine react without
            #    waiting for the sync.
            def _apply(s: Any) -> None:
                for c in s.cameras:
                    if c.matches(cam):
                        c.compute_tier = new_tier

            mutate_state(_apply)

            # 3. Start/stop the always-on edge worker to match the new tier.
            try:
                from sentry_agent_pc.edge.controller import get_edge_controller

                get_edge_controller().refresh()
            except Exception:  # noqa: BLE001 — engine reconcile is best-effort
                log.exception("toggle_edge.refresh_failed")
            return {"ok": True, "tier": new_tier}

        def done(result: Any) -> None:
            self.refresh_cameras()
            if isinstance(result, dict) and result.get("ok"):
                if result["tier"] == "edge_pc":
                    self.set_status(
                        "🧠 Edge AI аслаа — сэжигтэй бичлэг үүл рүү автоматаар илгээгдэнэ ✓"
                    )
                else:
                    self.set_status("Cloud AI руу буцаалаа ✓")
            else:
                err = str(result.get("error", ""))[:60] if isinstance(result, dict) else ""
                self.set_status(f"⚠ AI горим солиж чадсангүй — сүлжээгээ шалгана уу {err}")

        self._run_bg(work, done, status="AI горим солиж байна…")

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

    # === «Plan зураг» — in-app read-only preview + «Засах» opens the editor ===

    def _build_plan_page(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        """The «Plan зураг» page: a read-only schematic of the saved store plan
        (left) + a coverage/setup inspector (right). «✏ Засах» opens the full
        editor window (unchanged behaviour)."""
        page = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar = self._page_head(
            page, "Plan зураг",
            "Дэлгүүрийн план ба камерын хамрах талбай. «✏ Засах» дэлгэрэнгүй засварлагч нээнэ.",
        )
        ctk.CTkButton(
            toolbar, text="✏ Засах", width=100, height=32,
            fg_color=BRAND_PRIMARY, hover_color=BRAND_PRIMARY_HOVER,
            command=self.open_floor_plan,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            toolbar, text="↻ Сэргээх", width=100, height=32,
            fg_color="transparent", border_width=1, border_color=UI_BORDER,
            command=self._refresh_plan_preview,
        ).pack(side="right")

        split = ctk.CTkFrame(page, fg_color="transparent")
        split.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        canvas_panel = Panel(split, "Floor plan")
        canvas_panel.pack(side="left", fill="both", expand=True, padx=(0, 12))
        # Darker CAD-like canvas backdrop for the schematic preview.
        holder = ctk.CTkFrame(canvas_panel.body, fg_color="#070A0E", corner_radius=8)
        holder.pack(fill="both", expand=True)
        self._plan_preview = ctk.CTkLabel(
            holder, text="Ачаалж байна…", text_color=UI_MUTED_FG, font=ctk.CTkFont(size=14),
        )
        self._plan_preview.pack(fill="both", expand=True, padx=10, pady=10)
        self._plan_img: Any = None

        inspector = Panel(split, "Inspector")
        inspector.configure(width=320)
        inspector.pack(side="left", fill="y")
        inspector.pack_propagate(False)
        self._plan_inspector = inspector.body
        self._render_plan_inspector(None)
        return page

    def _render_plan_inspector(self, plan: dict[str, Any] | None) -> None:
        """Coverage summary + setup-progress checklist for the loaded plan."""
        holder = getattr(self, "_plan_inspector", None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        walls = (plan or {}).get("walls") or []
        fixtures = (plan or {}).get("fixtures") or []
        cameras = (plan or {}).get("cameras") or []
        n_shelf = sum(1 for f in fixtures if f.get("type") == "shelf")
        n_cash = sum(1 for f in fixtures if f.get("type") == "checkout")
        n_door = sum(1 for f in fixtures if f.get("type") in ("exit", "entrance"))
        n_cam = len(cameras)

        # Setup checklist — honest, derived from what's actually drawn.
        steps = (
            ("Талбай / хана зурсан", bool(walls or fixtures)),
            ("Тавиур / касс байрлуулсан", bool(fixtures)),
            ("Камер байрлуулсан", n_cam > 0),
        )
        done = sum(1 for _, ok in steps if ok)
        ctk.CTkLabel(
            holder, text="Setup progress", anchor="w", font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w")
        bar = ctk.CTkProgressBar(holder, height=8)
        bar.pack(fill="x", pady=(8, 6))
        bar.set(done / len(steps) if steps else 0)
        for text, ok in steps:
            r = ctk.CTkFrame(holder, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(
                r, text="✓" if ok else "•", width=18,
                text_color=UI_SUCCESS if ok else UI_MUTED_FG, font=ctk.CTkFont(size=13, weight="bold"),
            ).pack(side="left")
            ctk.CTkLabel(
                r, text=text, anchor="w", font=ctk.CTkFont(size=12),
                text_color=UI_FG if ok else UI_MUTED_FG, wraplength=250, justify="left",
            ).pack(side="left", fill="x", expand=True)

        ctk.CTkFrame(holder, height=1, fg_color=UI_LINE_SOFT, corner_radius=0).pack(
            fill="x", pady=12
        )
        ctk.CTkLabel(
            holder, text="Coverage", anchor="w", font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(0, 6))
        for label, count, variant in (
            ("Камер", n_cam, "good" if n_cam else "warn"),
            ("Тавиур", n_shelf, "good" if n_shelf else "neutral"),
            ("Касс", n_cash, "good" if n_cash else "neutral"),
            ("Орц / гарц", n_door, "good" if n_door else "neutral"),
        ):
            r = ctk.CTkFrame(holder, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=label, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
            StatusPill(r, str(count), variant, dot=False).pack(side="right")

    def _refresh_plan_preview(self) -> None:
        """Fetch the plan off-thread and re-render the preview (called on page show
        and by «🔄 Сэргээх» after editing)."""
        if getattr(self, "_plan_preview", None) is None:
            return
        self._plan_preview.configure(text="Ачаалж байна…", image="")
        threading.Thread(target=self._fetch_plan, daemon=True).start()

    def _fetch_plan(self) -> None:
        plan: dict[str, Any] | None = None
        err: str | None = None
        try:
            plan = BackendClient().agent_get_floor_plan()
        except Exception as e:  # noqa: BLE001 — the preview must never crash the app
            err = str(e)
        self.after(0, lambda: self._apply_plan_preview(plan, err))

    def _apply_plan_preview(self, plan: dict[str, Any] | None, err: str | None) -> None:
        if getattr(self, "_plan_preview", None) is None:
            return
        self._render_plan_inspector(plan)
        if err is not None or plan is None:
            self._plan_preview.configure(text=f"Планыг ачаалж чадсангүй.\n{err or ''}", image="")
            self._plan_img = None
            return
        # Render at a modest size so the preview fits beside the fixed-width
        # inspector in the split (a 900px image would squeeze the inspector).
        img = render_plan_image(plan, size=(560, 410))
        if img is None:
            self._plan_preview.configure(
                text="Одоогоор зурсан план алга.\n«✏ Засах» дарж дэлгүүрээ зураарай.",
                image="",
            )
            self._plan_img = None
            return
        self._plan_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self._plan_preview.configure(text="", image=self._plan_img)

    def open_floor_plan(self) -> None:
        """Open the «Plan зураг» floor-plan editor (docs/30) in a webview window.

        Spawned as a separate process (pywebview owns its own loop) — the same
        pattern as the live view. Needs a paired agent: the editor loads/saves the
        store plan through the backend."""
        if not self._require_paired():
            return
        open_floor_plan()
        self.set_status("Plan зураг нээгдэж байна…")

    # === Window / tray lifecycle ===

    def _set_window_icon(self) -> None:
        """Set the title-bar / taskbar icon (best-effort; Windows .ico)."""
        try:
            ico = resources.icon_ico()
            if ico.exists():
                # Passing this as ``default=`` does not reliably update the
                # already-created main window; PyInstaller's embedded icon can
                # remain visible. Set this window's icon directly.
                self.iconbitmap(str(ico))
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
        self._teardown_live()  # stop the live grid's RTSP readers, if open
        for after_id in self._after_ids.values():
            with contextlib.suppress(tk.TclError):
                self.after_cancel(after_id)
        self._after_ids.clear()
        try:
            get_stream_controller().stop()
        except Exception as e:  # noqa: BLE001
            log.debug("quit.stop_streams_failed", error=str(e))
        try:
            get_edge_controller().stop()
        except Exception as e:  # noqa: BLE001
            log.debug("quit.stop_edge_failed", error=str(e))
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
            self._pill_backend.set("Холбогдоогүй", "warn")
            return
        store = state.store_name or "дэлгүүр"

        def work() -> dict[str, Any]:
            try:
                # Attach push-relay state so the cloud can show WHY a camera's push
                # is down (ffmpeg last_error) without RDP-ing into this store PC.
                # Only when push is enabled — pull/on-LAN stores have nothing to push.
                push_status: list[dict[str, Any]] | None = None
                hls_tunnel_base: str | None = None
                try:
                    ctrl = get_stream_controller()
                    if ctrl.push_enabled:
                        push_status = ctrl.status()
                    # The public HLS tunnel base (if cloudflared is up) so the
                    # backend can point /live straight at this agent.
                    hls_tunnel_base = ctrl.tunnel_url()
                except Exception:  # noqa: BLE001 — never let telemetry block liveness
                    push_status = None
                BackendClient().heartbeat(push_status=push_status, hls_tunnel_base=hls_tunnel_base)
                return {"ok": True}
            except BackendError as e:
                return {"ok": False, "error": str(e)}

        def done(result: dict[str, Any]) -> None:
            # The heartbeat tick runs this on a thread; if the window was quit
            # while it was in flight, configuring backend_label raises TclError.
            if self._closing or not self.winfo_exists():
                return
            if result.get("ok"):
                self._pill_backend.set(store, "good")
            else:
                self._pill_backend.set(f"{store} — тасарсан", "danger")

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
            fg_color=BRAND_PRIMARY,
            hover_color=BRAND_PRIMARY_HOVER,
            command=self._pair,
        )
        self.connect_btn.pack(side="right")
        if state.is_paired:
            ctk.CTkButton(
                btn_row,
                text="Салгах",
                fg_color="transparent",
                border_width=1,
                text_color=UI_DANGER,
                border_color=UI_DANGER,
                command=self._unpair,
            ).pack(side="left")

        self.status_lbl = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=UI_MUTED_FG,
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
                text_color=UI_SUCCESS,
                anchor="w",
            ).pack(fill="x", padx=20, pady=(2, 8))

        ctk.CTkLabel(
            body,
            text="Веб апп → Дэлгүүр → 'Компьютер холбох' дарж 6 оронтой код аваад доор оруулна уу.",
            font=ctk.CTkFont(size=12),
            text_color=UI_MUTED_FG,
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
            text_color=UI_MUTED_FG,
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
            text_color=UI_MUTED_FG,
        ).pack(fill="x", padx=20, pady=(8, 0))
        self.url_entry = ctk.CTkEntry(self._adv, placeholder_text=DEFAULT_BACKEND_URL)
        self.url_entry.pack(fill="x", padx=20, pady=(2, 8))
        self.url_entry.insert(0, cfg.get("BACKEND_URL") or DEFAULT_BACKEND_URL)
        ctk.CTkLabel(
            self._adv,
            text="Веб хаяг (Шууд харах цонхонд ачаална):",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=UI_MUTED_FG,
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
                text_color=UI_DANGER,
            )
            return
        self.connect_btn.configure(state="disabled")
        self.status_lbl.configure(text="Холбож байна…", text_color=UI_MUTED_FG)
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
                text_color=UI_SUCCESS,
            )
            self.on_saved()
            self.after(1200, self.destroy)
        else:
            self.status_lbl.configure(
                text=f"❌ {result.get('error', 'алдаа')[:120]}",
                text_color=UI_DANGER,
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


def set_dpi_awareness() -> None:
    """Claim Per-Monitor-v2 DPI awareness so Windows renders the title-bar and
    taskbar icon (and the whole UI) crisply at the monitor's scale.

    CustomTkinter only sets Per-Monitor *v1* (`SetProcessDpiAwareness(2)`),
    whose non-client area — the title bar and the icon that lives in it — is NOT
    rescaled by Windows. On a scaled display (125/150/175%) the icon is then
    bitmap-stretched and looks blurry, both in the title bar and on the taskbar.
    v2 rescales the non-client area, which fixes it.

    We set awareness ourselves and neutralise CTk's own attempt (it is not
    exception-guarded and would raise E_ACCESSDENIED once awareness is already
    set), while leaving CTk's widget-scaling math untouched so the UI still
    scales. Idempotent and a no-op off Windows. Must run before the first Tk
    window is created.
    """
    import sys

    if not sys.platform.startswith("win"):
        return
    import ctypes

    # getattr avoids a platform-dependent mypy ignore: ctypes.windll only exists
    # on Windows, but this branch is only reached there (mirrors the helper above).
    windll = getattr(ctypes, "windll")  # noqa: B009

    # Stop CTk from calling SetProcessDpiAwareness() itself — that call lives in
    # ScalingTracker.activate_high_dpi_awareness() and is unguarded, so it would
    # crash once we've claimed awareness below. Replacing only this method (not
    # the public deactivate_automatic_dpi_awareness toggle) keeps CTk's
    # per-monitor widget-scaling math intact, so the UI is not left tiny.
    with contextlib.suppress(Exception):
        from customtkinter.windows.widgets.scaling.scaling_tracker import (
            ScalingTracker,
        )

        ScalingTracker.activate_high_dpi_awareness = classmethod(lambda _cls: None)

    # Prefer Per-Monitor-v2 (Win10 1703+); fall back to v1, then system-aware.
    # The v2 context is the pseudo-handle -4 passed as a void*.
    with contextlib.suppress(Exception):
        if windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    with contextlib.suppress(Exception):
        windll.shcore.SetProcessDpiAwareness(2)
        return
    with contextlib.suppress(Exception):
        windll.user32.SetProcessDPIAware()


def run(minimized: bool = False) -> None:
    """GUI entry point. `minimized=True` (auto-start) launches hidden in the tray."""
    set_dpi_awareness()  # before the first Tk window → crisp title-bar/taskbar icon
    set_app_user_model_id()  # before the first Tk window → taskbar icon binds
    app = AgentApp()
    if minimized:
        app.after(0, app.hide_to_tray)
    app.mainloop()
