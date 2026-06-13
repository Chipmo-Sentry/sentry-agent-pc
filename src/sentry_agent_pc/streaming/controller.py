"""Tie the StreamPusher to backend stream-config + local camera state.

The GUI calls `start()` once after pairing; thereafter `refresh()` (on camera
add/delete or a timer) reconciles the running ffmpeg relays with the current
camera list. If the backend reports push is disabled (pull/on-LAN topology),
this is a no-op and no ffmpeg runs.
"""

from __future__ import annotations

import threading

from sentry_agent_pc.backend_client import BackendClient
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.resources import resolve_mediamtx_exe
from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR, get_settings
from sentry_agent_pc.state import load_state
from sentry_agent_pc.streaming.local_mediamtx import LocalMediaMTX
from sentry_agent_pc.streaming.pusher import PushTarget, StreamPusher

log = get_logger("sentry_agent_pc.streaming.controller")


class StreamController:
    """Singleton-ish owner of the StreamPusher for the GUI process.

    Also owns the optional :class:`LocalMediaMTX` fan-out hub. When push is
    enabled and the hub comes up, every camera is pulled once by MediaMTX and
    BOTH the push relay and the offline grid read from it — the camera sees a
    single RTSP session. If the hub can't start, relays use direct camera URLs.
    """

    def __init__(self) -> None:
        self._pusher: StreamPusher | None = None
        self._push_enabled = False
        self._lock = threading.Lock()
        s = get_settings()
        self._local: LocalMediaMTX | None = None
        if s.local_fanout_enabled:
            self._local = LocalMediaMTX(
                exe_path=resolve_mediamtx_exe(s.mediamtx_path),
                config_dir=DEFAULT_CONFIG_DIR,
                rtsp_port=s.local_mediamtx_rtsp_port,
                api_port=s.local_mediamtx_api_port,
            )

    @property
    def push_enabled(self) -> bool:
        return self._push_enabled

    def local_url(self, mediamtx_path: str | None) -> str | None:
        """Loopback URL for a camera served by the local hub, else None.

        Called by the offline grid so it reads through the same single camera
        pull as the push relay instead of opening its own session.
        """
        with self._lock:
            return self._local.local_url(mediamtx_path) if self._local else None

    def refresh(self) -> None:
        """Re-fetch stream-config (cheap) and reconcile relays with state.

        Safe to call from a background thread. Never raises — logs instead.
        """
        with self._lock:
            try:
                self._refresh_locked()
            except Exception as e:  # noqa: BLE001 — bg refresh must never crash the thread
                # Covers BackendError AND httpx connection/timeout errors (a
                # flaky store uplink is exactly when this fires) — relays keep
                # running on the last good config; next refresh retries.
                log.warning("stream_controller.refresh_failed", error=str(e))

    def _refresh_locked(self) -> None:
        state = load_state()
        if not state.is_paired:
            self._teardown()
            return

        cfg = BackendClient().agent_stream_config()
        self._push_enabled = bool(cfg.get("push_enabled"))
        if not self._push_enabled or not cfg.get("push_rtsp_base"):
            self._teardown()
            return

        if self._pusher is None:
            self._pusher = StreamPusher(
                push_base=str(cfg["push_rtsp_base"]),
                publish_user=cfg.get("publish_user"),
                publish_pass=cfg.get("publish_pass"),
            )
        else:
            # Credentials/base may have changed — rebuild if so.
            if self._pusher.push_base != cfg["push_rtsp_base"]:
                self._pusher.stop_all()
                self._pusher = StreamPusher(
                    push_base=str(cfg["push_rtsp_base"]),
                    publish_user=cfg.get("publish_user"),
                    publish_pass=cfg.get("publish_pass"),
                )

        # Bring up the local fan-out hub for every camera, then point each relay
        # at the loopback path instead of the camera. If the hub is unavailable,
        # local_url() returns None and the relay reads the camera directly — the
        # pre-fan-out behaviour, so this can only improve, never regress.
        cams = [
            (c.mediamtx_path, c.rtsp_url)
            for c in state.cameras
            if c.mediamtx_path and c.rtsp_url
        ]
        if self._local is not None:
            self._local.sync(cams)

        targets = []
        for c in state.cameras:
            if not (c.mediamtx_path and c.rtsp_url):
                continue
            local = self._local.local_url(c.mediamtx_path) if self._local else None
            targets.append(
                PushTarget(
                    mediamtx_path=c.mediamtx_path,
                    lan_rtsp=local or c.rtsp_url,
                    codec=c.codec,
                )
            )
        self._pusher.sync(targets)

    def status(self) -> list[dict[str, object]]:
        with self._lock:
            return self._pusher.status() if self._pusher else []

    def stop(self) -> None:
        with self._lock:
            self._teardown()

    def _teardown(self) -> None:
        if self._pusher is not None:
            self._pusher.stop_all()
            self._pusher = None
        if self._local is not None:
            self._local.stop()


_controller: StreamController | None = None


def get_stream_controller() -> StreamController:
    global _controller
    if _controller is None:
        _controller = StreamController()
    return _controller
