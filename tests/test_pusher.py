"""StreamPusher URL building + relay reconciliation (no real ffmpeg spawned)."""

from __future__ import annotations

import subprocess
import threading

from sentry_agent_pc.redact import scrub_credentials
from sentry_agent_pc.streaming.pusher import (
    PushTarget,
    StreamPusher,
    _PushState,
    _reap_proc,
    build_push_url,
    build_relay_cmd,
)


def test_build_push_url_no_creds() -> None:
    assert build_push_url("rtsp://mtx:8554", "cam1", None, None) == "rtsp://mtx:8554/cam1"


def test_relay_cmd_h264_copies_single_video_track() -> None:
    """H.264 → remux only (-c:v copy), but ALWAYS map a single video track +
    drop audio so a camera's extra 'Generic'/data track can't trip WebRTC."""
    t = PushTarget("cam1", "rtsp://lan/1", codec="h264")
    cmd = build_relay_cmd("ffmpeg", t, "rtsp://mtx:8554/cam1")
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "-an" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "libx264" not in cmd  # no re-encode for H.264
    assert cmd[-1] == "rtsp://mtx:8554/cam1"


def test_relay_cmd_h265_transcodes_to_h264() -> None:
    for codec in ("hevc", "h265", "HEVC"):
        t = PushTarget("cam1", "rtsp://lan/1", codec=codec)
        cmd = build_relay_cmd("ffmpeg", t, "rtsp://mtx:8554/cam1")
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v:0"


def test_relay_cmd_unknown_codec_defaults_to_copy() -> None:
    t = PushTarget("cam1", "rtsp://lan/1", codec=None)
    cmd = build_relay_cmd("ffmpeg", t, "rtsp://mtx:8554/cam1")
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_build_push_url_with_creds_urlencoded() -> None:
    url = build_push_url("rtsp://mtx:8554/", "cam1", "pub", "p@ss/word")
    assert url == "rtsp://pub:p%40ss%2Fword@mtx:8554/cam1"


def test_build_push_url_user_only() -> None:
    assert build_push_url("rtsp://mtx:8554", "c", "pub", None) == "rtsp://pub@mtx:8554/c"


def test_sync_starts_and_stops_relays(monkeypatch) -> None:
    """sync() should spawn a relay thread per target and stop removed ones —
    we stub the relay loop so no ffmpeg is launched."""
    started: list[str] = []
    release = threading.Event()

    def fake_relay(self, st) -> None:  # noqa: ANN001
        started.append(st.target.mediamtx_path)
        st.running = True
        release.wait(2.0)  # block until test tears down

    monkeypatch.setattr(StreamPusher, "_run_relay", fake_relay)

    pusher = StreamPusher("rtsp://mtx:8554")
    pusher.sync([PushTarget("cam1", "rtsp://lan/1"), PushTarget("cam2", "rtsp://lan/2")])
    # Both relays registered.
    paths = {s["path"] for s in pusher.status()}
    assert paths == {"cam1", "cam2"}

    # Removing cam2 from targets stops its relay.
    pusher.sync([PushTarget("cam1", "rtsp://lan/1")])
    paths = {s["path"] for s in pusher.status()}
    assert paths == {"cam1"}

    release.set()
    pusher.stop_all()
    assert pusher.status() == []
    assert "cam1" in started and "cam2" in started


def test_sync_restarts_relay_on_codec_change(monkeypatch) -> None:
    """Same URL but a flipped codec (h264↔hevc) must restart the relay so the
    copy-vs-transcode argv is rebuilt — else the stream silently won't play."""
    started: list[tuple[str, str | None]] = []
    release = threading.Event()

    def fake_relay(self, st) -> None:  # noqa: ANN001
        started.append((st.target.mediamtx_path, st.target.codec))
        st.running = True
        release.wait(2.0)

    monkeypatch.setattr(StreamPusher, "_run_relay", fake_relay)

    pusher = StreamPusher("rtsp://mtx:8554")
    pusher.sync([PushTarget("cam1", "rtsp://lan/1", codec="h264")])
    pusher.sync([PushTarget("cam1", "rtsp://lan/1", codec="hevc")])  # same URL, new codec
    release.set()
    pusher.stop_all()
    assert started == [("cam1", "h264"), ("cam1", "hevc")]  # restarted, not skipped


# ── credential scrubbing (#8) ───────────────────────────────────────────────


def test_scrub_credentials_strips_userinfo() -> None:
    assert (
        scrub_credentials("rtsp://admin:s3cret@10.0.0.5:554/Streaming")
        == "rtsp://***@10.0.0.5:554/Streaming"
    )
    # User-only and http schemes too.
    assert scrub_credentials("rtsp://user@host/x") == "rtsp://***@host/x"
    assert scrub_credentials("http://u:p@h/x") == "http://***@h/x"


