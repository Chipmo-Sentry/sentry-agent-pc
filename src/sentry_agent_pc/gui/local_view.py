"""Offline LAN live view — decode each camera's RTSP DIRECTLY and show a grid.

This is the no-internet path: it talks straight to the cameras on the LAN (the
agent already stored each camera's RTSP URL + credentials), decodes with
OpenCV/ffmpeg, and renders the frames natively in a Tk window. No MediaMTX, no
web page, no login — it works even with the internet down. (When online, the
cloud sentry-ai pipeline + web /live with AI overlay run as well.)

Threading model: one daemon reader thread per camera owns its VideoCapture and
keeps only the LATEST frame (downscaled to the current tile box, so the UI
thread does almost no work). A single Tk `after` tick paints the latest frames
at ~15 fps — all PhotoImage creation stays on the main thread (Tk is not
thread-safe).

UI: a responsive dark grid. Tiles resize with the window; each shows a coloured
status badge (Шууд / Холбож байна / Алдаа) so a dead camera is obvious instead
of a silent black box. Double-click a tile to focus it full-window; double-click
again to return to the grid.
"""

from __future__ import annotations

import contextlib
import os
import threading
import tkinter as tk

# RTSP over TCP is far more reliable than the UDP default on busy LANs. Must be
# set before the first cv2 VideoCapture is created.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import customtkinter as ctk  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

from sentry_agent_pc.gui import widgets  # noqa: E402
from sentry_agent_pc.logging_setup import get_logger  # noqa: E402
from sentry_agent_pc.state import load_state  # noqa: E402

log = get_logger("sentry_agent_pc.gui.local_view")

_TARGET_FPS = 15
_MIN_TILE_W = 320  # below this we drop a column
_MAX_COLS = 3

# Status → (badge text, badge colour, dot colour)
_CONNECTING = "connecting"
_LIVE = "live"
_RECONNECTING = "reconnecting"
_ERROR = "error"

_STATUS_STYLE = {
    _CONNECTING: ("Холбож байна…", "#3B3320", "#E0A82E"),
    _LIVE: ("Шууд", "#13301B", "#3DD56D"),
    _RECONNECTING: ("Дахин холбож байна…", "#3B3320", "#E0A82E"),
    _ERROR: ("Алдаа", "#3A1C1C", "#E5484D"),
}


def grid_dims(n: int, max_cols: int = 2) -> tuple[int, int]:
    """(cols, rows) for n tiles — pure, so it's unit-testable.

    Kept for the existing layout unit tests; the live window now reflows
    responsively via :func:`_cols_for_width`.
    """
    if n <= 0:
        return (1, 1)
    cols = min(n, max_cols)
    rows = (n + cols - 1) // cols
    return (cols, rows)


