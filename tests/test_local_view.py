"""Offline LAN live-view grid math (pure logic — no Tk/cv2 needed)."""

from __future__ import annotations

import time

import pytest

from sentry_agent_pc.gui import local_view as lv
from sentry_agent_pc.gui.local_view import _CameraReader, _reader_urls, grid_dims


def test_edge_error_holder_is_per_camera() -> None:
    """One camera recovering must not wipe another camera's live edge error."""
    lv._edge_errors.clear()
    lv.record_edge_error("camA", "boom A")
    lv.record_edge_error("camB", "boom B")
    assert lv.last_edge_error() in {"boom A", "boom B"}
    lv.clear_edge_error("camA")  # A recovers
    assert lv.last_edge_error() == "boom B"  # B's error survives
    lv.clear_edge_error("camB")
    assert lv.last_edge_error() is None
    lv.clear_edge_error("camB")  # idempotent — clearing an absent key is fine


def test_store_skips_corrupt_frame_without_crashing() -> None:
    """A 0-dimension / corrupt decode must be dropped, not crash the reader
    thread (the old box_w/0 ZeroDivisionError froze the tile)."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    r = _CameraReader("cam", ["rtsp://x/1"])
    r.set_box(320, 180)
    r._store(np.zeros((0, 0, 3), dtype=np.uint8))  # corrupt → skipped
    img, seq = r.latest()
    assert img is None and seq == 0
    r._store(np.zeros((48, 64, 3), dtype=np.uint8))  # valid → stored
    img2, seq2 = r.latest()
    assert img2 is not None and seq2 == 1


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


def test_tile_double_click_binds_video_and_holder() -> None:
    """The double-click handler must reach the inner video Label + holder, not
    just the outer frame — otherwise clicking the actual video area does nothing
    (the children swallow the event; Tk doesn't bubble it to the parent)."""
    ctk = pytest.importorskip("customtkinter")
    try:
        root = ctk.CTk()
    except Exception:  # noqa: BLE001 — no display (headless) → skip
        pytest.skip("no Tk display available")
    try:
        root.withdraw()
        reader = _CameraReader("cam", ["rtsp://x/1"])
        from sentry_agent_pc.gui.local_view import _Tile

        tile = _Tile(root, reader)
        # Spy each covering widget's bind() — robust across Tk/CTk query-form
        # quirks: we assert the sequence was actually bound, not introspect Tcl.
        targets = (tile, tile._holder, tile._video)
        seqs: list[list[str]] = [[] for _ in targets]

        def make_spy(orig: object, sink: list[str]) -> object:
            def spy(sequence: object = None, command: object = None, *a: object, **k: object) -> object:
                if sequence is not None and command is not None:
                    sink.append(str(sequence))
                return orig(sequence, command, *a, **k)  # type: ignore[operator]
            return spy

        for w, sink in zip(targets, seqs, strict=True):
            w.bind = make_spy(w.bind, sink)  # type: ignore[method-assign]
        tile.bind_double_click(lambda _e: None)
        # Every widget that covers the tile must carry the binding.
        for sink in seqs:
            assert "<Double-Button-1>" in sink
    finally:
        root.destroy()


def test_reader_stops_and_joins_within_budget() -> None:
    """stop() must let a running reader thread exit promptly so _on_close() can
    join it (and release the VideoCapture) without hanging the UI on close."""
    pytest.importorskip("cv2")  # run() imports cv2 before its loop
    r = _CameraReader("cam", ["rtsp://127.0.0.1:1/none"])
    r.pause()  # idle cheaply (no VideoCapture) so the test is deterministic + fast
    r.start()
    time.sleep(0.05)
    r.stop()
    r.join(timeout=1.5)  # mirrors _on_close()'s bounded join
    assert not r.is_alive()
