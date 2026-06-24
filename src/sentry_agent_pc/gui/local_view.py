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
import time
import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import numpy as np

    from sentry_agent_pc.edge.pipeline import EdgePipeline
    from sentry_agent_pc.edge.recorder import ClipRecord, ClipStore, EdgeClipRecorder

# One shared clip store across all camera readers — a single writer (its lock
# serialises) so concurrent per-camera recorders never corrupt the index JSON.
_clip_store: ClipStore | None = None
_clip_store_lock = threading.Lock()


def _shared_clip_store() -> ClipStore:
    global _clip_store
    with _clip_store_lock:
        if _clip_store is None:
            from sentry_agent_pc.edge.recorder import ClipStore
            from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR

            _clip_store = ClipStore(DEFAULT_CONFIG_DIR / "edge" / "clips.json")
        return _clip_store


def _find_camera(cam_name: str) -> CameraRecord | None:
    """The local registry record for a locally-viewed camera name, or None.

    Carries the backend uuid + compute_tier the edge clip upload gate needs."""
    for c in load_state().cameras:
        if c.name == cam_name:
            return c
    return None


# RTSP over TCP is far more reliable than the UDP default on busy LANs. Must be
# set before the first cv2 VideoCapture is created.
#
# The extra flags fight DELAY, the other half of the "vendor app is ahead of us"
# complaint: FFmpeg otherwise builds a demux/jitter buffer that drifts seconds
# behind realtime. `nobuffer`+`low_delay` decode as soon as data arrives;
# `reorder_queue_size;0` is safe because TCP already delivers RTP in order; and
# `max_delay;500000` caps any residual buffering at 0.5 s. Net effect: the grid
# tracks realtime like the camera's own software instead of lagging it.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|reorder_queue_size;0|max_delay;500000",
)

import customtkinter as ctk  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

from sentry_agent_pc.gui import widgets  # noqa: E402
from sentry_agent_pc.logging_setup import get_logger  # noqa: E402
from sentry_agent_pc.state import CameraRecord, load_state  # noqa: E402

log = get_logger("sentry_agent_pc.gui.local_view")

# Per-camera edge-AI runtime errors, surfaced to the main-app sidebar badge.
# Reader threads (separate live-view window) record here keyed by camera; the
# sidebar polls it on its periodic refresh, and a camera that recovers clears
# ITS OWN entry (self-healing). Keyed by camera so one camera recovering can't
# wipe another camera's live error. Without this, every edge build/infer failure
# was silent (no-overlay only).
_edge_err_lock = threading.Lock()
_edge_errors: dict[str, str] = {}


def record_edge_error(camera: str, message: str) -> None:
    """Record an edge-AI failure for a camera (thread-safe; called off-UI)."""
    with _edge_err_lock:
        _edge_errors[camera] = message


def last_edge_error() -> str | None:
    """Any one active edge error (for the single-line sidebar badge), or None."""
    with _edge_err_lock:
        return next(iter(_edge_errors.values()), None)


def clear_edge_error(camera: str) -> None:
    """Drop a camera's recorded error once it recovers."""
    with _edge_err_lock:
        _edge_errors.pop(camera, None)


# 12 fps reads as smooth-enough live monitoring while still throttling UI-thread
# PhotoImage churn. We can afford more than the old 8 now that hardware decode
# (below) takes the per-frame decode cost off the CPU.
_TARGET_FPS = 12
# Hardware-accelerated decode (iGPU/GPU via d3d11va/dxva2/qsv etc). This is the
# core fix for BOTH symptoms vs the vendor app: it moves decode off the CPU, so
# several cameras + the ffmpeg push relays no longer saturate the box. When the
# box is saturated the software decoder falls behind the stream bitrate and drops
# packets mid-GOP — exactly the macroblock smearing ("сариналт") the user sees,
# regardless of codec. OpenCV silently uses software when HW is unsupported, so
# this is safe with zero config on every store PC. The env override exists ONLY
# for dev A/B testing — end users download, detect cameras, and never touch it.
_HWACCEL_ENABLED = os.environ.get("SENTRY_LOCAL_VIEW_HWACCEL", "1") not in ("0", "false", "False")
_MIN_TILE_W = 320  # below this we drop a column
# How long to wait for the FIRST decodable frame before rejecting a candidate URL.
# Generous on purpose: H.265 cameras (e.g. Skyworth) only emit a decodable frame at
# the next keyframe, which on a long-GOP stream — and under concurrent decode load —
# can be 5-10 s after open. The old 4 s budget timed out a perfectly good HEVC stream
# and the tile fell to "Холбогдсонгүй". Better to wait than to wrongly give up.
_PRIME_TIMEOUT_SEC = 12.0
# Stagger camera connects so N VideoCapture opens don't hammer the LAN/cameras at
# once (a burst of simultaneous RTSP sessions is what triggers reconnect storms).
_CONNECT_STAGGER_SEC = 0.35
_MAX_COLS = 3
# HTTP snapshot fallback poll interval (~1.4 fps). Choppy vs RTSP, but it's the
# "browser can see it, so can we" path for cameras with no usable RTSP stream.
_SNAPSHOT_INTERVAL_SEC = 0.7

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


