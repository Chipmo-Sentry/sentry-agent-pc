"""Run a LOCAL MediaMTX on the agent box as the single camera fan-out point.

Cheap IP cameras cap concurrent RTSP sessions (often 1-2). The agent has two
consumers that each used to open the camera DIRECTLY — the cloud push relay
(``streaming/pusher``) and the desktop offline grid (``gui/local_view``). When
both run at once they exceed the camera's session limit → the camera drops one,
both reconnect, and the stream flaps.

The fix: a local MediaMTX process pulls each camera EXACTLY ONCE (on-demand) and
both consumers read from MediaMTX instead of from the camera. The camera sees a
single session; MediaMTX fans the frames out to every reader.

Safety — this is best-effort and ADDITIVE. If MediaMTX can't start (missing
binary, port clash, crash) :meth:`local_url` returns ``None`` and callers fall
back to the direct camera URL — i.e. exactly today's behaviour. Worst case is no
regression, never a worse one. The whole feature is gated by the
``local_fanout_enabled`` setting so the founder can disable it instantly.

Reconfiguration model: paths are written into a generated config file and
MediaMTX is restarted only when the camera/source SET actually changes (a
``_signature`` guards it). Camera edits are rare, so steady state has zero
restarts; the push relay's own backoff absorbs the brief blip when one happens.
We restart-on-change rather than hot-patch via the API because config-file paths
match the proven ingest config exactly and don't depend on per-version API field
names.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import IO

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.streaming.local_mediamtx")

# Bind everything to loopback — this hub is for the agent's own processes only,
# never exposed on the LAN. Non-standard ports avoid clashing with a manually
# run ingest/cloud MediaMTX during testing (that one uses 8554/9997).
_HOST = "127.0.0.1"
# How long to wait for the API to answer after a (re)start before declaring the
# hub unhealthy and falling back to direct camera connections.
_HEALTH_TIMEOUT_SEC = 8.0
_HEALTH_POLL_SEC = 0.25
# Windows: hide the MediaMTX console window when launched from the GUI .exe.
_CREATE_NO_WINDOW = 0x08000000


def _yaml_dquote(s: str) -> str:
    """Double-quote a YAML scalar so creds/IP-slug keys can't be mis-parsed.

    Unquoted ``192_168_1_64`` is read as an int (underscores are digit
    separators) and the underscores vanish → wrong path name. Source URLs carry
    ``: @`` and possibly ``#`` which also need quoting. Escape ``\\`` and ``"``.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _signature(paths: dict[str, str], rtsp_port: int, api_port: int, hls_port: int) -> str:
    """Stable fingerprint of the applied config — restart only when it changes."""
    body = ";".join(f"{p}={src}" for p, src in sorted(paths.items()))
    return f"{rtsp_port}|{api_port}|{hls_port}|{body}"


