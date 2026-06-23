"""Pure model + view-transform for the floor-plan editor (docs/30 Phase A).

Tk-free so it unit-tests headlessly. Holds the plan↔screen transform (pan/zoom)
and the fixture/camera presentation metadata. The plan itself is plain dicts that
mirror the backend `FloorPlan` schema, so save/load is a straight JSON pass.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fixture type → (colour, Mongolian label). Mirrors the agent zone editor +
# frontend overlay so a zone reads the same everywhere.
FIXTURE_STYLE: dict[str, tuple[str, str]] = {
    "exit": ("#E5484D", "Гарц"),
    "shelf": ("#3DD56D", "Тавиур"),
    "checkout": ("#E0A82E", "Касс"),
    "entrance": ("#3B82F6", "Орц"),
}
WALL_COLOR = "#9CA3AF"
CAMERA_COLOR = "#FF8A1F"  # brand orange
_FALLBACK = ("#9CA3AF", "Бүс")

# Default plan canvas logical size (relative units; matches backend default).
DEFAULT_PLAN_SIZE = (1000.0, 800.0)


def fixture_color(ftype: str) -> str:
    return FIXTURE_STYLE.get(ftype, _FALLBACK)[0]


def fixture_label(ftype: str) -> str:
    return FIXTURE_STYLE.get(ftype, _FALLBACK)[1]


@dataclass
class ViewTransform:
    """Maps plan-logical coordinates ↔ canvas screen pixels (pan + uniform zoom).

    screen = (plan - pan) * zoom ;  plan = screen / zoom + pan.
    `pan` is a plan-space offset (the plan point shown at the canvas origin)."""

    pan_x: float = 0.0
    pan_y: float = 0.0
    zoom: float = 1.0

    def to_screen(self, px: float, py: float) -> tuple[float, float]:
        return ((px - self.pan_x) * self.zoom, (py - self.pan_y) * self.zoom)

    def to_plan(self, sx: float, sy: float) -> tuple[float, float]:
        z = self.zoom or 1e-9
        return (sx / z + self.pan_x, sy / z + self.pan_y)

    def pan_by_screen(self, dsx: float, dsy: float) -> None:
        """Pan by a screen-pixel delta (drag): content follows the cursor."""
        self.pan_x -= dsx / (self.zoom or 1e-9)
        self.pan_y -= dsy / (self.zoom or 1e-9)

    def zoom_at(self, sx: float, sy: float, factor: float) -> None:
        """Zoom by `factor`, keeping the plan point currently under (sx,sy) fixed
        (zoom toward the cursor). Clamped to a sane range."""
        px, py = self.to_plan(sx, sy)
        self.zoom = max(0.05, min(20.0, self.zoom * factor))
        # Solve pan so to_screen(px,py) == (sx,sy) again.
        self.pan_x = px - sx / self.zoom
        self.pan_y = py - sy / self.zoom

    def fit(self, plan_w: float, plan_h: float, screen_w: float, screen_h: float) -> None:
        """Center + scale the plan to fit the canvas (with a small margin)."""
        if plan_w <= 0 or plan_h <= 0 or screen_w <= 0 or screen_h <= 0:
            return
        self.zoom = min(screen_w / plan_w, screen_h / plan_h) * 0.9
        self.pan_x = (plan_w - screen_w / self.zoom) / 2.0
        self.pan_y = (plan_h - screen_h / self.zoom) / 2.0


def angle_deg(cx: float, cy: float, px: float, py: float) -> float:
    """Direction angle (degrees, 0 = +x / right, growing clockwise in screen
    space where y is down) from a camera centre (cx,cy) to a handle (px,py)."""
    import math

    return math.degrees(math.atan2(py - cy, px - cx)) % 360.0


def dir_handle(cx: float, cy: float, dir_deg: float, length: float) -> tuple[float, float]:
    """The point `length` away from (cx,cy) along `dir_deg` — the draggable
    direction handle (inverse of :func:`angle_deg`)."""
    import math

    r = math.radians(dir_deg)
    return (cx + math.cos(r) * length, cy + math.sin(r) * length)
