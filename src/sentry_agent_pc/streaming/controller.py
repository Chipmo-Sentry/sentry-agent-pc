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
from sentry_agent_pc.resources import resolve_cloudflared_exe, resolve_mediamtx_exe
from sentry_agent_pc.settings import DEFAULT_CONFIG_DIR, get_settings
from sentry_agent_pc.state import load_state
from sentry_agent_pc.streaming.local_mediamtx import LocalMediaMTX
from sentry_agent_pc.streaming.pusher import PushTarget, StreamPusher
from sentry_agent_pc.streaming.tunnel import CloudflaredTunnel

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
        self._stopped = False  # set by stop() → refresh() can't resurrect relays
        self._lock = threading.Lock()
        s = get_settings()
        self._local: LocalMediaMTX | None = None
        self._tunnel: CloudflaredTunnel | None = None
        if s.local_fanout_enabled:
            self._local = LocalMediaMTX(
                exe_path=resolve_mediamtx_exe(s.mediamtx_path),
                config_dir=DEFAULT_CONFIG_DIR,
                rtsp_port=s.local_mediamtx_rtsp_port,
                api_port=s.local_mediamtx_api_port,
                hls_port=s.local_mediamtx_hls_port,
            )
            # The tunnel exposes that loopback HLS to the cloud frontend. Target a
            # fixed port so the public URL stays stable until cloudflared restarts.
            if s.agent_hls_tunnel_enabled:
                self._tunnel = CloudflaredTunnel(
                    exe_path=resolve_cloudflared_exe(s.cloudflared_path),
                    target_url=f"http://127.0.0.1:{s.local_mediamtx_hls_port}",
                )

    @property
    def push_enabled(self) -> bool:
        return self._push_enabled

    def local_url(self, mediamtx_path: str | None) -> str | None:
        """Loopback URL for a camera served by the local hub, else None.

        Called by the offline grid (GUI thread) so it reads through the same
        single camera pull as the push relay. Deliberately does NOT take the
        controller lock: `_local` is set once in __init__ and never reassigned,
        and LocalMediaMTX.local_url has its own lock — holding the controller
        lock here would freeze the GUI for up to ~8s when a background refresh()
        is mid-sync (MediaMTX restart + health poll).
        """
        local = self._local
        return local.local_url(mediamtx_path) if local else None

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
        if self._stopped:
            # quit_app() already tore down; a refresh() thread that was parked on
            # the lock must NOT rebuild relays/MediaMTX after stop() — that orphans
            # ffmpeg/mediamtx still pushing to the cloud after the GUI exits.
            return
        state = load_state()
        if not state.is_paired:
            self._teardown()
            return

        # The local fan-out hub + cloud HLS tunnel run whenever there are cameras —
        # they feed the edge engine, the offline view, AND agent-direct cloud video.
        # This is INDEPENDENT of pushing to the GPU node (the node is optional, may
        # be offline): video must reach the frontend straight from this agent. So
        # bring them up BEFORE — and regardless of — the push config.
        cams = [
            (c.mediamtx_path, c.rtsp_url) for c in state.cameras if c.mediamtx_path and c.rtsp_url
        ]
        if self._local is not None:
            self._local.sync(cams)
            if self._tunnel is not None:
                # Idempotent: start() no-ops if already running; the public URL is
                # reported via the heartbeat. Stop when there's nothing to serve.
                if cams:
                    self._tunnel.start()
                else:
                    self._tunnel.stop()

        # Push to the GPU node — a SEPARATE concern, gated on the backend config.
        cfg = BackendClient().agent_stream_config()
        self._push_enabled = bool(cfg.get("push_enabled"))
        if not self._push_enabled or not cfg.get("push_rtsp_base"):
            # No node push (disabled, or node offline with no base) — stop ONLY the
            # pusher; the local hub + tunnel keep serving agent-direct video.
            if self._pusher is not None:
                self._pusher.stop_all()
                self._pusher = None
            return

        if self._pusher is None:
            self._pusher = StreamPusher(
                push_base=str(cfg["push_rtsp_base"]),
                publish_user=cfg.get("publish_user"),
                publish_pass=cfg.get("publish_pass"),
            )
        else:
            # Base OR credentials may have changed — rebuild if so. Comparing creds
            # too matters: when the backend's MediaMTX publish password is rotated
            # (same push base), a base-only check would keep relaying with the stale
            # password → every publish 401s until the app is restarted.
            if (
                self._pusher.push_base != cfg["push_rtsp_base"]
                or self._pusher.publish_user != cfg.get("publish_user")
                or self._pusher.publish_pass != cfg.get("publish_pass")
            ):
                self._pusher.stop_all()
                self._pusher = StreamPusher(
                    push_base=str(cfg["push_rtsp_base"]),
                    publish_user=cfg.get("publish_user"),
                    publish_pass=cfg.get("publish_pass"),
                )

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

    def tunnel_url(self) -> str | None:
        """Public ``*.trycloudflare.com`` HLS base for this agent, or None when no
        tunnel is up. Reported via the heartbeat so ``/live`` proxies straight from
        the agent. Lock-free: `_tunnel` is set once in __init__ + has its own lock."""
        return self._tunnel.url if self._tunnel else None

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._teardown()

    def _teardown(self) -> None:
        if self._pusher is not None:
            self._pusher.stop_all()
            self._pusher = None
        if self._tunnel is not None:
            self._tunnel.stop()
        if self._local is not None:
            self._local.stop()


_controller: StreamController | None = None


def get_stream_controller() -> StreamController:
    global _controller
    if _controller is None:
        _controller = StreamController()
    return _controller