class LocalMediaMTX:
    """Supervises a loopback MediaMTX that fans one camera pull out to N readers.

    Thread-safe: :meth:`sync` is called from the stream-refresh thread and
    :meth:`local_url` from the GUI thread (when the offline view opens).
    """

    def __init__(
        self,
        *,
        exe_path: str | None,
        config_dir: Path,
        rtsp_port: int = 18554,
        api_port: int = 19997,
        hls_port: int = 18888,
    ) -> None:
        self._exe = exe_path
        self._config_path = config_dir / "mediamtx.local.gen.yml"
        self._log_path = config_dir / "mediamtx.local.log"
        self._rtsp_port = rtsp_port
        self._api_port = api_port
        # HLS is served on loopback so a cloudflared tunnel can expose it to the
        # cloud frontend WITHOUT routing video through the (ephemeral) GPU node.
        self._hls_port = hls_port
        self._proc: subprocess.Popen[bytes] | None = None
        self._logfile: IO[bytes] | None = None
        self._sig: str | None = None
        self._paths: dict[str, str] = {}  # path → source rtsp (currently served)
        self._healthy = False
        self._lock = threading.Lock()

    # === public API ===

    def sync(self, cameras: list[tuple[str, str]]) -> bool:
        """Serve ``cameras`` = ``[(mediamtx_path, camera_rtsp), …]`` via the hub.

        Returns True if the hub is up and serving these paths (so callers may
        route through it), False to fall back to direct camera connections.
        Never raises.
        """
        with self._lock:
            try:
                return self._sync_locked(cameras)
            except Exception as e:  # noqa: BLE001 — best-effort; degrade to direct
                log.warning("local_mediamtx.sync_failed", error=str(e))
                self._healthy = False
                self._sig = None
                return False

    def local_url(self, path: str | None) -> str | None:
        """Loopback RTSP URL for a path the hub is actively serving, else None."""
        if not path:
            return None
        with self._lock:
            if self._healthy and path in self._paths:
                return f"rtsp://{_HOST}:{self._rtsp_port}/{path}"
            return None

    def hls_local_base(self) -> str | None:
        """The loopback HLS base a cloudflared tunnel should point at, or None when
        the hub isn't up. The cloud frontend reaches `{tunnel}/{path}/index.m3u8`."""
        with self._lock:
            if not self._healthy:
                return None
            return f"http://{_HOST}:{self._hls_port}"

    def status(self) -> dict[str, object]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return {
                "running": running,
                "healthy": self._healthy,
                "paths": sorted(self._paths),
                "rtsp_port": self._rtsp_port,
                "hls_port": self._hls_port,
            }

    def stop(self) -> None:
        with self._lock:
            self._stop_proc()
            self._healthy = False
            self._sig = None
            self._paths = {}

    # === internals (call with lock held) ===

    def _sync_locked(self, cameras: list[tuple[str, str]]) -> bool:
        if not self._exe or not Path(self._exe).exists():
            # No binary → feature unavailable; callers use direct URLs.
            self._healthy = False
            return False

        wanted = {p: r for p, r in cameras if p and r}
        if not wanted:
            # Nothing to fan out — tear the hub down so we don't burn a process.
            self._stop_proc()
            self._healthy = False
            self._sig = None
            self._paths = {}
            return False

        sig = _signature(wanted, self._rtsp_port, self._api_port, self._hls_port)
        running = self._proc is not None and self._proc.poll() is None
        if running and self._healthy and sig == self._sig:
            return True  # already serving exactly this set

        self._write_config(wanted)
        self._restart_proc()
        ok = self._wait_healthy()
        self._healthy = ok
        self._sig = sig if ok else None
        self._paths = wanted if ok else {}
        if ok:
            log.info("local_mediamtx.serving", count=len(wanted), port=self._rtsp_port)
        else:
            log.warning("local_mediamtx.unhealthy", count=len(wanted))
        return ok

    def _write_config(self, paths: dict[str, str]) -> None:
        lines = [
            "# Generated by sentry-agent-pc — local camera fan-out hub. Do not edit.",
            "logLevel: info",
            "logDestinations: [stdout]",
            "readTimeout: 10s",
            "writeTimeout: 10s",
            "api: yes",
            f"apiAddress: {_HOST}:{self._api_port}",
            "rtsp: yes",
            f"rtspAddress: {_HOST}:{self._rtsp_port}",
            "rtspTransports: [tcp]",
            # HLS on loopback for the cloudflared tunnel → cloud frontend.
            # lowLatency variant with ~200ms PARTS cuts glass-to-glass latency to
            # ~1s (vs ~4-7s for mpegts). Its `?session` keying used to break through
            # the backend HLS proxy (each of our multiple workers = a different IP,
            # and the session is IP-bound); that's gone now — /live 307-REDIRECTS
            # the player straight to this tunnel, so hls.js is ONE client and the
            # session stays valid. allowOrigin '*' for the cross-origin fetch.
            "hls: yes",
            f"hlsAddress: {_HOST}:{self._hls_port}",
            "hlsVariant: lowLatency",
            "hlsSegmentCount: 7",
            "hlsSegmentDuration: 1s",
            "hlsPartDuration: 200ms",
            "hlsAllowOrigin: '*'",
            "hlsAlwaysRemux: no",
            "webrtc: no",
            "rtmp: no",
            "srt: no",
            "paths:",
        ]
        for path, source in paths.items():
            lines.append(f"  {_yaml_dquote(path)}:")
            lines.append(f"    source: {_yaml_dquote(source)}")
            # Pull over TCP (reliable on busy LANs); only connect while a reader
            # is attached, and hold the camera ~10s after the last reader leaves
            # so a momentary gap (window reopen) doesn't re-handshake the camera.
            # (rtspTransport is the v1.18 name; the old sourceProtocol is
            # deprecated. Verified accepted by the pinned binary.)
            lines.append("    rtspTransport: tcp")
            lines.append("    sourceOnDemand: yes")
            lines.append("    sourceOnDemandCloseAfter: 10s")
        self._config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _restart_proc(self) -> None:
        self._stop_proc()
        assert self._exe is not None
        # Truncate the log each start so it can't grow unbounded over a long run.
        self._logfile = self._log_path.open("wb")  # noqa: SIM115 — closed in _stop_proc
        # Windows-only flag: guard on os.name, not hasattr — the attribute exists
        # on every modern OS, so hasattr would set the flag on Linux/Mac too →
        # Popen ValueError (bites dev/test).
        creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                [self._exe, str(self._config_path)],
                stdin=subprocess.DEVNULL,
                stdout=self._logfile,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception:
            # Popen failed (bad exe, OS limit): don't leak the logfile handle we
            # just opened — sync()'s except-path doesn't call _stop_proc().
            with contextlib.suppress(Exception):
                self._logfile.close()
            self._logfile = None
            raise
        log.info("local_mediamtx.started", pid=self._proc.pid)

    def _stop_proc(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                # terminate() ignored or timed out → force-kill, else the leaked
                # child keeps holding the loopback ports and the NEXT restart
                # bind-fails, locking us into direct-connection fallback for good.
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=3)
        self._proc = None
        if self._logfile is not None:
            with contextlib.suppress(Exception):
                self._logfile.close()
            self._logfile = None

    def _wait_healthy(self) -> bool:
        """Poll the API until it answers (MediaMTX is up) or we time out."""
        url = f"http://{_HOST}:{self._api_port}/v3/paths/list"
        deadline = time.monotonic() + _HEALTH_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return False  # process died on startup (port clash, bad config)
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310 — loopback only
                    if resp.status == 200:
                        return True
            except Exception:  # noqa: BLE001 — not up yet; keep polling
                pass
            time.sleep(_HEALTH_POLL_SEC)
        return False
