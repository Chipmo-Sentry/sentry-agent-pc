"""Push LAN camera RTSP streams to the cloud MediaMTX (publish), supervised.

In the cloud topology the AI runs centrally, but the cameras live on the
store LAN and are unreachable from the internet. So the agent relays each
camera to the cloud: one `ffmpeg -c copy` process per camera that reads the
LAN RTSP and republishes it to `rtsp://<cloud>/<mediamtx_path>`. `-c copy`
means no re-encode — negligible CPU, just remux + network.

Each push runs on its own thread that (re)launches ffmpeg and restarts it
with backoff whenever it exits (camera reboot, network blip, etc).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.redact import scrub_credentials
from sentry_agent_pc.resources import resolve_ffmpeg_exe
from sentry_agent_pc.settings import get_settings

log = get_logger("sentry_agent_pc.streaming.pusher")

_RESTART_MIN_SEC = 2.0
_RESTART_MAX_SEC = 30.0
# If ffmpeg ran at least this long it counts as "healthy" — reset the backoff
# so an occasional blip after hours of uptime doesn't permanently slow restarts.
_HEALTHY_RUN_SEC = 30.0
# Grace period to let a signalled ffmpeg child exit on its own before we
# force-kill it (so it can't survive app exit still publishing to the cloud).
_STOP_GRACE_SEC = 3.0
# Windows: hide the ffmpeg console window when launched from the GUI .exe.
_CREATE_NO_WINDOW = 0x08000000


def _reap_proc(proc: subprocess.Popen[bytes] | None) -> None:
    """terminate → wait(3s) → kill an ffmpeg child so it can't outlive the app.

    Mirrors ``local_mediamtx._stop_proc``: a bare ``terminate()`` with no kill
    escalation can leave a wedged ffmpeg child alive, still publishing to the
    cloud after the agent exits.
    """
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=_STOP_GRACE_SEC)
    except Exception:  # noqa: BLE001 — terminate ignored/timed out → force-kill
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_STOP_GRACE_SEC)


@dataclass(slots=True)
class PushTarget:
    """One camera to relay: LAN source → cloud path."""

    mediamtx_path: str        # destination path on the cloud MediaMTX
    lan_rtsp: str             # source RTSP on the store LAN (creds embedded)
    codec: str | None = None  # source codec; "hevc"/"h265" → transcode to H.264

    @property
    def needs_transcode(self) -> bool:
        # Browsers can't play H.265 over WebRTC/HLS → re-encode to H.264.
        return (self.codec or "").lower() in ("hevc", "h265")


@dataclass
class _PushState:
    target: PushTarget
    thread: threading.Thread
    stop: threading.Event
    running: bool = False
    restarts: int = 0
    last_error: str | None = None
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)


def build_push_url(base: str, path: str, user: str | None, password: str | None) -> str:
    """Compose the cloud publish URL: rtsp://[user:pass@]host[:port]/path."""
    base = base.rstrip("/")
    if user:
        scheme, _, rest = base.partition("://")
        cred = urllib.parse.quote(user, safe="")
        if password:
            cred += ":" + urllib.parse.quote(password, safe="")
        base = f"{scheme}://{cred}@{rest}"
    return f"{base}/{path}"


def build_relay_cmd(ffmpeg: str, target: PushTarget, dest: str) -> list[str]:
    """ffmpeg argv for one camera relay: LAN source → cloud publish.

    Always `-map 0:v:0`: take ONLY the first video stream, dropping audio and
    any extra data/metadata track. Some cameras (e.g. UNV) ship a "Generic"
    metadata track alongside H.264 that trips the browser's WebRTC — a bare
    `-c copy` would forward it. Mapping the single video track keeps the
    published stream clean for ALL cameras at near-zero cost.

      * H.264 source → `-c copy` (remux only, negligible CPU)
      * H.265 source → re-encode to H.264 (browsers can't play HEVC over WebRTC)
    """
    if target.needs_transcode:
        codec_args = [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
        ]
    else:
        codec_args = ["-c:v", "copy"]
    return [
        ffmpeg,
        "-nostdin",
        "-rtsp_transport", "tcp",
        "-i", target.lan_rtsp,
        "-map", "0:v:0",
        "-an",
        *codec_args,
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        dest,
    ]


