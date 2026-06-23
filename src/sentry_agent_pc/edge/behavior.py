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
from sentry_agent_pc.edge.zones import CompiledZones, compile_zones, zones_at

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
    # Per-movement accumulated score + first-seen offset this episode → the
    # suspicious-clip score breakdown ("which movement banked how much").
    ep_scores: dict[str, float] = field(default_factory=dict)
    ep_first_ts: dict[str, float] = field(default_factory=dict)
    # Per-FIRE event log this episode: (behavior_key, wall-clock ts, +amount,
    # risk_pct after this frame) — one entry EVERY banking frame, so the clip
    # detail view can show the exact timeline ("at HH:MM:SS conceal +14 → 72%").
    # Reset on episode open; the aggregated ep_scores above stay for the summary.
    ep_events: list[tuple[str, float, float, float]] = field(default_factory=list)
    # docs/29 P1c (edge) zone state — TRACK-lifetime (NOT reset on episode close,
    # only when the track is dropped): `concealed` latches once the person shows
    # a concealment posture (so exit_after_concealment can fire later at the door);
    # the shelf fields count distinct shelf entries; the *_scored flags make each
    # zone criterion bank at most once per track (no re-pump).
    concealed: bool = False
    in_shelf: bool = False
    shelf_visits: int = 0
    shelf_scored: bool = False
    exit_scored: bool = False


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
    kp: NDArray[np.float32] | None,
    items: list[ItemDet],
    person_h: float,
    *,
    reach_frac: float = 0.35,
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
) -> tuple[float, set[str], dict[str, float]]:
    """Instantaneous suspicion increment + active behaviour keys for this frame,
    plus the per-movement score contribution (drives the episode breakdown)."""
    c = cfg or EdgeConfig()
    behaviors: set[str] = set()
    scores: dict[str, float] = {}
    score = 0.0
    holding = _wrist_on_item(kp, items, person_h, reach_frac=c.reach_frac)
    if holding:
        behaviors.add("item_pickup")
        score += c.w_holding
        scores["item_pickup"] = c.w_holding
    if _wrist_to_torso(kp, person_h, near_frac=c.near_frac):
        behaviors.add("wrist_to_torso")
        if holding:
            behaviors.add("conceal")
            score += c.w_conceal
            scores["conceal"] = c.w_conceal
        else:
            score += c.w_wrist_torso
            scores["wrist_to_torso"] = c.w_wrist_torso
    return score, behaviors, scores