def _cols_for_width(n: int, avail_w: int) -> int:
    """How many columns fit in avail_w px, capped by camera count and _MAX_COLS."""
    if n <= 1:
        return 1
    fit = max(1, avail_w // _MIN_TILE_W)
    return max(1, min(n, _MAX_COLS, fit))


class _CameraReader(threading.Thread):
    """Owns one camera's VideoCapture; exposes the latest frame + a status.

    The target tile box is settable from the UI thread (on window resize) so the
    downscale always matches what's painted — no wasted pixels, no blur.
    """

    def __init__(self, name: str, rtsp_url: str) -> None:
        super().__init__(name=f"camreader-{name}", daemon=True)
        self.cam_name = name
        self.rtsp_url = rtsp_url
        self._latest: Image.Image | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._box = (640, 360)
        self.status = _CONNECTING
        self.detail = "Холбож байна…"

    def latest(self) -> Image.Image | None:
        with self._lock:
            return self._latest

    def set_box(self, w: int, h: int) -> None:
        with self._lock:
            self._box = (max(80, int(w)), max(45, int(h)))

    def _get_box(self) -> tuple[int, int]:
        with self._lock:
            return self._box

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            import cv2
        except Exception as e:  # noqa: BLE001 — surface the import failure in the UI
            self.status = _ERROR
            self.detail = "OpenCV ачаалагдсангүй (cv2)"
            log.error("local_view.cv2_import_failed", error=str(e))
            return

        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            with contextlib.suppress(Exception):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # low latency; some backends ignore
            if not cap.isOpened():
                cap.release()
                self.status = _ERROR
                self.detail = "Холбогдсонгүй — дахин оролдож байна…"
                # Non-fatal: a console-encoding error must never kill the reader
                # thread (the camera name is Cyrillic; stdout may be cp1252).
                # Log the host only — never the full URL (it carries credentials).
                with contextlib.suppress(Exception):
                    log.warning("local_view.open_failed", host=_redact_host(self.rtsp_url))
                if self._stop.wait(2.0):
                    break
                continue

            self.status = _LIVE
            self.detail = "Шууд"
            fails = 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 15:
                        break  # stream dropped → reconnect
                    continue
                fails = 0
                box_w, box_h = self._get_box()
                # Downscale to the tile box HERE (off the UI thread); convert
                # BGR→RGB and store a ready-to-paint PIL image.
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = _fit_letterbox(Image.fromarray(rgb), box_w, box_h)
                with self._lock:
                    self._latest = img
            cap.release()
            if not self._stop.is_set():
                self.status = _RECONNECTING
                self.detail = "Холболт тасарсан — дахин холбож байна…"
                if self._stop.wait(2.0):
                    break


def _redact_host(rtsp_url: str) -> str:
    """Host[:port] from an RTSP URL, with any user:pass@ credentials stripped."""
    after_scheme = rtsp_url.split("://", 1)[-1]
    after_creds = after_scheme.split("@", 1)[-1]  # drop user:pass@ if present
    return after_creds.split("/", 1)[0]


def _fit_letterbox(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resize preserving aspect ratio, centered on a black box (no distortion)."""
    iw, ih = img.size
    scale = min(box_w / iw, box_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh))
    canvas = Image.new("RGB", (box_w, box_h), (16, 16, 18))
    canvas.paste(resized, ((box_w - nw) // 2, (box_h - nh) // 2))
    return canvas


class _Tile(ctk.CTkFrame):
    """One camera cell: title row with a status badge + a fixed-pixel video area.

    The video area is a tk.Label inside a frame with pack_propagate(False), so it
    stays exactly the requested PIXEL size whether or not a frame has arrived —
    this is the fix for the old bug where width=640 was read as 640 *characters*
    and blew the tile up to fill the screen.
    """

    def __init__(self, master: ctk.CTkBaseClass, reader: _CameraReader) -> None:
        super().__init__(master, fg_color="#1A1A1C", corner_radius=10)
        self.reader = reader
        self._vid_w = 640
        self._vid_h = 360

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(
            bar, text=reader.cam_name, font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", text_color="gray85",
        ).pack(side="left")

        self._badge = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=11, weight="bold"),
            corner_radius=6, fg_color="#3B3320", text_color="gray90",
            padx=8, pady=2,
        )
        self._badge.pack(side="right")

        # Fixed-pixel container → the video Label never resizes the tile by text.
        self._holder = ctk.CTkFrame(
            self, fg_color="#0E0E10", corner_radius=8,
            width=self._vid_w, height=self._vid_h,
        )
        self._holder.pack(padx=10, pady=(0, 10))
        self._holder.pack_propagate(False)

        self._video = tk.Label(
            self._holder, bg="#0E0E10", bd=0, highlightthickness=0,
            fg="gray60", font=("Segoe UI", 12),
        )
        self._video.pack(fill="both", expand=True)

    def set_video_size(self, w: int, h: int) -> None:
        w, h = max(80, int(w)), max(45, int(h))
        if (w, h) == (self._vid_w, self._vid_h):
            return
        self._vid_w, self._vid_h = w, h
        self._holder.configure(width=w, height=h)
        self.reader.set_box(w, h)

    def paint(self) -> None:
        text, fg, dot = _STATUS_STYLE.get(self.reader.status, _STATUS_STYLE[_CONNECTING])
        self._badge.configure(text=f"●  {text}", fg_color=fg, text_color=dot)

        img = self.reader.latest()
        if img is not None:
            photo = ImageTk.PhotoImage(img)
            self._video.configure(image=photo, text="")
            self._video.image = photo  # type: ignore[attr-defined]  # keep ref (Tk GC)
        else:
            self._video.configure(image="", text=self.reader.detail, compound="center")
            self._video.image = None  # type: ignore[attr-defined]


class LocalLiveView(ctk.CTkToplevel):
    """A responsive grid window that plays the store's cameras straight off the LAN."""

    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master)
        self.title("Chipmo Sentry — Шууд харах (офлайн)")
        self.configure(fg_color="#0B0B0D")
        self.transient(master)

        cams = [c for c in load_state().cameras if c.rtsp_url]
        widgets.setup_dialog(self, 1180, 760, min_width=560, min_height=420)

        store = load_state().store_name
        title = ctk.CTkFrame(self, fg_color="transparent")
        title.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(
            title, text="Шууд харах", font=ctk.CTkFont(size=20, weight="bold"),
            text_color="gray95", anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            title,
            text=(
                f"{len(cams)} камер · LAN-аас шууд · интернэт шаардахгүй"
                + (f" · {store}" if store else "")
                if cams else "Камер алга"
            ),
            font=ctk.CTkFont(size=12), text_color="gray55", anchor="w",
        ).pack(side="left", padx=12, pady=(6, 0))

        if not cams:
            self._empty()
            return

        # Scrollable so any number of cameras works without clipping.
        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.pack(fill="both", expand=True, padx=12, pady=12)

        self._readers: list[_CameraReader] = []
        self._tiles: list[_Tile] = []
        for cam in cams:
            reader = _CameraReader(cam.name, cam.rtsp_url)
            reader.start()
            self._readers.append(reader)
            tile = _Tile(self._scroll, reader)
            tile.bind("<Double-Button-1>", lambda _e, t=tile: self._toggle_focus(t))
            self._tiles.append(tile)

        self._focused: _Tile | None = None
        self._cols = 0
        self._last_w = 0
        self._closed = False
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._scroll.bind("<Configure>", self._on_resize)
        self.after(50, self._relayout)
        self._tick()

    def _empty(self) -> None:
        ctk.CTkLabel(
            self,
            text="RTSP-тэй камер бүртгэгдээгүй байна.\n"
            "'Камер хайх (Scan)' эсвэл 'Камер нэмэх'-ээр камераа нэмнэ үү.",
            font=ctk.CTkFont(size=14), text_color="gray55", justify="center",
        ).pack(expand=True)

    # ----- layout -------------------------------------------------------------

    def _on_resize(self, _event: object) -> None:
        # Debounce: only relayout when the available width actually changed.
        w = self._scroll.winfo_width()
        if abs(w - self._last_w) < 24:
            return
        self._last_w = w
        self._relayout()

    def _relayout(self) -> None:
        if self._closed or not self._tiles:
            return
        avail = max(200, self._scroll.winfo_width() - 8)

        if self._focused is not None:
            for t in self._tiles:
                t.grid_forget()
            self._scroll.grid_columnconfigure(0, weight=1)
            self._focused.grid(row=0, column=0, sticky="n", padx=6, pady=6)
            w = avail - 20
            self._focused.set_video_size(w, int(w * 9 / 16))
            return

        cols = _cols_for_width(len(self._tiles), avail)
        # gap budget: 12px between tiles + tile inner padding (~20px each side)
        tile_w = (avail - (cols - 1) * 12) // cols
        vid_w = max(_MIN_TILE_W - 40, tile_w - 20)
        vid_h = int(vid_w * 9 / 16)

        for c in range(_MAX_COLS):
            self._scroll.grid_columnconfigure(c, weight=1 if c < cols else 0)

        for i, t in enumerate(self._tiles):
            r, c = divmod(i, cols)
            t.grid(row=r, column=c, padx=6, pady=6, sticky="n")
            t.set_video_size(vid_w, vid_h)
        self._cols = cols

    def _toggle_focus(self, tile: _Tile) -> None:
        self._focused = None if self._focused is tile else tile
        self._last_w = 0  # force a relayout
        self._relayout()

    # ----- paint loop ---------------------------------------------------------

    def _tick(self) -> None:
        if self._closed:
            return
        for tile in self._tiles:
            with contextlib.suppress(Exception):
                tile.paint()
        self.after(int(1000 / _TARGET_FPS), self._tick)

    def _on_close(self) -> None:
        self._closed = True
        for reader in self._readers:
            reader.stop()
        self.destroy()


def open_local_view(master: ctk.CTk) -> LocalLiveView:
    """Open (or focus) the offline LAN grid window."""
    return LocalLiveView(master)
