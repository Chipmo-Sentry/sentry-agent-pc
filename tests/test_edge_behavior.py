"""Edge behaviour-gate tests — IoU tracker, signals, episode open/close, drop."""

from __future__ import annotations

import numpy as np

from sentry_agent_pc.edge.behavior import (
    EdgeBehavior,
    _band,
    _frame_signal,
    _iou,
    _wrist_on_item,
    _wrist_to_torso,
)
from sentry_agent_pc.edge.detector import ItemDet, PersonDet

_BOX = (300.0, 100.0, 500.0, 400.0)  # person_h = 300


def _person(
    box: tuple[float, float, float, float] = _BOX,
    *,
    rwrist: tuple[float, float] | None = None,
    rhip: tuple[float, float] | None = None,
) -> PersonDet:
    kp = np.zeros((17, 3), dtype=np.float32)
    if rwrist is not None:
        kp[10] = (rwrist[0], rwrist[1], 0.9)
    if rhip is not None:
        kp[12] = (rhip[0], rhip[1], 0.9)
    return PersonDet(box=box, score=0.9, keypoints=kp)


def _conceal_frame() -> tuple[list[PersonDet], list[ItemDet]]:
    # right wrist on the right hip (concealment posture) + an item at the wrist
    p = _person(rwrist=(400.0, 300.0), rhip=(400.0, 300.0))
    items = [ItemDet("handbag", (390.0, 290.0, 415.0, 315.0), 0.8)]
    return [p], items


def test_iou_basic() -> None:
    assert _iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert _iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert 0.0 < _iou((0, 0, 10, 10), (5, 5, 15, 15)) < 1.0


def test_band_thresholds() -> None:
    assert _band(80.0) == "red"
    assert _band(50.0) == "yellow"
    assert _band(10.0) == "green"


def test_signals_holding_and_conceal() -> None:
    person_h = 300.0
    items = [ItemDet("bottle", (390.0, 290.0, 415.0, 315.0), 0.8)]
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[10] = (400.0, 300.0, 0.9)  # right wrist on the item
    kp[12] = (400.0, 300.0, 0.9)  # right hip at the wrist → wrist-to-torso
    assert _wrist_on_item(kp, items, person_h) is True
    assert _wrist_to_torso(kp, person_h) is True
    score, behaviors, scores = _frame_signal(kp, items, person_h)
    assert score > 0
    assert {"item_pickup", "wrist_to_torso", "conceal"} <= behaviors
    # the score map carries each movement's contribution
    assert scores.get("item_pickup", 0.0) > 0
    assert scores.get("conceal", 0.0) > 0

    # wrist far from any item / hip → no signal
    kp2 = np.zeros((17, 3), dtype=np.float32)
    kp2[10] = (50.0, 50.0, 0.9)
    s2, b2, sc2 = _frame_signal(kp2, items, person_h)
    assert s2 == 0.0 and b2 == set() and sc2 == {}


def test_tracker_stable_and_new_ids() -> None:
    eng = EdgeBehavior("cam01")
    eng.update([_person(_BOX)], [], now=0.0)
    eng.update([_person(_BOX)], [], now=0.1)  # same box → same single track
    assert len(eng._tracks) == 1
    eng.update([_person(_BOX), _person((10.0, 10.0, 60.0, 160.0))], [], now=0.2)
    assert len(eng._tracks) == 2  # the far second person is a new track


def test_episode_opens_then_closes_with_metadata() -> None:
    eng = EdgeBehavior("cam03")
    persons, items = _conceal_frame()

    # sustained concealment → risk climbs to the red band, but no episode emits yet
    last: list = []
    for i in range(8):
        last = eng.update(persons, items, now=i * 0.1).episodes
        assert last == []  # episode only emits on CLOSE
    frame = eng.update(persons, items, now=0.8)
    assert frame.bands[0] == "red"

    # action settles: zero-signal frames advancing time → exactly one episode closes
    closed = []
    t = 1.0
    for _ in range(80):
        closed += eng.update([_person(_BOX)], [], now=t).episodes
        if closed:
            break
        t += 0.2
    assert len(closed) == 1
    ep = closed[0]
    assert ep.camera_id == "cam03"
    assert ep.start_ts <= ep.end_ts
    assert ep.risk_pct >= 60.0
    assert "conceal" in ep.behaviors and "item_pickup" in ep.behaviors
    # the per-movement score breakdown is banked for the gallery + cloud handoff
    detail = {d["key"]: d for d in ep.behavior_detail}
    assert "item_pickup" in detail and "conceal" in detail
    assert detail["item_pickup"]["score"] > 0  # accumulated over the sustained frames
    assert all("offset_sec" in d for d in ep.behavior_detail)


def test_drop_stale_closes_open_episode() -> None:
    eng = EdgeBehavior("cam02")
    persons, items = _conceal_frame()
    for i in range(8):
        eng.update(persons, items, now=i * 0.1)  # open a suspicious episode
    # person vanishes; after the drop timeout the open episode is force-closed
    frame = eng.update([], [], now=10.0)
    assert len(frame.episodes) == 1
    assert frame.episodes[0].camera_id == "cam02"
    assert eng._tracks == {}  # track dropped


def test_update_returns_aligned_bands_and_trails() -> None:
    eng = EdgeBehavior("cam01")
    persons = [_person(_BOX), _person((10.0, 10.0, 60.0, 160.0))]
    frame = eng.update(persons, [], now=0.0)
    assert len(frame.bands) == len(persons)
    assert len(frame.trails) == len(persons)
    assert all(t.dtype == np.int32 for t in frame.trails)
