"""Edge behaviour gate — turn detections into a suspicion risk + episode events.

Design note (option A): the EDGE is a CONSERVATIVE GATE; the cloud VLM is the
real judge. So this is NOT a verbatim copy of sentry-ai's full (evolving)
behaviour engine — it's an agent-native, torch-free re-implementation of the key
signals (holding / wrist-to-torso concealment / dwell) that opens a
``SuspiciousEpisode`` when risk crosses a threshold and closes it once the action
settles. The episode drives the clip recorder; the server re-scores + VLM-judges.
It reuses the same behaviour vocabulary ("item_pickup", "wrist_to_torso",
"conceal") so edge and cloud stay consistent.

No torch / no ultralytics — a light IoU tracker for stable IDs + keypoint maths.
All thresholds live in EdgeConfig so the central config-poller can hot-apply.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import ItemDet, PersonDet
from sentry_agent_pc.edge.overlay import kp_point
from sentry_agent_pc.edge.recorder import SuspiciousEpisode

# COCO-17 indices used by the gate.
_KP_L_WRI, _KP_R_WRI = 9, 10
_KP_L_HIP, _KP_R_HIP = 11, 12
_TRAIL_MAXLEN = 32


@dataclass(slots=True)
class _Track:
    track_id: int
    box: tuple[float, float, float, float]
    keypoints: NDArray[np.float32] | None
    last_seen: float
    trail: deque[tuple[int, int]]
    raw: float = 0.0
    state: str = "normal"  # "normal" | "suspicious"
    ep_start: float = 0.0
    ep_peak: float = 0.0
    last_active: float = 0.0
    ep_behaviors: set[str] = field(default_factory=set)


@dataclass(slots=True)
class BehaviorFrame:
    """Per-frame output: overlay inputs (bands/trails aligned to `persons`) +
    any episodes that CLOSED this frame (→ feed to the clip recorder)."""

    bands: list[str]
    trails: list[NDArray[np.int32]]
    episodes: list[SuspiciousEpisode]


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _band(risk_pct: float, *, yellow: float = 40.0, red: float = 70.0) -> str:
    if risk_pct >= red:
        return "red"
    if risk_pct >= yellow:
        return "yellow"
    return "green"


def _wrist_on_item(
    kp: NDArray[np.float32] | None, items: list[ItemDet], person_h: float, *, reach_frac: float = 0.35
) -> bool:
    if kp is None or not items:
        return False
    reach = person_h * reach_frac
    for widx in (_KP_L_WRI, _KP_R_WRI):
        w = kp_point(kp, widx)
        if w is None:
            continue
        for it in items:
            ix1, iy1, ix2, iy2 = it.box
            nx = min(max(float(w[0]), ix1), ix2)
            ny = min(max(float(w[1]), iy1), iy2)
            if ((w[0] - nx) ** 2 + (w[1] - ny) ** 2) ** 0.5 <= reach:
                return True
    return False


def _wrist_to_torso(
    kp: NDArray[np.float32] | None, person_h: float, *, near_frac: float = 0.18
) -> bool:
    """A wrist pulled in to a hip/waist — the pocket/bag concealment posture."""
    if kp is None:
        return False
    near = person_h * near_frac
    for widx in (_KP_L_WRI, _KP_R_WRI):
        w = kp_point(kp, widx)
        if w is None:
            continue
        for hidx in (_KP_L_HIP, _KP_R_HIP):
            h = kp_point(kp, hidx)
            if h is None:
                continue
            if ((w[0] - h[0]) ** 2 + (w[1] - h[1]) ** 2) ** 0.5 <= near:
                return True
    return False


def _frame_signal(
    kp: NDArray[np.float32] | None,
    items: list[ItemDet],
    person_h: float,
    cfg: EdgeConfig | None = None,
) -> tuple[float, set[str]]:
    """Instantaneous suspicion increment + active behaviour keys for this frame."""
    c = cfg or EdgeConfig()
    behaviors: set[str] = set()
    score = 0.0
    holding = _wrist_on_item(kp, items, person_h, reach_frac=c.reach_frac)
    if holding:
        behaviors.add("item_pickup")
        score += c.w_holding
    if _wrist_to_torso(kp, person_h, near_frac=c.near_frac):
        behaviors.add("wrist_to_torso")
        if holding:
            behaviors.add("conceal")
            score += c.w_conceal
        else:
            score += c.w_wrist_torso
    return score, behaviors


class EdgeBehavior:
    """Light per-camera behaviour gate: detections → risk bands + episode events."""

    def __init__(self, camera_id: str, config: EdgeConfig | None = None) -> None:
        self.camera_id = camera_id
        self.cfg = config or EdgeConfig()
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    def apply_config(self, config: EdgeConfig) -> None:
        """Hot-apply new tunables (the config-poller swaps the whole config)."""
        self.cfg = config

    def update(
        self, persons: list[PersonDet], items: list[ItemDet], now: float
    ) -> BehaviorFrame:
        matched = self._match(persons)
        bands: list[str] = []
        trails: list[NDArray[np.int32]] = []
        episodes: list[SuspiciousEpisode] = []

        for person, tid in zip(persons, matched, strict=True):
            tr = self._tracks[tid]
            tr.box = person.box
            tr.keypoints = person.keypoints
            tr.last_seen = now
            tr.trail.append((int((person.box[0] + person.box[2]) / 2), int(person.box[3])))

            person_h = max(1.0, person.box[3] - person.box[1])
            signal, behaviors = _frame_signal(person.keypoints, items, person_h, self.cfg)
            tr.raw = tr.raw * self.cfg.decay + signal
            risk_pct = min(100.0, tr.raw)
            ep = self._advance_episode(tr, risk_pct, behaviors, now)
            if ep is not None:
                episodes.append(ep)

            bands.append(_band(risk_pct, yellow=self.cfg.band_yellow, red=self.cfg.band_red))
            trails.append(np.array(tr.trail, dtype=np.int32))

        episodes.extend(self._drop_stale(now))
        return BehaviorFrame(bands=bands, trails=trails, episodes=episodes)

    def _match(self, persons: list[PersonDet]) -> list[int]:
        """Greedy IoU match to existing tracks; unmatched → new track."""
        out: list[int] = []
        used: set[int] = set()
        for person in persons:
            best_id, best_iou = -1, self.cfg.iou_match
            for tid, tr in self._tracks.items():
                if tid in used:
                    continue
                iou = _iou(person.box, tr.box)
                if iou >= best_iou:
                    best_id, best_iou = tid, iou
            if best_id < 0:
                best_id = self._next_id
                self._next_id += 1
                self._tracks[best_id] = _Track(
                    track_id=best_id, box=person.box, keypoints=person.keypoints,
                    last_seen=0.0, trail=deque(maxlen=_TRAIL_MAXLEN),
                )
            used.add(best_id)
            out.append(best_id)
        return out

    def _advance_episode(
        self, tr: _Track, risk_pct: float, behaviors: set[str], now: float
    ) -> SuspiciousEpisode | None:
        if tr.state == "normal":
            if risk_pct >= self.cfg.open_risk:
                tr.state = "suspicious"
                tr.ep_start = now
                tr.ep_peak = risk_pct
                tr.ep_behaviors = set(behaviors)
                tr.last_active = now
            return None
        # suspicious
        tr.ep_peak = max(tr.ep_peak, risk_pct)
        tr.ep_behaviors |= behaviors
        if risk_pct >= self.cfg.close_risk:
            tr.last_active = now
        if now - tr.last_active >= self.cfg.post_quiet_sec:
            return self._close_episode(tr)
        return None

    def _close_episode(self, tr: _Track) -> SuspiciousEpisode:
        ep = SuspiciousEpisode(
            camera_id=self.camera_id,
            start_ts=tr.ep_start,
            end_ts=tr.last_active,
            risk_pct=tr.ep_peak,
            behaviors=sorted(tr.ep_behaviors),
        )
        tr.state = "normal"
        tr.ep_behaviors = set()
        return ep

    def _drop_stale(self, now: float) -> list[SuspiciousEpisode]:
        """Drop tracks unseen too long; close any open episode at last_seen."""
        episodes: list[SuspiciousEpisode] = []
        stale = [t for t, tr in self._tracks.items() if now - tr.last_seen > self.cfg.drop_after_sec]
        for tid in stale:
            tr = self._tracks.pop(tid)
            if tr.state == "suspicious":
                episodes.append(self._close_episode(tr))
        return episodes
