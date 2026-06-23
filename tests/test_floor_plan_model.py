"""Pure floor-plan model (docs/30): plan↔screen transform + camera direction maths."""

from __future__ import annotations

import math

from sentry_agent_pc.gui.floor_plan_model import (
    ViewTransform,
    angle_deg,
    dir_handle,
    fixture_color,
    fixture_label,
)


def test_transform_round_trip() -> None:
    v = ViewTransform(pan_x=50, pan_y=20, zoom=2.0)
    sx, sy = v.to_screen(100, 80)
    px, py = v.to_plan(sx, sy)
    assert px == 100 and py == 80


def test_zoom_keeps_cursor_point_fixed() -> None:
    v = ViewTransform(zoom=1.0)
    # The plan point under the cursor must stay under the cursor after zoom.
    before = v.to_plan(300, 200)
    v.zoom_at(300, 200, 1.5)
    after = v.to_plan(300, 200)
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert math.isclose(v.zoom, 1.5)


def test_zoom_clamped() -> None:
    v = ViewTransform(zoom=10.0)
    for _ in range(20):
        v.zoom_at(0, 0, 2.0)
    assert v.zoom <= 20.0
    for _ in range(40):
        v.zoom_at(0, 0, 0.5)
    assert v.zoom >= 0.05


def test_pan_by_screen() -> None:
    v = ViewTransform(zoom=2.0)
    # Dragging right by 20 screen px moves the plan-origin left by 10 plan units.
    v.pan_by_screen(20, 0)
    assert v.pan_x == -10.0


def test_fit_centers_plan() -> None:
    v = ViewTransform()
    v.fit(1000, 800, 500, 400)  # plan 1000x800 into 500x400 → zoom 0.45 (×0.9)
    assert math.isclose(v.zoom, 0.45)
    # Plan center maps to screen center.
    cx, cy = v.to_screen(500, 400)
    assert math.isclose(cx, 250.0)
    assert math.isclose(cy, 200.0)


def test_angle_and_handle_inverse() -> None:
    # A handle directly right of the camera → 0°; round-trips through dir_handle.
    assert math.isclose(angle_deg(100, 100, 130, 100), 0.0)
    assert math.isclose(angle_deg(100, 100, 100, 130) % 360, 90.0)  # down = +y
    hx, hy = dir_handle(100, 100, 0.0, 26)
    assert math.isclose(hx, 126.0) and math.isclose(hy, 100.0)
    a = angle_deg(100, 100, *dir_handle(100, 100, 217.0, 26))
    assert math.isclose(a, 217.0, abs_tol=1e-6)


def test_fixture_style() -> None:
    assert fixture_label("exit") == "Гарц"
    assert fixture_color("shelf").startswith("#")
    assert fixture_label("aisle") == "Бүс"  # unknown fallback