class EdgeBehavior:
    """Light per-camera behaviour gate: detections → risk bands + episode events."""

    def __init__(
        self,
        camera_id: str,
        config: EdgeConfig | None = None,
        zones: list[dict[str, object]] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.cfg = config or EdgeConfig()
        # docs/29 P1c — per-camera detection zones (compiled once). From the local
        # CameraRecord.zones, NOT the config poller (different granularity).
        self._zones: CompiledZones = compile_zones(zones)
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    def apply_config(self, config: EdgeConfig) -> None:
        """Hot-apply new tunables (the config-poller swaps the whole config)."""
        self.cfg = config

    def oldest_open_episode_start(self) -> float | None:
        """Earliest start_ts across currently-open (suspicious) episodes, or None.

        The recorder uses this to PROTECT pre-roll segments from the rolling
        prune while an episode is still open — otherwise a long episode would lose
        its '−3s before' segments before the clip is ever cut."""
        starts = [tr.ep_start for tr in self._tracks.values() if tr.state == "suspicious"]
        return min(starts) if starts else None

    def update(
        self,
        persons: list[PersonDet],
        items: list[ItemDet],
        now: float,
        frame_wh: tuple[int, int] | None = None,
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
            signal, behaviors, frame_scores = _frame_signal(
                person.keypoints, items, person_h, self.cfg
            )
            # docs/29 P1c — latch concealment + add zone-aware signals (no-op when
            # the camera has no zones or the frame size is unknown).
            if behaviors & {"conceal", "wrist_to_torso"}:
                tr.concealed = True
            if self._zones and frame_wh is not None:
                zsig, zbeh, zsc = self._zone_signal(tr, person.box, frame_wh)
                signal += zsig
                behaviors |= zbeh
                frame_scores.update(zsc)
            tr.raw = tr.raw * self.cfg.decay + signal
            risk_pct = min(100.0, tr.raw)
            ep = self._advance_episode(tr, risk_pct, behaviors, frame_scores, now)
            if ep is not None:
                episodes.append(ep)

            bands.append(_band(risk_pct, yellow=self.cfg.band_yellow, red=self.cfg.band_red))
            trails.append(np.array(tr.trail, dtype=np.int32))

        episodes.extend(self._drop_stale(now))
        return BehaviorFrame(bands=bands, trails=trails, episodes=episodes)

    def _zone_signal(
        self, tr: _Track, box: tuple[float, float, float, float], frame_wh: tuple[int, int]
    ) -> tuple[float, set[str], dict[str, float]]:
        """Zone-aware suspicion for one track this frame (docs/29 P1c). Mirrors the
        cloud detectors: repeated_shelf_visit (distinct shelf entries → mild) and
        exit_after_concealment (concealed, then enters an exit zone → strong). Each
        banks at most once per track via the track-lifetime *_scored flags."""
        w, h = frame_wh
        foot_x = (box[0] + box[2]) / 2.0
        in_zones = zones_at(foot_x / max(1, w), box[3] / max(1, h), self._zones)

        sig = 0.0
        beh: set[str] = set()
        sc: dict[str, float] = {}

        # repeated_shelf_visit — count distinct not-inside→inside shelf entries.
        now_in_shelf = "shelf" in in_zones
        if now_in_shelf and not tr.in_shelf:
            tr.shelf_visits += 1
            if tr.shelf_visits >= self.cfg.repeated_shelf_threshold and not tr.shelf_scored:
                sig += self.cfg.w_repeated_shelf
                beh.add("repeated_shelf_visit")
                sc["repeated_shelf_visit"] = self.cfg.w_repeated_shelf
                tr.shelf_scored = True
        tr.in_shelf = now_in_shelf

        # exit_after_concealment — concealed earlier, now standing in an exit zone.
        if "exit" in in_zones and tr.concealed and not tr.exit_scored:
            sig += self.cfg.w_exit_after_conceal
            beh.add("exit_after_concealment")
            sc["exit_after_concealment"] = self.cfg.w_exit_after_conceal
            tr.exit_scored = True

        return sig, beh, sc

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
                    track_id=best_id,
                    box=person.box,
                    keypoints=person.keypoints,
                    last_seen=0.0,
                    trail=deque(maxlen=_TRAIL_MAXLEN),
                )
            used.add(best_id)
            out.append(best_id)
        return out

    def _advance_episode(
        self,
        tr: _Track,
        risk_pct: float,
        behaviors: set[str],
        frame_scores: dict[str, float],
        now: float,
    ) -> SuspiciousEpisode | None:
        def bank() -> None:
            for k, v in frame_scores.items():
                tr.ep_scores[k] = tr.ep_scores.get(k, 0.0) + v
                tr.ep_first_ts.setdefault(k, now)
                tr.ep_events.append((k, now, v, risk_pct))

        if tr.state == "normal":
            if risk_pct >= self.cfg.open_risk:
                tr.state = "suspicious"
                tr.ep_start = now
                tr.ep_peak = risk_pct
                tr.ep_behaviors = set(behaviors)
                tr.ep_scores = {}
                tr.ep_first_ts = {}
                tr.ep_events = []
                bank()
                tr.last_active = now
            return None
        # suspicious
        tr.ep_peak = max(tr.ep_peak, risk_pct)
        tr.ep_behaviors |= behaviors
        bank()
        if risk_pct >= self.cfg.close_risk:
            tr.last_active = now
        if now - tr.last_active >= self.cfg.post_quiet_sec:
            return self._close_episode(tr)
        return None

    def _close_episode(self, tr: _Track) -> SuspiciousEpisode:
        detail = [
            {
                "key": k,
                "offset_sec": round(max(0.0, tr.ep_first_ts.get(k, tr.ep_start) - tr.ep_start), 1),
                "score": round(score, 1),
            }
            for k, score in tr.ep_scores.items()
        ]
        # Per-fire timeline: absolute wall-clock ts + offset-from-start + amount +
        # the risk after that frame (so the detail view shows each +N AND the decay
        # between fires via the running risk%). Chronological (append order).
        events = [
            {
                "key": k,
                "ts": round(ts, 2),
                "offset_sec": round(max(0.0, ts - tr.ep_start), 1),
                "amount": round(amount, 1),
                "risk": round(risk, 1),
            }
            for k, ts, amount, risk in tr.ep_events
        ]
        ep = SuspiciousEpisode(
            camera_id=self.camera_id,
            start_ts=tr.ep_start,
            end_ts=tr.last_active,
            risk_pct=tr.ep_peak,
            behaviors=sorted(tr.ep_behaviors),
            behavior_detail=detail,
            events=events,
        )
        tr.state = "normal"
        tr.ep_behaviors = set()
        tr.ep_scores = {}
        tr.ep_first_ts = {}
        tr.ep_events = []
        return ep

    def _drop_stale(self, now: float) -> list[SuspiciousEpisode]:
        """Drop tracks unseen too long; close any open episode at last_seen."""
        episodes: list[SuspiciousEpisode] = []
        stale = [
            t for t, tr in self._tracks.items() if now - tr.last_seen > self.cfg.drop_after_sec
        ]
        for tid in stale:
            tr = self._tracks.pop(tid)
            if tr.state == "suspicious":
                episodes.append(self._close_episode(tr))
        return episodes
