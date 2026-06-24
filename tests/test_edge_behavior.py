"""Edge behaviour-gate tests — IoU tracker, signals, episode open/close, drop."""

from __future__ import annotations

from collections import deque

import numpy as np

from sentry_agent_pc.edge.behavior import (
    EdgeBehavior,
    _band,
    _frame_signal,
    _iou,
    _Track,
    _wrist_on_item,
    _wrist_to_torso,
)
from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import ItemDet, PersonDet


def _bare_track(tid: int = 1) -> _Track:
    return _Track(
        track_id=tid, box=(0.0, 0.0, 1.0, 1.0), keypoints=None,
        last_seen=0.0, trail=deque(),
    )


# Mechanics tests below want the OLD ungated per-frame banking so they can open an
# episode in a few frames; the product defaults now gate banking (frame-rate
# independence) which is covered by its own tests further down.
_GATEFREE = EdgeConfig(
    interval_holding=0.0, mindur_holding=0.0,
    interval_wrist_torso=0.0, mindur_wrist_torso=0.0,
    interval_conceal=0.0, mindur_conceal=0.0,
    interval_repeated_shelf=0.0, mindur_repeated_shelf=0.0,
    interval_exit_after_conceal=0.0, mindur_exit_after_conceal=0.0,
)


def test_timing_gate_interval_debounces_banking() -> None:
    # interval_conceal=1.0 → conceal banks at 0.0, skips within 1s, banks again at 1.0.
    eng = EdgeBehavior("cam", EdgeConfig(interval_conceal=1.0, mindur_conceal=0.0))
    tr = _bare_track()
    fired = [
        t
        for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2)
        if "conceal" in eng._apply_timing_gate(tr, {"conceal": 14.0}, t)
    ]
    assert fired == [0.0, 1.0]


def test_timing_gate_min_duration_delays_first_bank() -> None:
    # mindur_holding=0.5 → item_pickup banks only after 0.5s continuous activity.
    eng = EdgeBehavior("cam", EdgeConfig(mindur_holding=0.5, interval_holding=0.0))
    tr = _bare_track()
    fired = [
        t
        for t in (0.0, 0.2, 0.4, 0.6, 0.8)
        if "item_pickup" in eng._apply_timing_gate(tr, {"item_pickup": 5.0}, t)
    ]
    assert fired == [0.6, 0.8]  # 0.0-0.4 still under the 0.5s min-duration


def test_timing_gate_disabled_banks_every_frame() -> None:
    # interval=0/mindur=0 preserves the old per-frame banking (no gate).
    eng = EdgeBehavior("cam", EdgeConfig(interval_conceal=0.0, mindur_conceal=0.0))
    tr = _bare_track()
    fired = [
        t
        for t in (0.0, 0.1, 0.2)
        if "conceal" in eng._apply_timing_gate(tr, {"conceal": 14.0}, t)
    ]
    assert fired == [0.0, 0.1, 0.2]


def test_timing_gate_resets_on_inactivity() -> None:
    # A behaviour that goes inactive then returns re-banks promptly (continuity reset).
    eng = EdgeBehavior("cam", EdgeConfig(interval_conceal=1.0, mindur_conceal=0.0))
    tr = _bare_track()
    assert "conceal" in eng._apply_timing_gate(tr, {"conceal": 14.0}, 0.0)  # bank
    eng._apply_timing_gate(tr, {}, 0.3)  # inactive → resets
    assert "conceal" in eng._apply_timing_gate(tr, {"conceal": 14.0}, 0.5)  # banks again

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
    eng = EdgeBehavior("cam03", _GATEFREE)
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
    # the per-FIRE timeline records one event per banking frame, in order, each
    # with a wall-clock ts + amount + the resulting risk (for the clip detail view)
    assert len(ep.events) >= len(ep.behavior_detail)
    assert all({"key", "ts", "offset_sec", "amount", "risk"} <= set(e) for e in ep.events)
    assert [e["ts"] for e in ep.events] == sorted(e["ts"] for e in ep.events)
    assert any(e["key"] == "conceal" and e["amount"] > 0 for e in ep.events)