def test_scrub_credentials_none_and_empty_safe() -> None:
    assert scrub_credentials(None) == ""
    assert scrub_credentials("") == ""
    # No creds → unchanged.
    assert scrub_credentials("rtsp://10.0.0.5/cam1") == "rtsp://10.0.0.5/cam1"


def test_scrub_credentials_keeps_path_at_signs() -> None:
    # An ``@`` in the path (after the host) must NOT be treated as userinfo.
    assert scrub_credentials("rtsp://host/path@v=1") == "rtsp://host/path@v=1"


def test_status_does_not_leak_credentials() -> None:
    """status() must scrub the rtsp creds an ffmpeg error tail would carry."""
    pusher = StreamPusher("rtsp://mtx:8554")
    stop = threading.Event()
    # Register a fake relay state directly with a leaky last_error.

    leaky = "Connection to rtsp://admin:hunter2@10.0.0.9:554/h264 failed"
    state = _PushState(
        target=PushTarget("cam1", "rtsp://admin:hunter2@10.0.0.9:554/h264"),
        thread=threading.Thread(target=lambda: None),
        stop=stop,
        last_error=leaky,
    )
    pusher._states["cam1"] = state  # type: ignore[attr-defined]

    rows = pusher.status()
    assert len(rows) == 1
    last_error = rows[0]["last_error"]
    assert isinstance(last_error, str)
    assert "hunter2" not in last_error
    assert "***@10.0.0.9" in last_error


def test_status_last_error_none_stays_none() -> None:
    pusher = StreamPusher("rtsp://mtx:8554")

    state = _PushState(
        target=PushTarget("cam1", "rtsp://lan/1"),
        thread=threading.Thread(target=lambda: None),
        stop=threading.Event(),
        last_error=None,
    )
    pusher._states["cam1"] = state  # type: ignore[attr-defined]
    assert pusher.status()[0]["last_error"] is None


# ── orphaned ffmpeg on stop (#7) ────────────────────────────────────────────


class _FakeProc:
    """Minimal Popen stand-in: terminate() is ignored, only kill() ends it."""

    def __init__(self, *, dies_on_terminate: bool) -> None:
        self._dies_on_terminate = dies_on_terminate
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        if self._dies_on_terminate:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        if self._alive:
            # terminate() was ignored — mimic a hung child that doesn't exit.
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)
        return 0


def test_reap_proc_escalates_to_kill_when_terminate_ignored() -> None:
    proc = _FakeProc(dies_on_terminate=False)
    _reap_proc(proc)  # type: ignore[arg-type]
    assert proc.terminated is True
    assert proc.killed is True  # terminate timed out → force-killed
    assert proc.poll() is not None


def test_reap_proc_no_kill_when_terminate_works() -> None:
    proc = _FakeProc(dies_on_terminate=True)
    _reap_proc(proc)  # type: ignore[arg-type]
    assert proc.terminated is True
    assert proc.killed is False  # clean terminate → no escalation needed


def test_reap_proc_noop_on_dead_or_none() -> None:
    _reap_proc(None)  # must not raise
    dead = _FakeProc(dies_on_terminate=True)
    dead.kill()  # already dead
    dead.killed = False
    _reap_proc(dead)  # type: ignore[arg-type]
    assert dead.terminated is False  # never touched an already-dead proc


def test_stop_locked_kills_hung_ffmpeg() -> None:
    """_stop_locked must reap (terminate→kill) a relay's wedged ffmpeg child."""
    pusher = StreamPusher("rtsp://mtx:8554")

    proc = _FakeProc(dies_on_terminate=False)
    state = _PushState(
        target=PushTarget("cam1", "rtsp://lan/1"),
        thread=threading.Thread(target=lambda: None),
        stop=threading.Event(),
        proc=proc,  # type: ignore[arg-type]
    )
    pusher._states["cam1"] = state  # type: ignore[attr-defined]

    with pusher._lock:  # type: ignore[attr-defined]
        pusher._stop_locked("cam1")  # type: ignore[attr-defined]

    assert state.stop.is_set()
    assert proc.killed is True
    assert pusher.status() == []


def test_force_transcode_overrides_copy() -> None:
    # An H.264 camera whose probe found B-frames must re-encode: `-c copy`
    # would crash the cloud HLS muxer («too many reordered frames»).
    t = PushTarget(mediamtx_path="cam", lan_rtsp="rtsp://x", codec="h264", force_transcode=True)
    cmd = build_relay_cmd("ffmpeg", t, "rtsp://dest/cam")
    assert "libx264" in cmd
    assert "copy" not in cmd
