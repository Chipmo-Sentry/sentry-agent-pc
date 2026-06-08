"""Offline LAN live-view grid math (pure logic — no Tk/cv2 needed)."""

from __future__ import annotations

from sentry_agent_pc.gui.local_view import grid_dims


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
