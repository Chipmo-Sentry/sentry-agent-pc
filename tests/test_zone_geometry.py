"""Pure geometry + zone-type metadata for the zone editor (docs/29 P1a).

No Tk, no display — these run headless in CI and pin the normalization that keeps
drawn zones valid across resolutions (docs/29 risk #3)."""

from __future__ import annotations

import pytest

from sentry_agent_pc.gui.zone_geometry import (
    ZONE_TYPES,
    fit_rect,
    to_norm,
    to_px,
    zone_color,
    zone_label,
)

# === zone-type metadata ===


def test_zone_types_cover_backend_literal() -> None:
    keys = {z.key for z in ZONE_TYPES}
    assert keys == {"exit", "shelf", "checkout", "entrance", "fridge"}


def test_zone_label_and_color_known() -> None:
    assert zone_label("exit") == "Гарц"
    assert zone_color("exit").startswith("#")


def test_zone_label_color_unknown_fallback() -> None:
    # A newer backend type must not crash the editor — fall back gracefully.
    assert zone_label("aisle") == "aisle"
    assert zone_color("aisle").startswith("#")


# === fit_rect (letterbox) ===


def test_fit_rect_wide_image_letterboxes_vertically() -> None:
    # 1920x1080 (16:9) into a 800x800 box → full width, centered vertically.
    r = fit_rect(1920, 1080, 800, 800)
    assert r.disp_w == pytest.approx(800.0)
    assert r.disp_h == pytest.approx(450.0)
    assert r.off_x == pytest.approx(0.0)
    assert r.off_y == pytest.approx(175.0)


def test_fit_rect_tall_box_letterboxes_horizontally() -> None:
    # 1000x1000 into 400x800 → fits width 400, centered horizontally.
    r = fit_rect(1000, 1000, 400, 800)
    assert r.disp_w == pytest.approx(400.0)
    assert r.disp_h == pytest.approx(400.0)
    assert r.off_x == pytest.approx(0.0)
    assert r.off_y == pytest.approx(200.0)


def test_fit_rect_degenerate_is_zero() -> None:
    assert fit_rect(0, 100, 50, 50) == (0.0, 0.0, 0.0, 0.0)
    assert fit_rect(100, 100, 0, 50) == (0.0, 0.0, 0.0, 0.0)


# === normalize / denormalize round-trip ===


def test_norm_px_round_trip() -> None:
    r = fit_rect(1920, 1080, 800, 800)
    # A point at the image center should normalize to ~(0.5, 0.5).
    cx, cy = to_px(0.5, 0.5, r)
    nx, ny = to_norm(cx, cy, r)
    assert nx == pytest.approx(0.5)
    assert ny == pytest.approx(0.5)


def test_norm_corners() -> None:
    r = fit_rect(1000, 500, 1000, 1000)  # disp 1000x500, off_y=250
    # Top-left image corner.
    assert to_norm(r.off_x, r.off_y, r) == pytest.approx((0.0, 0.0))
    # Bottom-right image corner.
    assert to_norm(r.off_x + r.disp_w, r.off_y + r.disp_h, r) == pytest.approx((1.0, 1.0))


def test_norm_clamps_outside_image() -> None:
    r = fit_rect(1000, 500, 1000, 1000)  # letterbox margins top/bottom
    # A click ABOVE the image (in the top margin) clamps to y=0, not negative.
    nx, ny = to_norm(r.off_x + 10, r.off_y - 80, r)
    assert ny == 0.0
    assert 0.0 <= nx <= 1.0
    # A click far right of the image clamps to x=1.
    nx2, _ = to_norm(r.off_x + r.disp_w + 500, r.off_y + 10, r)
    assert nx2 == 1.0


def test_norm_zero_rect_safe() -> None:
    from sentry_agent_pc.gui.zone_geometry import FitRect

    assert to_norm(10, 10, FitRect(0, 0, 0, 0)) == (0.0, 0.0)
