"""Per-camera detection zones for the EDGE gate (docs/29) — point-in-polygon in
normalized 0-1 space. Torch-free, pure, unit-testable.

Mirrors the cloud worker's live_worker/zones.py so edge and cloud agree on which
zone a person stands in. Zones arrive as the agent's local CameraRecord.zones
(synced from the backend), a list of {type, points:[[x,y],...]} normalized polygons.
The edge normalizes each person's foot point by the live frame's own width/height
(docs/29 risk #3) before testing it here.
"""

from __future__ import annotations

# type -> list of polygons; each polygon is a list of (x, y) in 0-1.
CompiledZones = dict[str, list[list[tuple[float, float]]]]

_VALID_TYPES = frozenset({"exit", "shelf", "checkout", "entrance"})


def compile_zones(zones: list[dict[str, object]] | None) -> CompiledZones:
    """Group raw zone dicts into {type: [polygon, ...]} of (x,y) tuples.

    Reject the WHOLE polygon if any vertex is malformed / out of [0,1] — dropping
    a single bad vertex would silently RESHAPE the operator's zone into a phantom
    region (→ false signals). The backend validates on write, so out-of-range
    coords only arrive via a garbled payload. Never raises."""
    out: CompiledZones = {}
    for z in zones or []:
        if not isinstance(z, dict):
            continue
        ztype = z.get("type")
        if ztype not in _VALID_TYPES:
            continue
        pts_raw = z.get("points")
        if not isinstance(pts_raw, list) or len(pts_raw) < 3:
            continue
        poly: list[tuple[float, float]] = []
        valid = True
        for p in pts_raw:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                valid = False
                break
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                valid = False
                break
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                valid = False
                break
            poly.append((x, y))
        if valid and len(poly) >= 3:
            out.setdefault(str(ztype), []).append(poly)
    return out


def point_in_poly(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test (even-odd rule)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def zones_at(x: float, y: float, compiled: CompiledZones) -> set[str]:
    """Zone TYPES whose any polygon contains (x,y) in normalized 0-1 space."""
    hit: set[str] = set()
    for ztype, polys in compiled.items():
        if any(point_in_poly(x, y, poly) for poly in polys):
            hit.add(ztype)
    return hit