class StreamPusher:
    """Supervises one ffmpeg relay per camera. Thread-safe start/stop."""

    def __init__(
        self,
        push_base: str,
        *,
        publish_user: str | None = None,
        publish_pass: str | None = None,
    ) -> None:
        self.push_base = push_base
        self.publish_user = publish_user
        self.publish_pass = publish_pass
        self._states: dict[str, _PushState] = {}
        self._lock = threading.Lock()

    # === public API ===

    def sync(self, targets: list[PushTarget]) -> None:
        """Reconcile running relays with `targets` — start new, stop removed."""
        wanted = {t.mediamtx_path: t for t in targets}
        with self._lock:
            # Stop relays no longer wanted.
            for path in list(self._states):
                if path not in wanted:
                    self._stop_locked(path)
            # Start relays not yet running (or restart if source changed).
            for path, target in wanted.items():
                st = self._states.get(path)
                if st is not None and st.target.lan_rtsp == target.lan_rtsp:
                    continue
                if st is not None:
                    self._stop_locked(path)
                self._start_locked(target)

    def stop_all(self) -> None:
        # Signal every relay to stop and reap its ffmpeg child, then JOIN the
        # relay threads (bounded) so no child survives this call still
        # publishing to the cloud. Threads are daemon, so without the join app
        # exit could race the kill and orphan ffmpeg.
        with self._lock:
            threads = [st.thread for st in self._states.values()]
            for path in list(self._states):
                self._stop_locked(path)
        for thread in threads:
            if thread is not None:
                thread.join(timeout=_STOP_GRACE_SEC + 1.0)

    def status(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {
                    "path": st.target.mediamtx_path,
                    "running": st.running,
                    "restarts": st.restarts,
                    # Scrub creds — last_error is surfaced to the GUI/heartbeat.
                    "last_error": (
                        scrub_credentials(st.last_error)
                        if st.last_error is not None
                        else None
                    ),
                }
                for st in self._states.values()
            ]

    # === internals (call with lock held) ===

    def _start_locked(self, target: PushTarget) -> None:
        stop = threading.Event()
        st = _PushState(target=target, thread=None, stop=stop)  # type: ignore[arg-type]
        thread = threading.Thread(
            target=self._run_relay,
            args=(st,),
            name=f"push-{target.mediamtx_path}",
            daemon=True,
        )
        st.thread = thread
        self._states[target.mediamtx_path] = st
        thread.start()
        log.info("pusher.started", path=target.mediamtx_path)

    def _stop_locked(self, path: str) -> None:
        st = self._states.pop(path, None)
        if st is None:
            return
        st.stop.set()
        _reap_proc(st.proc)
        log.info("pusher.stopped", path=path)

    def _run_relay(self, st: _PushState) -> None:
        dest = build_push_url(
            self.push_base, st.target.mediamtx_path, self.publish_user, self.publish_pass
        )
        cmd = build_relay_cmd(resolve_ffmpeg_exe(get_settings().ffmpeg_path), st.target, dest)
        try:
            self._relay_loop(st, cmd)
        finally:
            # Whatever ended this thread (stop, missing binary, crash), make sure
            # the ffmpeg child is gone before the thread exits so it can't
            # outlive the app still publishing to the cloud.
            _reap_proc(st.proc)
            st.running = False

    def _relay_loop(self, st: _PushState, cmd: list[str]) -> None:
        backoff = _RESTART_MIN_SEC
        while not st.stop.is_set():
            try:
                # Windows-only flag: only set it on Windows. The attribute exists
                # on every modern OS, so a hasattr guard would set the flag on
                # Linux/Mac too → Popen ValueError (bites dev/test).
                creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
                st.proc = proc
                st.running = True
                started_at = time.monotonic()
                log.info("pusher.ffmpeg_up", path=st.target.mediamtx_path)
                _, err = proc.communicate()
                st.running = False
                if st.stop.is_set():
                    break
                # A healthy long run means the next failure is a fresh blip —
                # reset the backoff so we reconnect fast, not at the 30s cap.
                if time.monotonic() - started_at >= _HEALTHY_RUN_SEC:
                    backoff = _RESTART_MIN_SEC
                tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
                # Scrub the ffmpeg stderr tail: it contains the full
                # rtsp://user:pass@host source URL, and last_error is both logged
                # and surfaced to the GUI/heartbeat via status().
                st.last_error = scrub_credentials(
                    " | ".join(tail) if tail else f"exit {proc.returncode}"
                )
                log.warning("pusher.ffmpeg_exit", path=st.target.mediamtx_path,
                            code=proc.returncode, err=st.last_error[:200])
            except FileNotFoundError:
                st.running = False
                st.last_error = "ffmpeg олдсонгүй (PATH-д нэмэх эсвэл FFMPEG_PATH тохируулах)"
                log.error("pusher.ffmpeg_missing", path=st.target.mediamtx_path)
                return  # no point retrying a missing binary
            except OSError as e:
                st.running = False
                st.last_error = scrub_credentials(str(e))
            # Backoff before restart.
            st.stop.wait(backoff)
            backoff = min(backoff * 2, _RESTART_MAX_SEC)
            if not st.stop.is_set():
                st.restarts += 1
