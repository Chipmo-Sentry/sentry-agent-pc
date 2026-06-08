"""Offline LAN live view — decode each camera's RTSP DIRECTLY and show a grid.

This is the no-internet path: it talks straight to the cameras on the LAN (the
agent already stored each camera's RTSP URL + credentials), decodes with
OpenCV/ffmpeg, and renders the frames natively in a Tk window. No MediaMTX, no
web page, no login — it works even with the internet down. (When online, the
cloud sentry-ai pipeline + web /live with AI overlay run as well.)

Threading model: one daemon reader thread per camera owns its VideoCapture and
keeps only the LATEST frame (downscaled to the tile box, so the UI thread does
almost no work). A single Tk `after` tick paints the latest frames at ~15 fps —
all PhotoImage creation stays on the main thread (Tk is not thread-safe).
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

_TILE_W = 640
_TILE_H = 360
_TARGET_FPS = 15
_MAX_COLS = 2


def grid_dims(n: int, max_cols: int = _MAX_COLS) -> tuple[int, int]:
    """(cols, rows) for n tiles — pure, so it's unit-testable."""
    if n <= 0:
        return (1, 1)
    cols = min(n, max_cols)
    rows = (n + cols - 1) // cols
    return (cols, rows)


class _CameraReader(threading.Thread):
    """Owns one camera's VideoCapture; exposes the latest frame (BGR, tile-sized)."""

    def __init__(self, name: str, rtsp_url: str) -> None:
        super().__init__(name=f"camreader-{name}", daemon=True)
        self.cam_name = name
        self.rtsp_url = rtsp_url
        self._latest: Image.Image | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.status = "Холбож байна…"

    def latest(self) -> Image.Image | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import cv2

        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            with contextlib.suppress(Exception):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # low latency; some backends ignore
            if not cap.isOpened():
                cap.release()
                self.status = "Холбогдсонгүй — дахин оролдож байна…"
                if self._stop.wait(2.0):
                    break
                continue

            self.status = "Шууд"
            fails = 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 15:
                        break  # stream dropped → reconnect
                    continue
                fails = 0
                # Downscale to the tile box HERE (off the UI thread); convert
                # BGR→RGB and store a ready-to-paint PIL image.
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = _fit_letterbox(Image.fromarray(rgb), _TILE_W, _TILE_H)
                with self._lock:
                    self._latest = img
            cap.release()
            if not self._stop.is_set():
                self.status = "Холболт тасарсан — дахин холбож байна…"
                if self._stop.wait(2.0):
                    break


def _fit_letterbox(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resize preserving aspect ratio, centered on a black box (no distortion)."""
    iw, ih = img.size
    scale = min(box_w / iw, box_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh))
    canvas = Image.new("RGB", (box_w, box_h), (0, 0, 0))
    canvas.paste(resized, ((box_w - nw) // 2, (box_h - nh) // 2))
    return canvas


class LocalLiveView(ctk.CTkToplevel):
    """A grid window that plays the store's cameras straight off the LAN."""

    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master)
        self.title("Chipmo Sentry — Шууд харах (офлайн)")
        self.transient(master)

        cams = [
            c for c in load_state().cameras if c.rtsp_url
        ]
        cols, rows = grid_dims(len(cams))
        width = cols * (_TILE_W + 12) + 12
        height = rows * (_TILE_H + 34) + 60
        widgets.setup_dialog(self, width, height, min_width=480, min_height=360)

        header = ctk.CTkLabel(
            self,
            text=(
                f"{len(cams)} камер · LAN-аас шууд (интернэт шаардахгүй)"
                if cams
                else "Камер алга"
            ),
            font=ctk.CTkFont(size=12), text_color="gray60", anchor="w",
        )
        header.pack(fill="x", padx=14, pady=(10, 4))

        if not cams:
            ctk.CTkLabel(
                self,
                text="RTSP-тэй камер бүртгэгдээгүй байна.\n'Камер хайх' эсвэл "
                "'Камер нэмэх'-ээр камераа нэмнэ үү.",
                font=ctk.CTkFont(size=13), text_color="gray60", justify="center",
            ).pack(expand=True)
            return

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=8, pady=8)

        self._readers: list[_CameraReader] = []
        self._tiles: list[tuple[_CameraReader, tk.Label]] = []
        for i, cam in enumerate(cams):
            r, c = divmod(i, cols)
            tile = ctk.CTkFrame(grid, fg_color="gray17", corner_radius=8)
            tile.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
            ctk.CTkLabel(
                tile, text=cam.name, font=ctk.CTkFont(size=12, weight="bold"),
                anchor="w",
            ).pack(fill="x", padx=8, pady=(6, 2))
            video = tk.Label(tile, bg="black", width=_TILE_W, height=_TILE_H)
            video.pack(padx=8, pady=(0, 8))
            reader = _CameraReader(cam.name, cam.rtsp_url)
            reader.start()
            self._readers.append(reader)
            self._tiles.append((reader, video))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._closed = False
        self._tick()

    def _tick(self) -> None:
        if self._closed:
            return
        for reader, video in self._tiles:
            img = reader.latest()
            if img is not None:
                photo = ImageTk.PhotoImage(img)
                video.configure(image=photo, text="")
                video.image = photo  # type: ignore[attr-defined]  # keep ref (Tk GC)
            else:
                video.configure(text=reader.status, fg="gray70", compound="center")
        self.after(int(1000 / _TARGET_FPS), self._tick)

    def _on_close(self) -> None:
        self._closed = True
        for reader in self._readers:
            reader.stop()
        self.destroy()


def open_local_view(master: ctk.CTk) -> LocalLiveView:
    """Open (or focus) the offline LAN grid window."""
    return LocalLiveView(master)
