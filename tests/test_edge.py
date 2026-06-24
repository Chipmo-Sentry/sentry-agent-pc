"""Edge Stage-1 detector + overlay unit tests (no model/GPU needed)."""

from __future__ import annotations

import numpy as np

from sentry_agent_pc.edge import overlay as ov
from sentry_agent_pc.edge.detector import DummyDetector, ItemDet, PersonDet


def test_dummy_detector_shapes() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    res = DummyDetector().detect(frame)
    assert len(res.persons) == 1
    kp = res.persons[0].keypoints
    assert kp is not None and kp.shape == (17, 3)
    assert len(res.items) == 1
    # item sits next to the right wrist (idx 10)
    assert res.items[0].label == "handbag"


def test_kp_point_validity() -> None:
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[10] = (100.0, 200.0, 0.9)
    assert ov.kp_point(kp, 10) == (100, 200)
    assert ov.kp_point(kp, 1) is None  # unset (0,0)
    kp[1] = (50.0, 50.0, 0.1)  # below conf gate
    assert ov.kp_point(kp, 1) is None
    assert ov.kp_point(None, 10) is None


def test_risk_bgr_bands() -> None:
    assert ov.risk_bgr("red") == (0, 0, 255)
    assert ov.risk_bgr("yellow") == (0, 230, 230)
    assert ov.risk_bgr("green") == (0, 255, 0)
    assert ov.risk_bgr("anything-else") == (0, 255, 0)


def test_draw_overlays_draws_and_preserves_original() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    res = DummyDetector().detect(frame)
    trail = np.array([(300, 440), (310, 442), (320, 441)], dtype=np.int32)
    out = ov.draw_overlays(frame, res.persons, res.items, bands=["red"], trails=[trail])
    assert out.shape == frame.shape
    assert bool((out != frame).any())  # something was drawn
    assert bool((frame == 0).all())  # original untouched (drew on a copy)


def test_draw_overlays_handles_no_persons() -> None:
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    out = ov.draw_overlays(frame, [], [])
    assert out.shape == frame.shape
    assert bool((out == 0).all())  # nothing to draw → blank stays blank


def test_draw_overlays_renders_score_and_behaviour_label() -> None:
    # The live overlay draws a per-person pill: risk % + Cyrillic behaviour label.
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    res = DummyDetector().detect(frame)
    out = ov.draw_overlays(
        frame, res.persons, res.items,
        bands=["yellow"],
        person_risks=[45.0],
        person_behaviors=[{"conceal", "item_pickup"}],
    )
    assert out.shape == frame.shape
    assert bool((out != frame).any())  # the score/label pill was drawn


def test_draw_overlays_skips_label_when_no_score_or_behaviour() -> None:
    # risk < 1 and no active behaviour → no pill (keeps the view clean).
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    res = DummyDetector().detect(frame)
    out = ov.draw_overlays(
        frame, res.persons, res.items,
        bands=["green"], person_risks=[0.0], person_behaviors=[set()],
    )
    # boxes/mask still draw, but assert no text region: hard to test text directly,
    # so just confirm it runs + returns a valid frame (label path is exercised).
    assert out.shape == frame.shape


def test_wrist_item_link_only_when_near() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[10] = (300.0, 300.0, 0.9)  # right wrist
    person = PersonDet(box=(250.0, 100.0, 350.0, 400.0), score=0.9, keypoints=kp)
    far = [ItemDet("bottle", (600.0, 50.0, 620.0, 70.0), 0.8)]
    near = [ItemDet("bottle", (305.0, 300.0, 325.0, 320.0), 0.8)]
    out_far = ov.draw_overlays(frame, [person], far)
    out_near = ov.draw_overlays(frame, [person], near)
    # the near item adds an amber link/box → more non-zero pixels than the far case
    assert int((out_near != 0).any(axis=2).sum()) > int((out_far != 0).any(axis=2).sum())
