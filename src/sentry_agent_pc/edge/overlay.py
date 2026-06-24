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
TRAIL_MAXLEN = 32  # foot points kept per track for the trajectory trail


def risk_bgr(band: str) -> tuple[int, int, int]:
    """Risk band → BGR (cv2)."""
    if band == "red":
        return (0, 0, 255)
    if band == "yellow":
        return (0, 230, 230)
    return (0, 255, 0)


def _band_rgb(band: str) -> tuple[int, int, int]:
    """Risk band → RGB (for PIL text)."""
    if band == "red":
        return (255, 90, 90)
    if band == "yellow":
        return (255, 210, 60)
    return (120, 230, 130)


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


def _draw_person_labels(
    frame_bgr: NDArray[np.uint8],
    persons: list[PersonDet],
    risks: list[float],
    behaviors: list[set[str]],
    bands: list[str],
) -> NDArray[np.uint8]:
    """Draw a per-person pill at the top of each box: the live risk % (in the band
    colour) + the active behaviour names (white). PIL so Cyrillic renders. Returns
    the frame; only persons with risk >= 1 or an active behaviour get a label."""
    from PIL import Image, ImageDraw

    img = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    font = _label_font(14)
    pad = 3
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
        rgb = _band_rgb(bands[idx] if idx < len(bands) else "green")
        x1, y1, x2, y2 = (int(v) for v in p.box)
        rw = int(draw.textlength(risk_txt, font=font))
        full = risk_txt + beh_txt
        fbox = draw.textbbox((0, 0), full, font=font)
        fw, fh = fbox[2] - fbox[0], fbox[3] - fbox[1]
        px = x1
        py = y1 - fh - 2 * pad - 1
        if py < 0:  # box hugs the top → drop the pill just inside the box
            py = y1 + 1
        draw.rectangle([px, py, px + fw + 2 * pad, py + fh + 2 * pad], fill=(0, 0, 0))
        draw.text((px + pad, py + pad), risk_txt, font=font, fill=rgb)
        if beh_txt:
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
) -> None:
    """Link a wrist to any nearby item box — visualises the 'holding' geometry."""
    if kp is None or not items:
        return
    reach = person_h * 0.35
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

    # Layer 1: translucent pose-polygon body "mask".
    overlay = annotated.copy()
    drew_mask = False
    for p, band in zip(persons, use_bands, strict=False):
        ph = max(1.0, p.box[3] - p.box[1])
        if draw_body_fill(overlay, p.keypoints, risk_bgr(band), ph):
            drew_mask = True
    if drew_mask:
        cv2.addWeighted(overlay, _MASK_ALPHA, annotated, 1 - _MASK_ALPHA, 0, annotated)

    # Layers 2-4: trail, wrist→item link, risk box.
    for idx, (p, band) in enumerate(zip(persons, use_bands, strict=False)):
        bgr = risk_bgr(band)
        x1, y1, x2, y2 = (int(v) for v in p.box)
        ph = max(1.0, p.box[3] - p.box[1])
        if trails is not None and idx < len(trails) and len(trails[idx]) >= 2:
            cv2.polylines(annotated, [trails[idx]], False, bgr, 2, cv2.LINE_AA)
            tail = trails[idx][-1]
            cv2.circle(annotated, (int(tail[0]), int(tail[1])), 4, bgr, -1, cv2.LINE_AA)
        draw_wrist_item_links(annotated, p.keypoints, items, ph)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), bgr, 2)

    # Per-person live score + behaviour labels (PIL pass, Cyrillic). Only when
    # there's something to show, to skip the BGR↔PIL round-trip on empty frames.
    if person_risks is not None and persons:
        annotated = _draw_person_labels(
            annotated, persons, person_risks,
            person_behaviors if person_behaviors is not None else [],
            use_bands,
        )

    if fps is not None:
        cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 26), (0, 0, 0), -1)
        cv2.putText(
            annotated, f"{fps:.1f} fps  persons={len(persons)}", (8, 18),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA,
        )
    return annotated
