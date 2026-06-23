"""Edge zone-aware signals (docs/29 P1c): exit_after_concealment +
repeated_shelf_visit in the EdgeBehavior gate. Track-state assertions (no torch)."""

from __future__ import annotations

import numpy as np

from sentry_agent_pc.edge.behavior import EdgeBehavior
from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import ItemDet, PersonDet

# A person whose box foot ((x1+x2)/2, y2) = (400, 400). With frame 800x600 that
# normalizes to (0.5, 0.667) — inside the zones below.
_IN_BOX = (300.0, 100.0, 500.0, 400.0)
# Foot (400, 250) → normalized (0.5, 0.417) — ABOVE the zones (y < 0.5) = outside.
_OUT_BOX = (300.0, 50.0, 500.0, 250.0)
_FRAME_WH = (800, 600)


# A zone covering normalized x∈[0.3,0.7], y∈[0.5,0.9] — contains the _IN_BOX foot.
def _zone(ztype: str) -> dict[str, object]:
    return {"type": ztype, "points": [[0.3, 0.5], [0.7, 0.5], [0.7, 0.9], [0.3, 0.9]]}


def _person(box: tuple[float, float, float, float] = _IN_BOX) -> PersonDet:
    return PersonDet(box=box, score=0.9, keypoints=np.zeros((17, 3), dtype=np.float32))


def _conceal(box: tuple[float, float, float, float]) -> tuple[list[PersonDet], list[ItemDet]]:
    """A wrist-on-hip + held item frame → holding + wrist_to_torso → conceal."""
    kp = np.zeros((17, 3), dtype=np.float32)
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    kp[10] = (cx, cy, 0.9)  # right wrist
    kp[12] = (cx, cy, 0.9)  # right hip — wrist ON hip
    p = PersonDet(box=box, score=0.9, keypoints=kp)
    items = [ItemDet("handbag", (cx - 10, cy - 10, cx + 10, cy + 10), 0.8)]
    return [p], items


def _track(b: EdgeBehavior):  # type: ignore[no-untyped-def]
    return next(iter(b._tracks.values()))


# === exit_after_concealment ===


def test_exit_after_concealment_fires_when_concealed_in_exit_zone() -> None:
    b = EdgeBehavior("cam", zones=[_zone("exit")])
    persons, items = _conceal(_IN_BOX)  # concealing, foot in the exit zone
    b.update(persons, items, 1.0, frame_wh=_FRAME_WH)
    tr = _track(b)
    assert tr.concealed is True
    assert tr.exit_scored is True
    assert tr.raw >= 40.0  # the strong exit-after-conceal weight was added


def test_mindur_delays_exit_signal_but_does_not_disable_it() -> None:
    # Regression (H6): latching *_scored happened BEFORE the timing gate, so any
    # non-zero mindur_exit_after_conceal silently killed exit-after-concealment for
    # the track's whole life. It must now only DELAY the bank.
    cfg = EdgeConfig(mindur_exit_after_conceal=2.0)
    b = EdgeBehavior("cam", config=cfg, zones=[_zone("exit")])
    persons, items = _conceal(_IN_BOX)
    b.update(persons, items, 1.0, frame_wh=_FRAME_WH)  # activity t=0s
    tr = _track(b)
    assert tr.exit_scored is False  # min-duration not met yet
    assert tr.raw < 40.0  # the strong exit weight has NOT banked
    # Stay concealed in the exit zone past the 2s min-duration → it banks.
    for t in (2.0, 3.0, 4.0):
        p, it = _conceal(_IN_BOX)
        b.update(p, it, t, frame_wh=_FRAME_WH)
    tr = _track(b)
    assert tr.exit_scored is True  # fired once the duration was met
    assert tr.raw >= 40.0


def test_exit_zone_without_concealment_does_not_fire() -> None:
    b = EdgeBehavior("cam", zones=[_zone("exit")])
    b.update([_person()], [], 1.0, frame_wh=_FRAME_WH)  # in exit, never concealed
    tr = _track(b)
    assert tr.exit_scored is False


def test_no_zones_no_zone_signal() -> None:
    b = EdgeBehavior("cam")  # no zones
    persons, items = _conceal(_IN_BOX)
    b.update(persons, items, 1.0, frame_wh=_FRAME_WH)
    tr = _track(b)
    assert tr.exit_scored is False


def test_no_frame_size_skips_zone_signal() -> None:
    b = EdgeBehavior("cam", zones=[_zone("exit")])
    persons, items = _conceal(_IN_BOX)
    b.update(persons, items, 1.0)  # frame_wh omitted → zones can't be tested
    tr = _track(b)
    assert tr.exit_scored is False


# === repeated_shelf_visit ===


def test_repeated_shelf_visit_counts_distinct_entries() -> None:
    b = EdgeBehavior("cam", zones=[_zone("shelf")])
    t = 0.0
    for _ in range(3):  # enter (in) → leave (out), 3 distinct entries
        t += 1
        b.update([_person(_IN_BOX)], [], t, frame_wh=_FRAME_WH)
        t += 1
        b.update([_person(_OUT_BOX)], [], t, frame_wh=_FRAME_WH)
    tr = _track(b)
    assert tr.shelf_visits >= 3
    assert tr.shelf_scored is True


def test_repeated_shelf_visit_not_before_threshold() -> None:
    b = EdgeBehavior("cam", zones=[_zone("shelf")])
    b.update([_person(_IN_BOX)], [], 1.0, frame_wh=_FRAME_WH)
    b.update([_person(_OUT_BOX)], [], 2.0, frame_wh=_FRAME_WH)
    b.update([_person(_IN_BOX)], [], 3.0, frame_wh=_FRAME_WH)  # only 2 entries
    tr = _track(b)
    assert tr.shelf_scored is False


def test_staying_in_shelf_is_one_visit() -> None:
    b = EdgeBehavior("cam", zones=[_zone("shelf")])
    for t in range(1, 6):  # never leaves → a single entry
        b.update([_person(_IN_BOX)], [], float(t), frame_wh=_FRAME_WH)
    tr = _track(b)
    assert tr.shelf_visits == 1
    assert tr.shelf_scored is False
