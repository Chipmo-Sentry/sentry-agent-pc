"""Headless edge Stage-1 controller — runs the edge engine 24/7, GUI-independent.

For every registered ``edge_pc`` camera this drives ONE decode loop off the local
MediaMTX loopback (the same single-pull fan-out the push relay uses) and feeds
frames to a shared :class:`EdgeRuntime`, which runs YOLO + behaviour and, on a
suspicious episode, cuts the clip and uploads it to the cloud VLM host
(``POST /agent/edge/clips`` → sentry-ai → alert).

Unlike the local-view tile (which builds its own pipeline only while its window is
open), this runs as long as the app PROCESS is alive — even minimised to the tray.
That is a hard founder requirement: edge analysis must never depend on a GUI
window being open. See docs/32-EDGE-FIRST-IMPLEMENTATION.md (D1, D4).

`cloud`-tier cameras are ignored here entirely — they are handled by the central
(node) pipeline, so this controller is a no-op until a camera is `edge_pc`.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import Detector
from sentry_agent_pc.edge.recorder import ClipRecord
from sentry_agent_pc.edge.runtime import EdgeRuntime
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.redact import scrub_credentials
from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR, get_settings
from sentry_agent_pc.state import load_state
from sentry_agent_pc.streaming.controller import get_stream_controller

log = get_logger("sentry_agent_pc.edge.controller")

_RECONNECT_MIN_SEC = 1.0
_RECONNECT_MAX_SEC = 15.0


def _detector_factory(_cfg: EdgeConfig) -> Detector:
    # Lazy import: OpenVINO/model load is heavy and optional (a box without the
    # model just won't run edge). Raises → caller logs and skips the camera.
    from sentry_agent_pc.edge.ov_lean import LeanOpenVinoDetector

    return LeanOpenVinoDetector(open_vocab=_cfg.open_vocab_items)


class _CamWorker:
    """One camera's headless decode loop: read the loopback → EdgeRuntime.process().

    The EdgeRuntime pipeline auto-submits suspicious episodes to the recorder, so
    this loop only has to keep frames flowing. Reconnects with backoff when the
    loopback drops (e.g. the fan-out restarts)."""

    def __init__(
        self,
        runtime: EdgeRuntime,
        camera_id: str,
        src_url: str,
        zones: list[dict[str, object]] | None = None,
    ) -> None:
        self._runtime = runtime
        self.camera_id = camera_id
        self.src_url = src_url
        # docs/33 P0-6 — the camera's detection polygons, threaded into the
        # pipeline so the zone criteria work in the 24/7 path too. Also the
        # restart-on-change snapshot (see _refresh_locked).
        self.zones = zones
        self._stop = threading.Event()
        self._last_wh: tuple[int, int] | None = None  # (w, h) of the last decoded frame
        self._frame_seq = 0
        self._thread = threading.Thread(target=self._run, name=f"edge-cam-{camera_id}", daemon=True)
        self._poster = threading.Thread(
            target=self._post_loop, name=f"edge-post-{camera_id}", daemon=True
        )

    def start(self) -> None:
        self._runtime.start_camera(self.camera_id, self.src_url, zones=self.zones)
        self._thread.start()
        self._poster.start()

    def stop(self) -> None:
        self._stop.set()
        self._runtime.stop_camera(self.camera_id)

    def _post_loop(self) -> None:
        """Push the latest edge tracks to the cloud live overlay at ~5 fps —
        decoupled from decode so a slow POST never stalls inference. Overlay-only:
        the backend publishes to the WS broker, never the alert path (docs/32 P2b)."""
        from sentry_agent_pc.backend_client import BackendClient

        while not self._stop.wait(0.2):
            wh = self._last_wh
            if wh is None:
                continue
            tracks = self._runtime.latest_tracks(self.camera_id)
            if not tracks:
                continue
            frame = {
                "camera_id": self.camera_id,
                "frame_id": self._frame_seq,
                "ts_ms": int(time.time() * 1000),
                "width": wh[0],
                "height": wh[1],
                "tracks": tracks,
            }
            try:
                BackendClient().agent_post_live_metadata([frame])
            except Exception:  # noqa: BLE001 — overlay feed is best-effort
                log.debug("edge.overlay_post_failed", camera_id=self.camera_id)

    def _run(self) -> None:
        backoff = _RECONNECT_MIN_SEC
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.src_url, cv2.CAP_FFMPEG)
            with contextlib.suppress(Exception):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # fresher frames, less latency
            if not cap.isOpened():
                cap.release()
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, _RECONNECT_MAX_SEC)
                continue
            backoff = _RECONNECT_MIN_SEC
            log.info("edge.decode_open", camera_id=self.camera_id)
            try:
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        break  # stream dropped → reconnect
                    try:
                        self._runtime.process(
                            self.camera_id, cast("NDArray[np.uint8]", frame), time.time()
                        )
                        self._last_wh = (int(frame.shape[1]), int(frame.shape[0]))
                        self._frame_seq += 1
                    except Exception:  # noqa: BLE001 — one bad frame must not kill the loop
                        log.exception("edge.process_failed", camera_id=self.camera_id)
            finally:
                cap.release()
            if self._stop.is_set():
                break
            log.info("edge.decode_lost", camera_id=self.camera_id)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, _RECONNECT_MAX_SEC)


class EdgeController:
    """Owns the headless EdgeRuntime + one decode worker per ``edge_pc`` camera."""

    _CONFIG_POLL_SEC = 30.0

    def __init__(self) -> None:
        self._runtime = EdgeRuntime(
            DEFAULT_CONFIG_DIR / "edge", _detector_factory, on_clip=self._on_clip
        )
        self._workers: dict[str, _CamWorker] = {}
        self._lock = threading.Lock()
        self._stopped = False
        # docs/33 P0-6 — backend edge-config poll. Pre-fix the headless runtime
        # ran on hardcoded EdgeConfig defaults FOREVER: poll_and_apply was wired
        # only to the GUI live-view pipelines, so superadmin «Edge тохиргоо»
        # silently had zero effect on the always-on engine. First poll fires
        # immediately (startup config), then every _CONFIG_POLL_SEC.
        self._cfg_version = -1
        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._config_poll_loop, name="edge-config-poll", daemon=True
        )
        self._poll_thread.start()

    def _config_poll_loop(self) -> None:
        from sentry_agent_pc.backend_client import BackendClient
        from sentry_agent_pc.edge.config_poller import poll_and_apply

        while not self._poll_stop.is_set():
            try:
                # EdgeRuntime.apply_config fans the new tunables out to every
                # running pipeline AND stores the cfg for future start_camera
                # (recorder knobs like segment/keep apply on the next start).
                self._cfg_version = poll_and_apply(
                    BackendClient(), [self._runtime], self._cfg_version
                )
            except Exception:  # noqa: BLE001 — a poll blip must not kill the loop
                log.debug("edge_controller.config_poll_failed", exc_info=True)
            if self._poll_stop.wait(self._CONFIG_POLL_SEC):
                return

    def refresh(self) -> None:
        """Reconcile decode workers with the current ``edge_pc`` camera set. Safe to
        call from any thread / repeatedly; never raises (logs instead)."""
        with self._lock:
            try:
                self._refresh_locked()
            except Exception as e:  # noqa: BLE001 — a bg reconcile must not crash
                log.warning("edge_controller.refresh_failed", error=str(e))

    def _refresh_locked(self) -> None:
        if self._stopped:
            return
        s = get_settings()
        if not (s.edge_ai_enabled and s.edge_clips_enabled):
            self._teardown_locked()
            return
        state = load_state()
        if not state.is_paired:
            self._teardown_locked()
            return

        ctrl = get_stream_controller()
        # camera_id (mediamtx_path) → (decode src url, zones). Zones ride along so
        # the headless pipeline gets the same polygons the GUI tile pipeline uses
        # (docs/33 P0-6) — and a zone edit restarts the worker like a src change.
        want: dict[str, tuple[str, list[dict[str, object]] | None]] = {}
        for c in state.cameras:
            if c.compute_tier != "edge_pc" or not c.mediamtx_path:
                continue
            # Prefer the local fan-out loopback (single camera pull shared with the
            # push relay); fall back to the camera's stored RTSP URL if the hub is
            # down. NB: do NOT require `rtsp_url` — a camera re-registered then
            # synced from the backend has no local RTSP (the backend stores it
            # encrypted, never returns plaintext), yet the loopback still exists
            # because the push relay is feeding it. Skip only when BOTH are absent.
            loopback = ctrl.local_url(c.mediamtx_path)
            src = loopback or c.rtsp_url
            if not src:
                continue
            want[c.mediamtx_path] = (src, c.zones)

        for cid in list(self._workers):
            w = self._workers[cid]
            if cid not in want or (w.src_url, w.zones) != want[cid]:
                self._workers.pop(cid).stop()
        for cid, (src, zones) in want.items():
            if cid not in self._workers:
                worker = _CamWorker(self._runtime, cid, src, zones=zones)
                self._workers[cid] = worker
                worker.start()
                log.info(
                    "edge_controller.worker_started",
                    camera_id=cid,
                    src=scrub_credentials(src),  # direct RTSP src carries user:pass
                    zones=len(zones) if zones else 0,
                )

    def _on_clip(self, rec: ClipRecord) -> None:
        """Upload one suspicious clip to the cloud VLM — for the registered edge_pc
        camera it belongs to (resolved by mediamtx_path). Best-effort."""
        if not get_settings().edge_upload_enabled:
            return
        cam = next((c for c in load_state().cameras if c.mediamtx_path == rec.camera_id), None)
        if cam is None or not cam.uuid or cam.compute_tier != "edge_pc":
            return
        from sentry_agent_pc.backend_client import BackendClient
        from sentry_agent_pc.edge.uploader import upload_clip

        try:
            upload_clip(BackendClient(), rec, cam.uuid)
        except Exception:  # noqa: BLE001 — upload is best-effort, never fatal
            log.exception("edge_controller.upload_failed", camera_id=rec.camera_id)

    def stop(self) -> None:
        self._poll_stop.set()
        with self._lock:
            self._stopped = True
            self._teardown_locked()
            self._runtime.stop_all()

    def _teardown_locked(self) -> None:
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()


_controller: EdgeController | None = None
_controller_lock = threading.Lock()


def get_edge_controller() -> EdgeController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = EdgeController()
        return _controller