def test_standing_person_stays_green_under_default_gates() -> None:
    # The reported bug: a shopper just STANDING (wrist near hip → wrist_to_torso
    # fires every frame) must NOT accumulate suspicion. With the default timing
    # gates, the score plateaus low — no episode, band stays out of red — across a
    # long, realistic 6 fps stand.
    eng = EdgeBehavior("cam-stand")  # PRODUCT DEFAULTS (gates on)
    # wrist on the hip but NO merchandise item → only wrist_to_torso (weak signal).
    person = _person(rwrist=(400.0, 300.0), rhip=(400.0, 300.0))
    last_band = "green"
    t = 0.0
    for _ in range(180):  # ~30 s at 6 fps
        frame = eng.update([person], [], now=t)
        assert frame.episodes == []  # never opens an episode just standing
        last_band = frame.bands[0]
        t += 1 / 6
    assert eng._tracks[1].raw < eng.cfg.open_risk  # stayed well under the open gate
    assert last_band != "red"


def test_sustained_conceal_still_opens_under_default_gates() -> None:
    # The gates must throttle benign poses WITHOUT killing real detection: a
    # sustained concealment (item held + wrist at hip) still climbs to open_risk
    # within a few seconds and emits an episode once it settles.
    eng = EdgeBehavior("cam-conceal")  # PRODUCT DEFAULTS (gates on)
    persons, items = _conceal_frame()
    opened = False
    t = 0.0
    for _ in range(60):  # up to 10 s of sustained concealment at 6 fps
        eng.update(persons, items, now=t)
        if eng._tracks[1].raw >= eng.cfg.open_risk:
            opened = True
            break
        t += 1 / 6
    assert opened, "sustained concealment should reach open_risk under the gates"
    assert t <= 6.0  # and do so within a realistic few seconds


def test_drop_stale_closes_open_episode() -> None:
    eng = EdgeBehavior("cam02", _GATEFREE)
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


# === recall + precision fixes (2026-06-22) ===


def test_wrist_to_torso_hip_fallback_when_hips_off_frame() -> None:
    # Overhead camera: hips off-frame, but shoulders + wrist visible. The waist is
    # estimated half a body-height below the shoulders so concealment still fires.
    person_h = 300.0
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[5] = (380.0, 150.0, 0.9)  # left shoulder
    kp[6] = (420.0, 150.0, 0.9)  # right shoulder
    kp[9] = (380.0, 300.0, 0.9)  # left wrist on the estimated waist (150 + 300*0.5)
    assert _wrist_to_torso(kp, person_h) is True


def test_hip_fallback_needs_both_shoulders() -> None:
    person_h = 300.0
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[5] = (380.0, 150.0, 0.9)  # only ONE shoulder, no hips → can't estimate waist
    kp[9] = (380.0, 300.0, 0.9)
    assert _wrist_to_torso(kp, person_h) is False


def test_own_phone_does_not_count_as_holding() -> None:
    # #1 edge false positive: holding your own phone near your waist must NOT read
    # as picking up merchandise; a real store item still does.
    person_h = 300.0
    kp = np.zeros((17, 3), dtype=np.float32)
    kp[10] = (400.0, 300.0, 0.9)  # wrist on the item
    phone = [ItemDet("cell phone", (390.0, 290.0, 415.0, 315.0), 0.8)]
    assert _wrist_on_item(kp, phone, person_h) is False
    merch = [ItemDet("bottle", (390.0, 290.0, 415.0, 315.0), 0.8)]
    assert _wrist_on_item(kp, merch, person_h) is True


def test_decay_is_wall_clock_not_per_frame() -> None:
    # A longer real-time gap decays the score more, so a camera's frame rate /
    # frame_skip can't silently change sensitivity. Same conceal signal + one idle
    # frame: the longer gap leaves the lower retained score.
    persons, items = _conceal_frame()
    fast, slow = EdgeBehavior("c", _GATEFREE), EdgeBehavior("c", _GATEFREE)
    fast.update(persons, items, now=0.0)
    slow.update(persons, items, now=0.0)
    seeded = fast._tracks[1].raw
    fast.update([_person(_BOX)], [], now=0.2)  # short gap
    slow.update([_person(_BOX)], [], now=0.6)  # 3x longer gap
    assert slow._tracks[1].raw < fast._tracks[1].raw < seeded
