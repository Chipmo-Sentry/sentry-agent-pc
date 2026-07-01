"""LocalMediaMTX fan-out hub — config gen, URL gating, sync reconcile.

No real MediaMTX process is spawned: the restart + health-check are stubbed so
the tests exercise the orchestration logic, not the binary.
"""

from __future__ import annotations

from pathlib import Path

from sentry_agent_pc.streaming.local_mediamtx import (
    LocalMediaMTX,
    _signature,
    _yaml_dquote,
)


def _hub(tmp_path: Path, *, with_exe: bool = True) -> LocalMediaMTX:
    exe: str | None = None
    if with_exe:
        exe_file = tmp_path / "mediamtx.exe"
        exe_file.write_bytes(b"stub")  # must merely exist
        exe = str(exe_file)
    return LocalMediaMTX(exe_path=exe, config_dir=tmp_path, rtsp_port=18554, api_port=19997)


def test_yaml_dquote_escapes_and_quotes() -> None:
    assert _yaml_dquote("192_168_1_64") == '"192_168_1_64"'
    assert _yaml_dquote('a"b\\c') == '"a\\"b\\\\c"'


def test_signature_changes_with_paths_and_ports() -> None:
    a = _signature({"c1": "rtsp://x/1"}, 18554, 19997, 18888)
    assert a == _signature({"c1": "rtsp://x/1"}, 18554, 19997, 18888)  # order-stable
    assert a != _signature({"c1": "rtsp://x/2"}, 18554, 19997, 18888)  # source changed
    assert a != _signature({"c1": "rtsp://x/1"}, 9999, 19997, 18888)  # rtsp port changed
    assert a != _signature({"c1": "rtsp://x/1"}, 18554, 19997, 28888)  # hls port changed


def test_local_url_gated_by_health_and_membership(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    hub._healthy = True
    hub._paths = {"cam1": "rtsp://x/1"}
    assert hub.local_url("cam1") == "rtsp://127.0.0.1:18554/cam1"
    assert hub.local_url("cam2") is None  # not served
    assert hub.local_url(None) is None
    hub._healthy = False
    assert hub.local_url("cam1") is None  # hub down → fall back to direct


def test_sync_without_binary_returns_false(tmp_path: Path) -> None:
    hub = _hub(tmp_path, with_exe=False)
    assert hub.sync([("cam1", "rtsp://x/1")]) is False
    assert hub.local_url("cam1") is None


def test_sync_with_no_cameras_returns_false(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    assert hub.sync([]) is False
    assert hub.sync([("", "")]) is False  # blanks filtered out


def test_sync_happy_path_serves_and_dedupes_restarts(tmp_path: Path, monkeypatch) -> None:
    hub = _hub(tmp_path)
    restarts: list[int] = []

    class _FakeProc:
        def poll(self) -> None:  # running
            return None

    def fake_restart(self: LocalMediaMTX) -> None:
        restarts.append(1)
        self._proc = _FakeProc()  # type: ignore[assignment]

    monkeypatch.setattr(LocalMediaMTX, "_restart_proc", fake_restart)
    monkeypatch.setattr(LocalMediaMTX, "_wait_healthy", lambda self: True)

    assert hub.sync([("cam1", "rtsp://u:p@host/1")]) is True
    assert hub.local_url("cam1") == "rtsp://127.0.0.1:18554/cam1"
    assert len(restarts) == 1

    # Same camera set → no second restart (signature guard).
    assert hub.sync([("cam1", "rtsp://u:p@host/1")]) is True
    assert len(restarts) == 1

    # Source changed → restart.
    assert hub.sync([("cam1", "rtsp://u:p@host/2")]) is True
    assert len(restarts) == 2

    # Config file reflects the latest source + on-demand pull.
    cfg = (tmp_path / "mediamtx.local.gen.yml").read_text(encoding="utf-8")
    assert '"cam1":' in cfg
    assert 'source: "rtsp://u:p@host/2"' in cfg
    assert "sourceOnDemand: yes" in cfg


def test_sync_unhealthy_falls_back(tmp_path: Path, monkeypatch) -> None:
    hub = _hub(tmp_path)

    class _FakeProc:
        def poll(self) -> None:
            return None

    monkeypatch.setattr(
        LocalMediaMTX,
        "_restart_proc",
        lambda self: setattr(self, "_proc", _FakeProc()),
    )
    monkeypatch.setattr(LocalMediaMTX, "_wait_healthy", lambda self: False)

    assert hub.sync([("cam1", "rtsp://x/1")]) is False
    assert hub.local_url("cam1") is None  # not served → callers use direct