def _redact_host(rtsp_url: str) -> str:
    """Host[:port] from an RTSP URL, with any user:pass@ credentials stripped."""
    after_scheme = rtsp_url.split("://", 1)[-1]
    after_creds = after_scheme.split("@", 1)[-1]  # drop user:pass@ if present
    return after_creds.split("/", 1)[0]


# Main-stream path → low-res sub-stream path, by brand convention. The live grid
# only needs ~640px tiles, so decoding a 4–5 MP MAIN stream and shrinking it is
# pure waste — on a PC also running the AI workers it saturates the CPU and the
# whole window stutters. We pull the SUB stream instead (~16× fewer pixels) and
# fall back to the main URL if the camera has no sub path.
_SUBSTREAM_RULES: tuple[tuple[str, str], ...] = (
    ("/Streaming/Channels/101", "/Streaming/Channels/102"),  # Hikvision main→sub
    ("/Streaming/Channels/1", "/Streaming/Channels/2"),
    ("/stream1", "/stream2"),  # UNV / Skyworth / generic
    ("subtype=0", "subtype=1"),  # Dahua
    ("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
    ("/h264Preview_01_main", "/h264Preview_01_sub"),  # Reolink
    ("/live/main", "/live/sub"),
    ("/ch1/main", "/ch1/sub"),
    ("/main/av_stream", "/sub/av_stream"),
    ("/videoMain", "/videoSub"),
    ("/media/video1", "/media/video2"),  # Uniview / generic ONVIF
    ("/media/video0", "/media/video1"),
    ("/video1", "/video2"),
)


def _substream_url(main_url: str) -> str | None:
    """Best-guess low-res sub-stream URL for a known brand path, else None."""
    for main, sub in _SUBSTREAM_RULES:
        if main in main_url:
            return main_url.replace(main, sub, 1)
    return None


def _candidate_urls(main_url: str) -> list[str]:
    """URLs to try in priority order: sub-stream first (light), main as fallback."""
    sub = _substream_url(main_url)
    return [sub, main_url] if sub else [main_url]


def _reader_urls(main_url: str, local_url: str | None) -> list[str]:
    """URLs for one camera, local fan-out first when available.

    When the agent's local MediaMTX is serving this camera, read from the
    loopback path FIRST: that shares the single pull the push relay already
    holds, so the camera isn't hit by a second session. The direct sub/main URLs
    stay as fallbacks for when the hub is down (offline mode, hub crash) — so the
    offline grid keeps working exactly as before with no hub.
    """
    direct = _candidate_urls(main_url)
    return [local_url, *direct] if local_url else direct


class _CameraReader(threading.Thread):
    """Owns one camera's VideoCapture; exposes the latest frame + a status.

    Tries each candidate URL in order (sub-stream first, main as fallback) and
    PINS the one that works, so a reconnect goes straight back to the good path.
    The target tile box is settable from the UI thread (on window resize) so the
    downscale always matches what's painted — no wasted pixels, no blur.
    """

    def __init__(
        self,
        name: str,
        urls: list[str],
        start_delay: float = 0.0,
        *,
        snapshot_urls: list[str] | None = None,
        snap_user: str | None = None,
        snap_pass: str | None = None,
    ) -> None:
        super().__init__(name=f"camreader-{name}", daemon=True)
        self.cam_name = name
        self.urls = urls  # priority order: sub-stream first, main last
        # Browser-style HTTP snapshot fallback (tried only when all RTSP fail).
        self._snapshot_urls = snapshot_urls or []
        self._snap_user = snap_user
        self._snap_pass = snap_pass
        self._snap_pin: int | None = None
        self._start_delay = start_delay
        self._latest: Image.Image | None = None
        self._seq = 0  # bumped on every new frame; lets the UI skip idle repaints
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        # _active gated: cleared = paused (hold no VideoCapture, ~0 CPU). Used so a
        # focused tile doesn't keep the other cameras decoding in the background.
        self._active = threading.Event()
        self._active.set()
        self._box = (640, 360)
        self._pin: int | None = None  # index of the URL that's working
        self._hw_logged = -2.0  # last logged hwaccel value; log the path once per change
        # Edge Stage-1 overlay — runs on a SEPARATE worker thread so YOLO never
        # blocks the decode loop (decode stays a tight, low-latency read). Decode
        # publishes the newest raw frame into a depth-1 slot; the worker pulls the
        # latest, runs detect+overlay on a tile-sized copy, and produces the shown
        # image. Decode shows raw until the worker is ready / if edge is off/failed.
        self._edge_pipe: EdgePipeline | None = None
        self._edge_on = False  # decided lazily from settings on the first frame
        self._edge_decided = False
        self._edge_ready = False  # worker has produced an annotated frame
        self._edge_failed = False  # build/inference gave up → decode shows raw
        self._edge_err: str | None = None
        self._edge_worker: threading.Thread | None = None
        self._edge_recorder: EdgeClipRecorder | None = None
        self._raw_frame: object | None = None
        self._raw_seq = 0
        self._raw_lock = threading.Lock()
        self.status = _CONNECTING
        self.detail = "Холбож байна…"

    def latest(self) -> tuple[Image.Image | None, int]:
        """Latest frame plus its sequence number (so the UI repaints only on change)."""
        with self._lock:
            return self._latest, self._seq

    def pause(self) -> None:
        """Stop decoding and drop the capture until resumed (tile hidden)."""
        self._active.clear()

    def resume(self) -> None:
        self._active.set()

    def set_box(self, w: int, h: int) -> None:
        with self._lock:
            self._box = (max(80, int(w)), max(45, int(h)))

    def _get_box(self) -> tuple[int, int]:
        with self._lock:
            return self._box

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            # Probe only: _open() imports cv2 itself. This early import turns a
            # missing/broken OpenCV install into a clear UI error instead of a
            # silent "Холбогдсонгүй".
            import cv2  # noqa: F401
        except Exception as e:  # noqa: BLE001 — surface the import failure in the UI
            self.status = _ERROR
            self.detail = "OpenCV ачаалагдсангүй (cv2)"
            log.error("local_view.cv2_import_failed", error=str(e))
            return

        # Stagger the first connect so all cameras don't open at the same instant.
        if self._start_delay and self._stop_event.wait(self._start_delay):
            return

        while not self._stop_event.is_set():
            # Paused (hidden behind a focused tile): hold no capture, idle cheaply.
            if not self._active.is_set():
                if self._stop_event.wait(0.2):
                    break
                continue
            # Pinned URL first (fast reconnect); otherwise try all candidates.
            idxs = [self._pin] if self._pin is not None else list(range(len(self.urls)))
            streamed = False
            for i in idxs:
                if self._stop_event.is_set():
                    return
                cap = self._open(self.urls[i])
                if cap is None:
                    continue
                self._pin = i
                self.status = _LIVE
                self.detail = "Шууд"
                self._consume(cap)  # blocks until the stream drops or we stop
                cap.release()  # type: ignore[attr-defined]
                streamed = True
                break

            if self._stop_event.is_set():
                break
            # RTSP failed → try the browser-style HTTP snapshot fallback before
            # giving up (the camera may serve a picture over HTTP but no RTSP).
            if not streamed and self._snapshot_urls:
                streamed = self._consume_snapshot()
            if self._stop_event.is_set():
                break
            if not streamed:
                # Nothing worked this round. Un-pin so the next round re-tries
                # every candidate (the camera may have moved or changed path).
                self._pin = None
                self.status = _ERROR
                self.detail = "Холбогдсонгүй — дахин оролдож байна…"
                # Non-fatal: a console-encoding error must never kill the reader
                # thread (the camera name is Cyrillic; stdout may be cp1252).
                # Log the host only — never the full URL (it carries credentials).
                with contextlib.suppress(Exception):
                    log.warning("local_view.open_failed", host=_redact_host(self.urls[-1]))
            else:
                self.status = _RECONNECTING
                self.detail = "Холболт тасарсан — дахин холбож байна…"
            if self._stop_event.wait(2.0):
                break

    def _open(self, url: str) -> object | None:
        """Open `url` with a hardware-decode hint, falling back to plain software.

        The HW hint (``CAP_PROP_HW_ACCELERATION=ANY``) offloads decode to the
        iGPU/GPU when the OpenCV build + driver support it; otherwise OpenCV
        silently decodes in software, so passing it is safe on every box. Builds
        that reject the params arg, or a HW attempt that opens but never delivers
        a frame, fall through to a plain software open. We log the negotiated
        path once (so the store PC's effective decode mode is visible in the logs
        remotely, without the user running anything)."""
        import cv2

        attempts: list[list[int]] = []
        if _HWACCEL_ENABLED:
            attempts.append([int(cv2.CAP_PROP_HW_ACCELERATION), int(cv2.VIDEO_ACCELERATION_ANY)])
        attempts.append([])  # plain software open — always tried as the fallback
        for params in attempts:
            try:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
            except Exception:  # noqa: BLE001 — some builds reject the params arg
                continue
            with contextlib.suppress(Exception):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # low latency; some ignore it
            if cap.isOpened() and self._prime(cap):
                with contextlib.suppress(Exception):
                    hw = float(cap.get(cv2.CAP_PROP_HW_ACCELERATION))
                    if hw != self._hw_logged:
                        self._hw_logged = hw
                        log.info(
                            "local_view.decode_path",
                            cam=self.cam_name,
                            hwaccel="on" if hw > 0 else "software",
                        )
                return cast("object", cap)
            cap.release()
            if self._stop_event.is_set() or not self._active.is_set():
                return None
        return None

    def _prime(self, cap: object) -> bool:
        """Confirm the stream really delivers a frame (so a 404/401 path is
        rejected fast and we fall through to the next candidate). Stores the
        first frame so the tile lights up immediately."""
        deadline = time.monotonic() + _PRIME_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._stop_event.is_set() or not self._active.is_set():
                return False  # stopped or paused mid-prime → drop this attempt
            ok, frame = cap.read()  # type: ignore[attr-defined]
            if ok and frame is not None:
                self._store(frame)
                return True
            # Pre-keyframe reads can return empty instantly — a tiny sleep keeps
            # the (now up to 12 s) wait from busy-spinning a core per HEVC camera.
            time.sleep(0.02)
        return False

    def _consume(self, cap: object) -> None:
        """Pump frames until the stream drops (then the caller reconnects).

        Stays a TIGHT loop: read → publish the newest raw frame for the edge
        worker (cheap) and/or paint it directly. Inference NEVER runs here, so a
        slow YOLO infer can't stall reads (the low-latency capture would drop
        packets mid-GOP → smearing/lag)."""
        if not self._edge_decided:
            self._edge_decided = True
            with contextlib.suppress(Exception):
                from sentry_agent_pc.settings import get_settings

                self._edge_on = bool(get_settings().edge_ai_enabled)
        fails = 0
        while not self._stop_event.is_set():
            if not self._active.is_set():
                return  # paused → drop the capture; run() idles until resumed
            ok, frame = cap.read()  # type: ignore[attr-defined]
            if not ok or frame is None:
                fails += 1
                if fails > 15:
                    return  # stream dropped → reconnect
                continue
            fails = 0
            if self._edge_on and not self._edge_failed:
                self._publish_raw(frame)
                if self._edge_worker is None or not self._edge_worker.is_alive():
                    # (Re)start if never started or the worker died — reset _ready
                    # so decode paints raw until the fresh worker takes over.
                    self._edge_ready = False
                    self._start_edge_worker()
                # Decode keeps painting raw until the worker takes over — so the
                # tile is never blank and the video stays live during model load.
                if not self._edge_ready:
                    self._store(frame)
            else:
                self._store(frame)

    def _publish_raw(self, frame: object) -> None:
        with self._raw_lock:
            self._raw_frame = frame
            self._raw_seq += 1

    def _start_edge_worker(self) -> None:
        self._edge_worker = threading.Thread(
            target=self._edge_loop, name=f"edge-{self.cam_name}", daemon=True
        )
        self._edge_worker.start()

    def _edge_loop(self) -> None:
        """Build the edge pipeline once, then continuously annotate the LATEST raw
        frame (drop-old) on this thread — decoupled from decode."""
        try:
            from sentry_agent_pc.edge.ov_lean import LeanOpenVinoDetector
            from sentry_agent_pc.edge.pipeline import EdgePipeline

            detector = LeanOpenVinoDetector()
        except Exception as e:  # noqa: BLE001 — no model/openvino → decode shows raw
            self._edge_failed = True
            self._edge_err = str(e)[:160]
            record_edge_error(self.cam_name, self._edge_err)
            log.info("local_view.edge_off", cam=self.cam_name, reason=self._edge_err)
            return

        recorder = self._build_clip_recorder()
        # docs/29 P1c (edge) — feed the camera's drawn zones to the edge engine so
        # exit_after_concealment / repeated_shelf_visit fire locally too.
        cam = _find_camera(self.cam_name)
        zones = cam.zones if cam is not None else None
        self._edge_pipe = EdgePipeline(self.cam_name, detector, recorder=recorder, zones=zones)
        log.info(
            "local_view.edge_on",
            cam=self.cam_name,
            clips=recorder is not None,
            zones=len(zones) if zones else 0,
        )
        try:
            self._edge_run()
        finally:
            if recorder is not None:
                with contextlib.suppress(Exception):
                    recorder.stop()

    def _build_clip_recorder(self) -> EdgeClipRecorder | None:
        """A −3s…+3s clip recorder for this camera — ONLY when reading off the
        local MediaMTX fan-out (loopback), so it never opens a 2nd direct camera
        connection. None otherwise (no clips, no risk)."""
        try:
            from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR, get_settings

            if not getattr(get_settings(), "edge_clips_enabled", True):
                return None
            pin = self._pin if self._pin is not None and self._pin < len(self.urls) else 0
            src = self.urls[pin] if self.urls else ""
            if "127.0.0.1" not in src and "localhost" not in src:
                return None  # not the fan-out loopback → skip (avoid 2nd connection)
            from sentry_agent_pc.edge.recorder import EdgeClipRecorder

            on_clip = self._make_clip_uploader()
            rec = EdgeClipRecorder(
                self.cam_name,
                src,
                DEFAULT_CONFIG_DIR / "edge",
                _shared_clip_store(),
                on_clip=on_clip,
            )
            rec.start()
            self._edge_recorder = rec
            log.info("local_view.edge_clips_on", cam=self.cam_name, upload=on_clip is not None)
            return rec
        except Exception as e:  # noqa: BLE001 — recording is optional, never fatal
            log.info("local_view.edge_clips_off", cam=self.cam_name, reason=str(e)[:120])
            return None

    def _make_clip_uploader(self) -> Callable[[ClipRecord], None] | None:
        """on_clip that forwards each suspicious clip to the cloud (ADR-0029 B3),
        ONLY for a registered EDGE_PC camera and when EDGE_UPLOAD_ENABLED. None →
        record into the local gallery only (a `cloud` camera is handled by the
        central pipeline, NOT the edge upload — I8 topology gate)."""
        from sentry_agent_pc.settings import get_settings

        if not getattr(get_settings(), "edge_upload_enabled", True):
            return None
        cam = _find_camera(self.cam_name)
        if cam is None or not cam.uuid:
            log.info("local_view.edge_upload_skip_unregistered", cam=self.cam_name)
            return None
        if cam.compute_tier != "edge_pc":
            log.info(
                "local_view.edge_upload_skip_not_edge", cam=self.cam_name, tier=cam.compute_tier
            )
            return None
        from sentry_agent_pc.edge.uploader import make_clip_uploader

        log.info("local_view.edge_upload_on", cam=self.cam_name)
        return make_clip_uploader(cam.uuid)

    def _edge_run(self) -> None:
        last_seq = -1
        fail_count = 0
        # Gate on _stop_event ONLY — NOT _active. A paused tile (focus/minimize)
        # just idles here; exiting would kill the worker, and the decode loop
        # never restarts it (it's non-None), leaving _edge_ready=True so the tile
        # freezes on its last annotated frame. Staying alive also avoids rebuilding
        # the OpenVINO models on every resume.
        while not self._stop_event.is_set():
            if not self._active.is_set():
                if self._stop_event.wait(0.05):
                    break
                continue
            with self._raw_lock:
                seq, frame = self._raw_seq, self._raw_frame
            if frame is None or seq == last_seq:
                if self._stop_event.wait(0.01):
                    break
                continue
            last_seq = seq
            pipe = self._edge_pipe
            if pipe is None:
                return
            try:
                # Detect + overlay on a TILE-sized copy (not full res) — YOLO
                # letterboxes to 640 internally, so this loses no accuracy and
                # avoids full-res draw/copy churn.
                annotated = pipe.process(self._fit_to_box(frame), time.time())
                self._store(annotated)
                if not self._edge_ready:
                    clear_edge_error(self.cam_name)  # recovered → drop stale badge
                self._edge_ready = True
                fail_count = 0
            except Exception as e:  # noqa: BLE001 — one bad infer must not kill edge
                fail_count += 1
                if fail_count >= 30:  # persistent failure → release models, show raw
                    self._edge_failed = True
                    self._edge_err = str(e)[:160]
                    record_edge_error(self.cam_name, self._edge_err)
                    self._edge_pipe = None
                    log.warning("local_view.edge_giveup", cam=self.cam_name, reason=self._edge_err)
                    return

    def _fit_to_box(self, frame: object) -> Any:
        """Downscale a BGR frame to fit the tile box (keep aspect), so edge work
        runs at display resolution, not full decode resolution."""
        import cv2

        arr = cast("np.ndarray", frame)
        ih, iw = arr.shape[:2]
        box_w, box_h = self._get_box()
        scale = min(box_w / max(1, iw), box_h / max(1, ih))
        if scale >= 1.0:
            return arr
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        return cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA)

    def _store(self, frame: object) -> None:
        """BGR frame → tile-sized RGB PIL image (off the UI thread).

        The resize + letterbox is done in OpenCV (C, releases the GIL) instead of
        PIL — far cheaper per frame, which is what keeps N cameras from saturating
        the box. PIL is used only for the final zero-copy ``fromarray`` wrap.

        DEFENSIVE: a corrupt/partial decode — some P2P / H.265 cameras (e.g. the
        Skyworth ZHCSDB6) emit zero-dimension or odd-channel frames — must SKIP
        the frame, never raise. An unhandled error here used to crash the reader
        thread (e.g. ``box_w / 0`` → ZeroDivisionError) and freeze the tile on its
        last image, which read as "stuck on a width/height/resolution error".
        """
        import cv2
        import numpy as np

        arr = cast("np.ndarray", frame)  # a cv2 BGR frame (ndarray)
        if not isinstance(arr, np.ndarray) or arr.ndim < 2:
            return
        ih, iw = arr.shape[:2]
        if iw <= 0 or ih <= 0:
            return  # 0-dim frame → can't scale; drop it instead of dividing by 0
        try:
            box_w, box_h = self._get_box()
            scale = min(box_w / iw, box_h / ih)
            nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
            resized = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA)
            if resized.ndim == 2:  # grayscale → promote to 3-channel BGR
                resized = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
            if resized.ndim != 3 or resized.shape[2] != 3:
                return  # unexpected channel count — can't letterbox onto BGR
            canvas = np.full((box_h, box_w, 3), 16, dtype=np.uint8)  # dark backdrop
            x0, y0 = (box_w - nw) // 2, (box_h - nh) // 2
            canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
            rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
        except Exception:  # noqa: BLE001 — one bad frame must not kill the reader
            return
        with self._lock:
            self._latest = img
            self._seq += 1

    def _consume_snapshot(self) -> bool:
        """Browser-style HTTP snapshot fallback when no RTSP path opens.

        Polls a JPEG endpoint (Hik/Dahua/ONVIF/OEM conventions), decodes it, and
        renders at ~1.4 fps. PINS the working endpoint so a reconnect goes
        straight back to it. Returns True once it has shown ≥1 frame (so the
        caller treats it as a live source and shows "reconnecting", not "failed").
        """
        import cv2
        import numpy as np

        from sentry_agent_pc.discovery.snapshot import fetch_snapshot

        def _grab(url: str) -> bool:
            data = fetch_snapshot(url, self._snap_user, self._snap_pass)
            if not data:
                return False
            arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                return False
            self._store(arr)
            return True

        idxs = (
            [self._snap_pin]
            if self._snap_pin is not None
            else list(range(len(self._snapshot_urls)))
        )
        for i in idxs:
            if self._stop_event.is_set() or not self._active.is_set():
                return False
            if not _grab(self._snapshot_urls[i]):
                continue
            self._snap_pin = i
            self.status = _LIVE
            self.detail = "Снапшот (HTTP)"
            # Pump this pinned endpoint until it stops/fails repeatedly, then
            # return so run() re-tries RTSP first on the next round.
            fails = 0
            while not self._stop_event.wait(_SNAPSHOT_INTERVAL_SEC) and self._active.is_set():
                if _grab(self._snapshot_urls[i]):
                    fails = 0
                else:
                    fails += 1
                    if fails > 3:
                        break
            return True
        self._snap_pin = None  # nothing answered → re-scan all next time
        with contextlib.suppress(Exception):
            log.warning("local_view.snapshot_failed", host=_redact_host(self._snapshot_urls[-1]))
        return False


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
        self._painted_seq = -1  # last frame seq drawn; skip idle PhotoImage rebuilds
        self._has_image = False
        self._placeholder = ""  # last placeholder text shown (avoid idle relabels)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(
            bar,
            text=reader.cam_name,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            text_color="gray85",
        ).pack(side="left")

        self._badge = ctk.CTkLabel(
            bar,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            corner_radius=6,
            fg_color="#3B3320",
            text_color="gray90",
            padx=8,
            pady=2,
        )
        self._badge.pack(side="right")

        # Fixed-pixel container → the video Label never resizes the tile by text.
        self._holder = ctk.CTkFrame(
            self,
            fg_color="#0E0E10",
            corner_radius=8,
            width=self._vid_w,
            height=self._vid_h,
        )
        self._holder.pack(padx=10, pady=(0, 10))
        self._holder.pack_propagate(False)

        self._video = tk.Label(
            self._holder,
            bg="#0E0E10",
            bd=0,
            highlightthickness=0,
            fg="gray60",
            font=("Segoe UI", 12),
        )
        self._video.pack(fill="both", expand=True)

    def bind_double_click(self, handler: Callable[..., object]) -> None:
        """Route a double-click anywhere on the tile to `handler`.

        Binding only the outer frame is dead: the inner video Label + holder fill
        the tile and swallow the event (Tk doesn't propagate <Double-Button-1> to
        the parent), so double-clicking the actual video area did nothing. Bind
        the frame AND every child that covers it so the whole tile is clickable.
        """
        self.bind("<Double-Button-1>", handler)
        self._holder.bind("<Double-Button-1>", handler)
        self._video.bind("<Double-Button-1>", handler)

    def set_video_size(self, w: int, h: int) -> None:
        w, h = max(80, int(w)), max(45, int(h))
        if (w, h) == (self._vid_w, self._vid_h):
            return
        self._vid_w, self._vid_h = w, h
        self._holder.configure(width=w, height=h)
        self.reader.set_box(w, h)
        self._painted_seq = -1  # box changed → force one repaint at the new size

    def paint(self) -> None:
        text, fg, dot = _STATUS_STYLE.get(self.reader.status, _STATUS_STYLE[_CONNECTING])
        self._badge.configure(text=f"●  {text}", fg_color=fg, text_color=dot)

        img, seq = self.reader.latest()
        if img is not None:
            if seq == self._painted_seq:
                return  # no new frame since last paint → skip the PhotoImage rebuild
            self._painted_seq = seq
            photo = ImageTk.PhotoImage(img)
            self._video.configure(image=photo, text="")
            self._video.image = photo  # type: ignore[attr-defined]  # keep ref (Tk GC)
            self._has_image = True
            self._placeholder = ""
        else:
            # No frame yet / stream dropped: show the status text, but only
            # reconfigure when it actually changed (not every tick).
            detail = self.reader.detail
            if self._has_image or detail != self._placeholder:
                self._video.configure(image="", text=detail, compound="center")
                self._video.image = None  # type: ignore[attr-defined]
                self._has_image = False
                self._placeholder = detail


