"""Regenerate the Windows app icon bundle from the brand SVG.

Renders EACH icon size natively from the vector (16/20/24/32/40/48/64/128/256)
— not a downscale of one big bitmap — so every size, including the small
taskbar/title-bar ones, is sharp. Writes:

  src/sentry_agent_pc/assets/icon.ico            (production: exe / title-bar / installer)
  src/sentry_agent_pc/assets/icon.png            (production: tray, 256px)
  src/sentry_agent_pc/assets/icons/windows/*.png (per-size PNGs)
  src/sentry_agent_pc/assets/icons/windows/app-icon.ico

Usage:
    uv run --with cairosvg python scripts/generate_icons.py

Needs a working SVG rasterizer. `cairosvg` is used here; on Windows it requires
the Cairo runtime (`libcairo-2.dll`). If Cairo isn't available, render each size
from the SVG with any rasterizer (e.g. a headless Chromium canvas) and feed the
PNGs to `_build_ico` — the per-size native render is the only thing that matters.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "sentry_agent_pc" / "assets"
SVG = ASSETS / "icons" / "source" / "chipmo-logo.svg"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def _render(svg_bytes: bytes, size: int) -> Image.Image:
    """Rasterize the SVG natively at `size`×`size` (crisp at every size)."""
    import cairosvg

    png = cairosvg.svg2png(bytestring=svg_bytes, output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _build_ico(layers: dict[int, Image.Image], ico_path: Path) -> None:
    """Embed each native per-size layer (NOT a resize of one image) into a .ico.

    Pillow drops sizes outside its default set unless `sizes` is passed
    explicitly, and would resize the base image unless each size is supplied via
    `append_images` — so we pass both.
    """
    ordered = [layers[s] for s in SIZES]
    ordered[-1].save(
        ico_path, format="ICO", sizes=[(s, s) for s in SIZES], append_images=ordered[:-1]
    )


def main() -> None:
    svg_bytes = SVG.read_bytes()
    layers = {s: _render(svg_bytes, s) for s in SIZES}

    win = ASSETS / "icons" / "windows"
    win.mkdir(parents=True, exist_ok=True)
    for s in SIZES:
        layers[s].save(win / f"icon-{s}.png", format="PNG")

    _build_ico(layers, win / "app-icon.ico")
    _build_ico(layers, ASSETS / "icon.ico")  # production path
    layers[256].save(ASSETS / "icon.png", format="PNG")  # tray

    print("regenerated icon.ico (sizes:", ", ".join(map(str, SIZES)), ") + per-size PNGs")


if __name__ == "__main__":
    main()
