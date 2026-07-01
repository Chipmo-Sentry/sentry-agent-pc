"""Cloudflared Quick Tunnel — expose this agent's loopback HLS to the cloud.

The cloud frontend can't reach the store PC directly (it's behind NAT). Rather
than relay video through the ephemeral GPU node, we run a cloudflared *quick
tunnel* (`cloudflared tunnel --url http://127.0.0.1:<hls_port>`) which gives a
public ``https://<random>.trycloudflare.com`` URL backed by Cloudflare's network
— no account, no VPS, no open inbound port. The agent reports that URL to the
backend; ``/live`` then proxies HLS straight from the agent. The node is needed
ONLY for on-demand VLM on suspicious clips, so video survives the node going down.

The quick-tunnel URL is ephemeral: it changes whenever cloudflared restarts, so
we re-report it on every change via the heartbeat. Supervised like the other
child processes: parse the URL from cloudflared's output, restart with backoff if
it exits, force-kill on stop so it can't outlive the app.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import threading
import time

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.streaming.tunnel")

# cloudflared prints the assigned URL once, e.g.
#   2024-... INF |  https://calm-river-1234.trycloudflare.com  |
_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

_RESTART_MIN_SEC = 2.0
_RESTART_MAX_SEC = 60.0


class CloudflaredTunnel:
    """Supervises a cloudflared quick tunnel to a loopback URL; exposes the public
    ``*.trycloudflare.com`` base. Thread-safe."""

    def __init__(self, *, exe_path: str | None, target_url: str) -> None:
        self._exe = exe_path
        self._target = target_url
        self._proc: subprocess.Popen[str] | None = None
        self._url: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str | None:
        """Current public tunnel base (``https://….trycloudflare.com``) or None."""
        with self._lock:
            return self._url

    def start(self) -> None:
        """Start the supervisor thread (idempotent). No-op without a cloudflared."""
        if not self._exe:
            log.warning("tunnel.no_binary")
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._supervise, name="cloudflared", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_proc()

    # ── internals ──────────────────────────────────────────────────────────

    def _supervise(self) -> None:
        backoff = _RESTART_MIN_SEC
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001 — a supervisor must never die
                log.warning("tunnel.run_failed", error=str(e))
            with self._lock:
                self._url = None  # the URL dies with the process
            if self._stop.is_set():
                break
            # A tunnel that ran a good while then dropped → reset backoff; a fast
            # crash-loop (bad binary/port) → back off so we don't spin.
            backoff = (
                _RESTART_MIN_SEC
                if (time.monotonic() - started) > 30
                else min(backoff * 2, _RESTART_MAX_SEC)
            )
            log.info("tunnel.restarting", delay=round(backoff, 1))
            self._stop.wait(backoff)

    def _run_once(self) -> None:
        assert self._exe is not None
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        # --no-autoupdate: never let cloudflared swap its own binary at runtime.
        # Quick tunnel prints the assigned URL to stderr; merge stderr→stdout so
        # one reader catches it regardless of cloudflared's logging target.
        proc = subprocess.Popen(  # noqa: S603 — fixed args, bundled binary
            [
                self._exe,
                "tunnel",
                "--no-autoupdate",
                "--url",
                self._target,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        with self._lock:
            self._proc = proc
        log.info("tunnel.started", target=self._target)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                m = _URL_RE.search(line)
                if m:
                    url = m.group(0)
                    with self._lock:
                        self._url = url
                    log.info("tunnel.url", url=url)
            proc.wait()
        finally:
            self._kill_proc()

    def _kill_proc(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                proc.kill()