class LocalLiveView(ctk.CTkToplevel):
    """A responsive grid window that plays the store's cameras straight off the LAN."""

    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master)
        self.title("Sentry — Шууд харах (офлайн)")
        self.configure(fg_color="#0B0B0D")
        self.transient(master)

        cams = [c for c in load_state().cameras if c.rtsp_url]
        widgets.setup_dialog(self, 1180, 760, min_width=560, min_height=420)

        store = load_state().store_name
        title = ctk.CTkFrame(self, fg_color="transparent")
        title.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(
            title,
            text="Шууд харах",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="gray95",
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            title,
            text=(
                f"{len(cams)} камер · LAN-аас шууд · интернэт шаардахгүй"
                + (f" · {store}" if store else "")
                if cams
                else "Камер алга"
            ),
            font=ctk.CTkFont(size=12),
            text_color="gray55",
            anchor="w",
        ).pack(side="left", padx=12, pady=(6, 0))

        if not cams:
            self._empty()
            return

        # Scrollable so any number of cameras works without clipping.
        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.pack(fill="both", expand=True, padx=12, pady=12)

        # Share the push relay's single camera pull via the local MediaMTX hub
        # when it's up; fall back to direct URLs (offline / hub down).
        from sentry_agent_pc.discovery.snapshot import snapshot_urls
        from sentry_agent_pc.gui.edit_dialog import parse_rtsp
        from sentry_agent_pc.streaming.controller import get_stream_controller

        ctrl = get_stream_controller()

        self._readers: list[_CameraReader] = []
        self._tiles: list[_Tile] = []
        for i, cam in enumerate(cams):
            local = ctrl.local_url(cam.mediamtx_path)
            # Browser-style HTTP snapshot fallback: parse host+creds off the RTSP
            # URL and offer the camera's HTTP picture endpoints if RTSP won't open.
            parts = parse_rtsp(cam.rtsp_url)
            snaps = snapshot_urls(parts["host"]) if parts.get("host") else []
            reader = _CameraReader(
                cam.name,
                _reader_urls(cam.rtsp_url, local),
                start_delay=i * _CONNECT_STAGGER_SEC,
                snapshot_urls=snaps,
                snap_user=parts.get("user") or None,
                snap_pass=parts.get("password") or None,
            )
            reader.start()
            self._readers.append(reader)
            tile = _Tile(self._scroll, reader)
            # Bind the frame AND its video Label/holder so a double-click on the
            # actual video area focuses the tile (the children would otherwise
            # swallow the event — Tk doesn't bubble it to the parent frame).
            tile.bind_double_click(lambda _e, t=tile: self._toggle_focus(t))
            self._tiles.append(tile)

        self._focused: _Tile | None = None
        self._cols = 0
        self._last_w = 0
        self._closed = False
        self._minimized = False
        self._edge_cfg_version = -1  # forces the first poll to apply (I7)
        # Pending self-rescheduling timer ids, so _on_close can cancel them
        # instead of relying on the _closed flag to no-op a queued tick after
        # the widget is destroyed.
        self._tick_after: str | None = None
        self._edge_after: str | None = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._scroll.bind("<Configure>", self._on_resize)
        self.after(50, self._relayout)
        self._tick()
        self._tick_edge_config()

    def _empty(self) -> None:
        ctk.CTkLabel(
            self,
            text="RTSP-тэй камер бүртгэгдээгүй байна.\n"
            "'Камер хайх (Scan)' эсвэл 'Камер нэмэх'-ээр камераа нэмнэ үү.",
            font=ctk.CTkFont(size=14),
            text_color="gray55",
            justify="center",
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

        # Also constrain by the available HEIGHT so a single / few cameras fill
        # the viewport instead of sitting small with a big black gap below.
        # ~52px per row covers the tile's title bar + padding.
        rows = (len(self._tiles) + cols - 1) // cols
        avail_h = max(200, self._scroll.winfo_height() - 8)
        max_vid_h = (avail_h - rows * (12 + 52)) // rows
        if max_vid_h > 160 and vid_h > max_vid_h:
            vid_h = max_vid_h
            vid_w = int(vid_h * 16 / 9)  # keep 16:9; column weight centres it

        for c in range(_MAX_COLS):
            self._scroll.grid_columnconfigure(c, weight=1 if c < cols else 0)

        for i, t in enumerate(self._tiles):
            r, c = divmod(i, cols)
            t.grid(row=r, column=c, padx=6, pady=6, sticky="n")
            t.set_video_size(vid_w, vid_h)
        self._cols = cols

    def _toggle_focus(self, tile: _Tile) -> None:
        self._focused = None if self._focused is tile else tile
        self._apply_visibility()
        self._last_w = 0  # force a relayout
        self._relayout()

    def _apply_visibility(self) -> None:
        """Decode only what's actually on screen: nothing when minimized, just the
        focused camera in focus mode, otherwise every tile. Keeps the always-on
        agent box cool when the user leaves the window minimized."""
        for t in self._tiles:
            if self._minimized:
                t.reader.pause()
            elif self._focused is not None:
                (t.reader.resume if t is self._focused else t.reader.pause)()
            else:
                t.reader.resume()

    # ----- paint loop ---------------------------------------------------------

    def _tick(self) -> None:
        if self._closed:
            return
        # Pause/resume decoding when the window is minimized or restored.
        try:
            minimized = self.state() in ("iconic", "withdrawn")
        except Exception:  # noqa: BLE001 — Tk may transiently refuse state(); ignore
            minimized = self._minimized
        if minimized != self._minimized:
            self._minimized = minimized
            self._apply_visibility()
        if not minimized:
            for tile in self._tiles:
                with contextlib.suppress(Exception):
                    tile.paint()
        self._tick_after = self.after(int(1000 / _TARGET_FPS), self._tick)

    def _tick_edge_config(self) -> None:
        """I7: every 30 s, hot-apply the backend's edge tunables to the running
        pipelines. The network fetch runs OFF the UI thread; re-applies only on a
        version change (no-op until the backend serves per-store config)."""
        if self._closed:
            return
        # Snapshot the live pipelines, dropping any that a reader's edge loop has
        # cleared (set to None on give-up) — passing a None into the poller would
        # be a per-frame footgun. A reference read is atomic under the GIL.
        pipes = [p for p in (r._edge_pipe for r in self._readers) if p is not None]

        def work() -> None:
            if self._closed or not pipes:
                return
            from sentry_agent_pc.backend_client import BackendClient
            from sentry_agent_pc.edge.config_poller import poll_and_apply

            self._edge_cfg_version = poll_and_apply(BackendClient(), pipes, self._edge_cfg_version)

        threading.Thread(target=work, name="edge-config-poll", daemon=True).start()
        self._edge_after = self.after(30000, self._tick_edge_config)

    def _on_close(self) -> None:
        self._closed = True
        # Cancel the pending self-rescheduling timers so a queued tick can't fire
        # into the widget mid-teardown (don't lean on the _closed no-op).
        for aid in (self._tick_after, self._edge_after):
            if aid is not None:
                with contextlib.suppress(Exception):
                    self.after_cancel(aid)
        for reader in self._readers:
            reader.stop()
        # Join the reader threads (bounded) so each VideoCapture/ffmpeg session is
        # released promptly instead of lingering after the window is gone. Cap the
        # TOTAL wait so a wedged capture can't hang the UI on close — a 0.4s read
        # in progress will wake on the stop Event well within budget.
        deadline = time.monotonic() + 1.5
        for reader in self._readers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # budget spent — let the daemon thread die with the process
            reader.join(timeout=remaining)
        self.destroy()


def open_local_view(master: ctk.CTk) -> LocalLiveView:
    """Open (or focus) the offline LAN grid window.

    Reuses an already-open window — without this, each click spawns a new window
    with its own per-camera reader threads + RTSP sessions, hammering the cameras
    with duplicate connections."""
    existing = getattr(master, "_local_view_win", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return cast("LocalLiveView", existing)
        except tk.TclError:
            pass  # window was destroyed — fall through and open a fresh one
    win = LocalLiveView(master)
    master._local_view_win = win
    return win
