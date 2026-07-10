"""Pure geometry + zone-type metadata for the zone editor (docs/29).

Kept Tk-free so it unit-tests headlessly (CI has no display). The editor draws on
a canvas that letterboxes the still frame; these helpers convert between canvas
pixels and the NORMALIZED 0-1 image space the backend stores. Normalization is
against the DISPLAYED image rectangle (docs/29 risk #3 — NOT the camera's native
resolution), so one zone definition is valid at any pull/draw resolution.
"""

from __future__ import annotations

from typing import NamedTuple


class ZoneType(NamedTuple):
    key: str  # backend Literal: exit | shelf | checkout | entrance
    label: str  # Mongolian UI label
    color: str  # outline/fill hex


# Order = the segmented picker order. Colours chosen to stay distinct over video.
ZONE_TYPES: tuple[ZoneType, ...] = (
    ZoneType("exit", "Гарц", "#E5484D"),  # red — concealment→exit is the key signal
    ZoneType("shelf", "Тавиур", "#3DD56D"),  # green
    ZoneType("checkout", "Касс", "#E0A82E"),  # amber
    ZoneType("entrance", "Орц", "#3B82F6"),  # blue
    ZoneType("fridge", "Хөргүүр", "#38BDF8"),  # cyan — item-taking area like shelf
)

_BY_KEY = {z.key: z for z in ZONE_TYPES}
_FALLBACK_COLOR = "#9CA3AF"  # gray — for an unknown type from a newer backend


def zone_label(key: str) -> str:
    z = _BY_KEY.get(key)
    return z.label if z else key


def zone_color(key: str) -> str:
    z = _BY_KEY.get(key)
    return z.color if z else _FALLBACK_COLOR


class FitRect(NamedTuple):
    """Where the letterboxed image sits on the canvas, in canvas pixels."""

    off_x: float
    off_y: float
    disp_w: float
    disp_h: float


def fit_rect(img_w: int, img_h: int, box_w: int, box_h: int) -> FitRect:
    """Letterbox an `img_w`×`img_h` image into a `box_w`×`box_h` canvas: scale to
    fit (keep aspect), centre. Returns the displayed rect in canvas pixels.

    Degenerate inputs (any dimension <= 0) collapse to a zero-size rect at the
    origin rather than dividing by zero — the caller treats that as "not ready"."""
    if img_w <= 0 or img_h <= 0 or box_w <= 0 or box_h <= 0:
        return FitRect(0.0, 0.0, 0.0, 0.0)
    scale = min(box_w / img_w, box_h / img_h)
    disp_w = img_w * scale
    disp_h = img_h * scale
    off_x = (box_w - disp_w) / 2.0
    off_y = (box_h - disp_h) / 2.0
    return FitRect(off_x, off_y, disp_w, disp_h)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def to_norm(px: float, py: float, rect: FitRect) -> tuple[float, float]:
    """Canvas pixel → normalized 0-1 image coord, clamped to the image rect.

    A click in the letterbox margin (outside the image) clamps onto the nearest
    edge, so a zone never gets a coord outside 0-1 (which the backend rejects)."""
    if rect.disp_w <= 0 or rect.disp_h <= 0:
        return (0.0, 0.0)
    nx = (px - rect.off_x) / rect.disp_w
    ny = (py - rect.off_y) / rect.disp_h
    return (_clamp01(nx), _clamp01(ny))


def to_px(nx: float, ny: float, rect: FitRect) -> tuple[float, float]:
    """Normalized 0-1 image coord → canvas pixel (inverse of :func:`to_norm`)."""
    return (rect.off_x + nx * rect.disp_w, rect.off_y + ny * rect.disp_h)
