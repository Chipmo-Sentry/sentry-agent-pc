"""Edge Stage-1 tunables — one place for every knob, so the central config-poller
can hot-apply operator changes WITHOUT a release (mirrors the node's pattern).

EdgeConfig is immutable; ``from_dict`` builds one from a partial backend payload
(unknown keys ignored, missing keys keep defaults). The runtime swaps the whole
config atomically to hot-apply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.config")

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class EdgeConfig:
    # Detection
    person_conf: float = 0.35
    item_conf: float = 0.40
    frame_skip: int = 3  # run YOLO every Nth decoded frame
    # Open-vocabulary item detection. COCO has only ~10 retail-relevant classes,
    # so most merchandise a shopper can pick up (snacks, cartons, jars, cosmetics)
    # is INVISIBLE to the stock yolo11n item model — the root cause behind
    # "concealment never fires" (see require_holding). When True, the lean detector
    # swaps the COCO item model for a YOLOE/YOLO-World IR exported with a retail
    # vocabulary (bin/yoloe_items_openvino_model + vocab.json), so "хүний барьж
    # болох зүйл" is detected generically. Default OFF for a shadow-style rollout:
    # ship the IR, flip the flag per-store from superadmin, compare, then default on.
    # Falls back to the COCO model if the open-vocab IR isn't bundled.
    open_vocab_items: bool = False
    # Behaviour signal weights + geometry
    w_holding: float = 5.0
    w_conceal: float = 14.0
    w_wrist_torso: float = 3.0
    reach_frac: float = 0.35  # wrist→item proximity as a fraction of person height
    near_frac: float = 0.18  # wrist→hip (concealment) proximity fraction
    min_kp_conf: float = 0.30
    # Concealment gating (mirrors the cloud engine). Most retail merchandise is
    # NOT a COCO class, so requiring a held COCO item silently disabled "хүн юм
    # нуухад" detection — the #1 "it never catches me" cause. Default OFF → conceal
    # fires on the wrist→pocket/waist gesture ALONE (the cloud VLM verifies the
    # clip downstream, so a stray reach is filtered). Set True to restore the
    # strict "must have picked up a COCO item first" gate.
    require_holding: bool = False
    # Keep "holding" latched this many seconds after the wrist last touched an
    # item, so the act of concealing (which HIDES the item from YOLO) doesn't
    # instantly clear the hold — concealment then counts as still-holding.
    hold_latch_sec: float = 1.5
    # docs/29 P1c (edge parity) — zone-aware signals. exit_after_concealment is
    # strong (a concealed person entering an exit zone should push the gate open
    # so the clip is recorded + sent to the cloud VLM); repeated_shelf_visit is a
    # mild dwell hint. No-op on a camera with no zones drawn.
    w_exit_after_conceal: float = 40.0
    w_repeated_shelf: float = 3.0
    repeated_shelf_threshold: int = 3  # distinct shelf entries before it banks
    # Per-behaviour TIMING gates. Each behaviour banks its score only after it's
    # been continuously active for >= its `mindur_*` seconds, then at most once per
    # `interval_*` seconds. This makes scoring FRAME-RATE INDEPENDENT (a bank is an
    # event-per-second, not per-frame) — without it, a continuously-detected benign
    # pose (e.g. a standing shopper whose wrist sits near a hip → wrist_to_torso
    # every frame) accumulates `weight × fps / decay-loss` and saturates the score
    # in ~1-2s even though nothing suspicious happened. The defaults below are tuned
    # so: a brief/incidental pose (< mindur) never banks; a benign sustained pose
    # plateaus well under the bands; only a SUSTAINED concealment climbs to
    # open_risk. All globally tunable from superadmin. (0 = ungated per-frame.)
    interval_holding: float = 2.0
    mindur_holding: float = 0.5
    interval_wrist_torso: float = 3.0
    mindur_wrist_torso: float = 1.5
    interval_conceal: float = 0.5
    mindur_conceal: float = 0.6
    interval_repeated_shelf: float = 0.0  # one-shot per track; mindur filters noise
    mindur_repeated_shelf: float = 0.5
    interval_exit_after_conceal: float = 0.0  # one-shot per track
    mindur_exit_after_conceal: float = 0.3
    # Risk → episode FSM. `decay` is the retained fraction per 1/5 s (wall-clock);
    # 0.92 ≈ a ~2.3 s half-life, slightly slower than before so a genuine sustained
    # signal still accumulates to open_risk under the throttled (gated) banking.
    decay: float = 0.92
    open_risk: float = 60.0
    close_risk: float = 30.0
    post_quiet_sec: float = 2.0
    drop_after_sec: float = 1.5
    iou_match: float = 0.3
    band_yellow: float = 40.0
    band_red: float = 70.0
    # Clip recorder (the −3s … +3s requirement)
    pre_sec: float = 3.0
    post_sec: float = 3.0
    segment_sec: float = 1.0
    keep_sec: float = 45.0
    max_clips: int = 50
    max_age_sec: float = 7 * 24 * 3600
    # Server handoff
    upload_clips: bool = True  # push suspicious clips to the cloud VLM host

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EdgeConfig:
        """Build from a partial dict (e.g. backend payload); ignore unknown keys."""
        if not data:
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known or value is None:
                continue
            try:
                kwargs[key] = _coerce(known[key], value)
            except (TypeError, ValueError):
                log.warning("edge_config.reject_value", key=key, value=repr(value))
                continue
        # `decay` is the per-step retained fraction; it MUST stay in [0, 1). A
        # misconfigured >= 1 makes `raw` grow without bound so the episode FSM can
        # never reach close_risk — the suspicion never cools off. Clamp it.
        if "decay" in kwargs:
            kwargs["decay"] = min(0.9999, max(0.0, kwargs["decay"]))
        return cls(**kwargs)


def _coerce(type_str: Any, value: Any) -> Any:
    """Best-effort coerce a JSON value to the field's annotated scalar type.

    Strings are parsed by content — ``"false"``/``"off"`` become ``False`` (a
    plain ``bool("false")`` is ``True``, which would silently flip a knob).
    Invalid values raise so ``from_dict`` rejects + logs them rather than
    coercing to a wrong default.
    """
    t = str(type_str)
    if "bool" in t:
        if isinstance(value, str):
            s = value.strip().lower()
            if s in _TRUE_STRINGS:
                return True
            if s in _FALSE_STRINGS:
                return False
            raise ValueError(f"not a bool: {value!r}")
        return bool(value)
    if "int" in t:
        return int(float(value))  # tolerate "5", "5.0", 5.0 → 5
    if "float" in t:
        v = float(value)
        # Reject NaN/inf: a single non-finite weight/threshold poisons `raw`
        # (NaN compares False against every band/open/close threshold), silently
        # killing detection for that camera until restart. Better to drop + keep
        # the default than to coerce a value that breaks the FSM.
        if not math.isfinite(v):
            raise ValueError(f"non-finite float: {value!r}")
        return v
    return value
