"""Tie the StreamPusher to backend stream-config + local camera state.

The GUI calls `start()` once after pairing; thereafter `refresh()` (on camera
add/delete or a timer) reconciles the running ffmpeg relays with the current
camera list. If the backend reports push is disabled (pull/on-LAN topology),
this is a no-op and no ffmpeg runs.
"""

from __future__ import annotations

import threading

from sentry_agent_pc.backend_client import BackendClient, BackendError
from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.state import load_state
from sentry_agent_pc.streaming.pusher import PushTarget, StreamPusher

log = get_logger("sentry_agent_pc.streaming.controller")


class StreamController:
    """Singleton-ish owner of the StreamPusher for the GUI process."""

    def __init__(self) -> None:
        self._pusher: StreamPusher | None = None
        self._push_enabled = False
        self._lock = threading.Lock()

    @property
    def push_enabled(self) -> bool:
        return self._push_enabled

    def refresh(self) -> None:
        """Re-fetch stream-config (cheap) and reconcile relays with state.

        Safe to call from a background thread. Never raises — logs instead.
        """
        with self._lock:
            try:
                self._refresh_locked()
            except BackendError as e:
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

        targets = [
            PushTarget(mediamtx_path=c.mediamtx_path, lan_rtsp=c.rtsp_url)
            for c in state.cameras
            if c.mediamtx_path and c.rtsp_url
        ]
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


_controller: StreamController | None = None


def get_stream_controller() -> StreamController:
    global _controller
    if _controller is None:
        _controller = StreamController()
    return _controller
