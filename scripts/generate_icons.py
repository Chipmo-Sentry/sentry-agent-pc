"""Regenerate the Windows app icon bundle from the brand SVG.

Renders EACH icon size natively from the vector (16/20/24/32/40/48/64/128/256)
— not a downscale of one big bitmap — so every size, including the small
taskbar/title-bar ones, is sharp. The brand SVG draws the logo FULL-BLEED (it
touches the viewBox edges), so each render is scaled to fill `FILL` of the icon
(a thin uniform margin) — otherwise the logo reads tiny/blurry at 16-24 px.
Writes:

  src/sentry_agent_pc/assets/icon.ico            (production: exe / title-bar / installer)
  src/sentry_agent_pc/assets/icon.png            (production: tray, 256px)
  src/sentry_agent_pc/assets/icons/windows/*.png (per-size PNGs)
  src/sentry_agent_pc/assets/icons/windows/app-icon.ico

Usage (Cairo-free — works on a stock Windows box):
    uv run --no-project --with "reportlab==3.6.13" --with "svglib==1.5.1" \
        --with pillow python scripts/generate_icons.py

`svglib` + reportlab 3.6 ship a bundled C rasterizer (no Cairo). `cairosvg`
also works where `libcairo-2.dll` is present; reportlab >= 4 needs `rlPyCairo`
(Cairo) so pin < 4. Any rasterizer that returns an RGBA PNG works — only the
per-size native render + FILL margin matter.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "sentry_agent_pc" / "assets"
SVG = ASSETS / "icons" / "source" / "chipmo-logo.svg"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
FILL = 0.96  # logo fills 96% of each icon (≈2% margin) — full-bleed looks cramped


def _render_master() -> Image.Image:
    """Rasterize the SVG once at high resolution → a tight (cropped) RGBA master."""
    from reportlab.graphics import renderPM
    from svglib.svglib import svg2rlg

    drawing = svg2rlg(str(SVG))
    scale = 1024.0 / drawing.width
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)
    out = ASSETS / "icons" / "_master.png"
    renderPM.drawToFile(drawing, str(out), fmt="PNG")
    master = Image.open(out).convert("RGBA")
    out.unlink(missing_ok=True)
    bbox = master.split()[3].getbbox()  # trim any stray transparent border
    return master.crop(bbox) if bbox else master


def _fit(master: Image.Image, size: int) -> Image.Image:
    """Scale the master to `FILL`×size and centre it on a transparent square."""
    inner = max(1, round(size * FILL))
    logo = master.resize((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    off = (size - inner) // 2
    canvas.alpha_composite(logo, (off, off))
    return canvas


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
    master = _render_master()
    layers = {s: _fit(master, s) for s in SIZES}

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
