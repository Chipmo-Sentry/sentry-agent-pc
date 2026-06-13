"""Offline LAN live-view grid math (pure logic — no Tk/cv2 needed)."""

from __future__ import annotations

from sentry_agent_pc.gui.local_view import _reader_urls, grid_dims


def test_reader_urls_prefers_local_fanout() -> None:
    # Hikvision main → sub fallback is derived; local hub URL goes first.
    main = "rtsp://u:p@cam/Streaming/Channels/101"
    local = "rtsp://127.0.0.1:18554/cam1"
    urls = _reader_urls(main, local)
    assert urls[0] == local  # share the single pull
    assert main in urls       # direct main still a fallback
    assert "rtsp://u:p@cam/Streaming/Channels/102" in urls  # sub fallback too


def test_reader_urls_without_local_is_direct_only() -> None:
    main = "rtsp://u:p@cam/stream1"
    urls = _reader_urls(main, None)
    assert urls == ["rtsp://u:p@cam/stream2", main]  # sub-first, then main


def test_grid_dims_layout() -> None:
    assert grid_dims(0) == (1, 1)   # empty → harmless 1x1
    assert grid_dims(1) == (1, 1)
    assert grid_dims(2) == (2, 1)
    assert grid_dims(3) == (2, 2)
    assert grid_dims(4) == (2, 2)
    assert grid_dims(5) == (2, 3)   # 5 cams → 2 cols, 3 rows


def test_grid_dims_custom_cols() -> None:
    assert grid_dims(6, max_cols=3) == (3, 2)
    assert grid_dims(2, max_cols=3) == (2, 1)
