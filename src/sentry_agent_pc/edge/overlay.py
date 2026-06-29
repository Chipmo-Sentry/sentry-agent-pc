"""Edge live-view overlay — pose-polygon mask, trajectory trail, wrist→item link,
risk box. Ported from sentry-ai's snapshot overlay (PR #6) to draw on the agent's
'Шууд харах' frames. cv2/numpy for the geometry; a small PIL pass renders the
per-person Cyrillic score + behaviour labels (cv2's Hershey fonts are ASCII-only).
No model, no torch.

The caller supplies a risk band per person ("green"/"yellow"/"red"); this module
is risk-agnostic (the behaviour engine decides the band). Trails are passed in as
ready polylines so this stays a pure drawing function.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.edge.behaviors_common import BEHAVIOR_LABELS
from sentry_agent_pc.edge.detector import ItemDet, PersonDet

# COCO-17 indices + draw constants (mirror sentry-ai camera_worker overlay).
_KP_NOSE = 0
_KP_L_WRI, _KP_R_WRI = 9, 10
# Limb chains drawn as thick rounded strokes; torso quad filled for body bulk.
_LIMB_CHAINS: tuple[tuple[int, ...], ...] = ((5, 7, 9), (6, 8, 10), (11, 13, 15), (12, 14, 16))
_TORSO_QUAD = (5, 6, 12, 11)  # L-shoulder, R-shoulder, R-hip, L-hip (non-self-intersecting)
_MIN_KP_CONF = 0.30
_MASK_ALPHA = 0.40
_ITEM_LINK_BGR = (0, 170, 255)  # amber (BGR)
_ITEM_LABEL_RGB = (255, 170, 0)  # amber pill behind the held-item name (RGB, PIL)
TRAIL_MAXLEN = 32  # foot points kept per track for the trajectory trail

# Display names for held items. The open-vocab detector emits English category
# labels ("snack bag", "bottle"); show them in Mongolian on «Шууд харах» so the
# operator instantly reads WHAT is in the hand. Unmapped labels fall back to the
# raw detector label, so a tuned vocabulary still shows something sensible.
ITEM_LABELS_MN: dict[str, str] = {
    "bottle": "лонх",
    "plastic bottle": "хуванцар лонх",
    "can": "лааз",
    "canned food": "лааз хүнс",
    "box": "хайрцаг",
    "carton": "картон хайрцаг",
    "milk carton": "сүүний хайрцаг",
    "jar": "шил сав",
    "packet": "боодол",
    "snack bag": "чипсний уут",
    "bag of chips": "чипсний уут",
    "chocolate bar": "шоколад",
    "candy": "чихэр",
    "instant noodle": "бэлэн гоймон",
    "cup": "аяга",
    "container": "сав",
    "tube": "тюбик",
    "cosmetic bottle": "косметик",
    "shampoo bottle": "шампунь",
    "boxed product": "хайрцагтай бараа",
    "handheld product": "бараа",
    "bag": "уут",
    "backpack": "үүргэвч",
    "handbag": "гар цүнх",
    "suitcase": "чемодан",
    "laptop": "зөөврийн компьютер",
    "cell phone": "гар утас",
    "book": "ном",
}


def item_label_mn(label: str) -> str:
    """Mongolian display name for a detected item, or the raw label if unmapped."""
    return ITEM_LABELS_MN.get(label.lower(), label)


# Brand-aligned risk palette — MUST match sentry-ui-kit `RISK_COLORS`
# (low=green #22C55E, medium=royal-blue #2563EB, high=red #EF4444) so the SAME
# score shows the SAME colour on web /live and the agent «Шууд харах». The band
# key "yellow" is the historical MEDIUM slot; it now renders royal-blue.
_RISK_RGB: dict[str, tuple[int, int, int]] = {
    "green": (34, 197, 94),  # #22C55E
    "yellow": (37, 99, 235),  # #2563EB — royal-blue (MEDIUM)
    "red": (239, 68, 68),  # #EF4444
}

# Visual band cutoffs on the 0-100 risk_pct — MUST match ui-kit `riskBand`
# (MEDIUM ≥ 30, HIGH ≥ 70). These are DISPLAY cutoffs only; the behaviour
# engine's own band/alert calibration is separate (tuned via the eval harness).
_RISK_MEDIUM_MIN = 30.0
_RISK_HIGH_MIN = 70.0


def _display_band(risk_pct: float) -> str:
    """Map a 0-100 risk_pct to its visual band key (matches ui-kit riskBand)."""
    if risk_pct >= _RISK_HIGH_MIN:
        return "red"
    if risk_pct >= _RISK_MEDIUM_MIN:
        return "yellow"
    return "green"


def _band_rgb(band: str) -> tuple[int, int, int]:
    """Risk band → RGB."""
    return _RISK_RGB.get(band, _RISK_RGB["green"])


def risk_bgr(band: str) -> tuple[int, int, int]:
    """Risk band → BGR (cv2)."""
    r, g, b = _band_rgb(band)
    return (b, g, r)


@lru_cache(maxsize=4)
def _label_font(size: int) -> Any:
    """A TrueType font with Cyrillic glyphs (cv2's Hershey fonts are ASCII-only,
    so Mongolian behaviour labels need PIL + a real font). Prefer the Windows UI
    font on the store PC, then Pillow's bundled DejaVuSans (also has Cyrillic),
    then the tiny bitmap default (ASCII — acceptable last resort)."""
    from PIL import ImageFont

    for path in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            if path.startswith("C:") and not Path(path).exists():
                continue
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 — any load failure → try the next candidate
            continue
    return ImageFont.load_default()


def _draw_item_pills(draw: Any, font: Any, held_items: list[ItemDet], pad: int) -> None:
    """Label each held item with its Mongolian name on an amber pill at the box's
    top-left — so the operator reads WHAT is in the hand, not just that something
    is. Deduped by box so an item near two wrists/people is labelled once."""
    seen: set[tuple[int, int, int, int]] = set()
    for it in held_items:
        x1, y1, x2, y2 = (int(v) for v in it.box)
        key = (x1, y1, x2, y2)
        if key in seen:
            continue
        seen.add(key)
        text = item_label_mn(it.label)
        tb = draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pill_w, pill_h = tw + 2 * pad, th + 2 * pad
        px = x1
        py = y1 - pill_h - 2
        if py < 0:  # box hugs the top → drop the label just inside the box
            py = y1 + 1
        radius = max(4, min(9, pill_h // 2))
        draw.rounded_rectangle([px, py, px + pill_w, py + pill_h], radius=radius, fill=_ITEM_LABEL_RGB)
        draw.text((px + pad, py + pad), text, font=font, fill=(20, 20, 20))


def _draw_person_labels(
    frame_bgr: NDArray[np.uint8],
    persons: list[PersonDet],
    risks: list[float],
    behaviors: list[set[str]],
    bands: list[str],
    held_items: list[ItemDet] | None = None,
) -> NDArray[np.uint8]:
    """Draw a per-person pill at the top of each box: the live risk % (in the band
    colour) + the active behaviour names (white). Also labels held items with their
    Mongolian name. PIL so Cyrillic renders. Returns the frame; only persons with
    risk >= 1 or an active behaviour get a label."""
    from PIL import Image, ImageDraw

    img = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    font = _label_font(14)
    pad = 3
    if held_items:
        _draw_item_pills(draw, font, held_items, pad)
    for idx, p in enumerate(persons):
        if idx >= len(risks):
            break
        risk = risks[idx]
        beh = behaviors[idx] if idx < len(behaviors) else set()
        labels = [BEHAVIOR_LABELS.get(b, b) for b in sorted(beh)]
        if risk < 1 and not labels:
            continue
        risk_txt = f"{risk:.0f}%"
        beh_txt = ("  " + ", ".join(labels)) if labels else ""
        # Solid band-colour pill + white text (reference UX), instead of the old
        # black pill + coloured text. Band derives from the live risk via the
        # unified spec so the colour matches the box and the web /live overlay.
        rgb = _band_rgb(bands[idx] if idx < len(bands) else "green")
        x1, y1, x2, y2 = (int(v) for v in p.box)
        rw = int(draw.textlength(risk_txt, font=font))
        full = risk_txt + beh_txt
        fbox = draw.textbbox((0, 0), full, font=font)
        fw, fh = fbox[2] - fbox[0], fbox[3] - fbox[1]
        pill_w = fw + 2 * pad
        pill_h = fh + 2 * pad
        px = x1
        py = y1 - pill_h - 2
        if py < 0:  # box hugs the top → drop the pill just inside the box
            py = y1 + 1
        radius = max(4, min(9, pill_h // 2))
        draw.rounded_rectangle(
            [px, py, px + pill_w, py + pill_h], radius=radius, fill=rgb
        )
        draw.text((px + pad, py + pad), risk_txt, font=font, fill=(255, 255, 255))
        if beh_txt:
            # Slightly dimmed white so the % reads as primary.
            draw.text((px + pad + rw, py + pad), beh_txt, font=font, fill=(235, 235, 235))
    return cast("NDArray[np.uint8]", cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))


def kp_point(kp: NDArray[np.float32] | None, idx: int) -> tuple[int, int] | None:
    """(x, y) pixel of keypoint `idx`, or None if unset / below draw-confidence."""
    if kp is None or idx >= kp.shape[0]:
        return None
    row = kp[idx]
    x, y = float(row[0]), float(row[1])
    if not (x > 1.0 and y > 1.0):  # (0,0)-ish → not detected
        return None
    if kp.shape[1] >= 3 and float(row[2]) < _MIN_KP_CONF:
        return None
    return int(x), int(y)


def draw_body_fill(
    overlay: NDArray[np.uint8],
    kp: NDArray[np.float32] | None,
    bgr: tuple[int, int, int],
    person_h: float,
) -> bool:
    """Approximate a body silhouette from pose keypoints (option A). Draws onto
    `overlay` (later alpha-blended). Returns True if anything was drawn."""
    if kp is None:
        return False
    th = max(4, int(person_h * 0.07))
    drew = False
    quad = [p for p in (kp_point(kp, i) for i in _TORSO_QUAD) if p is not None]
    if len(quad) >= 3:
        cv2.fillPoly(overlay, [np.array(quad, dtype=np.int32)], bgr)
        drew = True
    for chain in _LIMB_CHAINS:
        prev: tuple[int, int] | None = None
        for idx in chain:
            cur = kp_point(kp, idx)
            if cur is not None:
                cv2.circle(overlay, cur, max(3, th // 2), bgr, -1, cv2.LINE_AA)
                if prev is not None:
                    cv2.line(overlay, prev, cur, bgr, th, cv2.LINE_AA)
                    drew = True
            prev = cur
    nose = kp_point(kp, _KP_NOSE)
    if nose is not None:
        cv2.circle(overlay, nose, max(6, int(person_h * 0.06)), bgr, -1, cv2.LINE_AA)
        drew = True
    return drew


def draw_wrist_item_links(
    img: NDArray[np.uint8],
    kp: NDArray[np.float32] | None,
    items: list[ItemDet],
    person_h: float,
) -> list[ItemDet]:
    """Link a wrist to any nearby item box — visualises the 'holding' geometry.

    Returns the items detected as held (wrist within reach of the box) so the
    caller can label them in the Cyrillic PIL pass."""
    if kp is None or not items:
        return []
    reach = person_h * 0.35
    held: list[ItemDet] = []
    for widx in (_KP_L_WRI, _KP_R_WRI):
        w = kp_point(kp, widx)
        if w is None:
            continue
        for it in items:
            ix1, iy1, ix2, iy2 = it.box
            nx = min(max(float(w[0]), ix1), ix2)
            ny = min(max(float(w[1]), iy1), iy2)
            if ((w[0] - nx) ** 2 + (w[1] - ny) ** 2) ** 0.5 <= reach:
                cv2.rectangle(img, (int(ix1), int(iy1)), (int(ix2), int(iy2)), _ITEM_LINK_BGR, 2)
                cv2.line(
                    img, w, (int((ix1 + ix2) / 2), int((iy1 + iy2) / 2)),
                    _ITEM_LINK_BGR, 2, cv2.LINE_AA,
                )
                cv2.circle(img, w, 4, _ITEM_LINK_BGR, -1, cv2.LINE_AA)
                held.append(it)
    return held


def draw_overlays(
    frame_bgr: NDArray[np.uint8],
    persons: list[PersonDet],
    items: list[ItemDet],
    *,
    bands: list[str] | None = None,
    trails: list[NDArray[np.int32]] | None = None,
    person_risks: list[float] | None = None,
    person_behaviors: list[set[str]] | None = None,
    fps: float | None = None,
) -> NDArray[np.uint8]:
    """Return a copy of `frame_bgr` with the overlays drawn. `bands` is the
    per-person risk band (parallel to `persons`; default all green). `trails` are
    per-person foot-path polylines (parallel; optional). `person_risks` +
    `person_behaviors` (parallel) drive the live score + behaviour label per box."""
    annotated = frame_bgr.copy()
    use_bands = bands if bands is not None else ["green"] * len(persons)
    # Recolour by the live risk % via the unified visual spec (matches ui-kit
    # riskBand + the web /live overlay) so the SAME score shows the SAME colour
    # everywhere. Display-only: the engine's own `bands` (alert calibration) is
    # left untouched.
    if person_risks is not None and len(person_risks) == len(persons):
        use_bands = [_display_band(r) for r in person_risks]

    # Layer 1: translucent pose-polygon body "mask".
    overlay = annotated.copy()
    drew_mask = False
    for p, band in zip(persons, use_bands, strict=False):
        ph = max(1.0, p.box[3] - p.box[1])
        if draw_body_fill(overlay, p.keypoints, risk_bgr(band), ph):
            drew_mask = True
    if drew_mask:
        cv2.addWeighted(overlay, _MASK_ALPHA, annotated, 1 - _MASK_ALPHA, 0, annotated)

    # Layers 2-4: trail, wrist→item link, risk box. Collect held items (boxed by
    # draw_wrist_item_links) so the PIL pass can label WHAT each one is.
    held_items: list[ItemDet] = []
    for idx, (p, band) in enumerate(zip(persons, use_bands, strict=False)):
        bgr = risk_bgr(band)
        x1, y1, x2, y2 = (int(v) for v in p.box)
        ph = max(1.0, p.box[3] - p.box[1])
        if trails is not None and idx < len(trails) and len(trails[idx]) >= 2:
            cv2.polylines(annotated, [trails[idx]], False, bgr, 2, cv2.LINE_AA)
            tail = trails[idx][-1]
            cv2.circle(annotated, (int(tail[0]), int(tail[1])), 4, bgr, -1, cv2.LINE_AA)
        held_items.extend(draw_wrist_item_links(annotated, p.keypoints, items, ph))
        cv2.rectangle(annotated, (x1, y1), (x2, y2), bgr, 2)

    # Per-person live score + behaviour labels + held-item names (PIL pass,
    # Cyrillic). Fire when there's a score to show OR an item to name, to skip the
    # BGR↔PIL round-trip on empty frames.
    if persons and (person_risks is not None or held_items):
        annotated = _draw_person_labels(
            annotated, persons,
            person_risks if person_risks is not None else [],
            person_behaviors if person_behaviors is not None else [],
            use_bands,
            held_items=held_items,
        )

    if fps is not None:
        cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 26), (0, 0, 0), -1)
        cv2.putText(
            annotated, f"{fps:.1f} fps  persons={len(persons)}", (8, 18),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA,
        )
    return annotated
