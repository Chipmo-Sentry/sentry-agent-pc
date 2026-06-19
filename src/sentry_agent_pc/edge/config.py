"""Edge Stage-1 tunables — one place for every knob, so the central config-poller
can hot-apply operator changes WITHOUT a release (mirrors the node's pattern).

EdgeConfig is immutable; ``from_dict`` builds one from a partial backend payload
(unknown keys ignored, missing keys keep defaults). The runtime swaps the whole
config atomically to hot-apply.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class EdgeConfig:
    # Detection
    person_conf: float = 0.35
    item_conf: float = 0.40
    frame_skip: int = 3  # run YOLO every Nth decoded frame
    # Behaviour signal weights + geometry
    w_holding: float = 5.0
    w_conceal: float = 14.0
    w_wrist_torso: float = 3.0
    reach_frac: float = 0.35  # wrist→item proximity as a fraction of person height
    near_frac: float = 0.18  # wrist→hip (concealment) proximity fraction
    min_kp_conf: float = 0.30
    # Risk → episode FSM
    decay: float = 0.90
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
                continue
        return cls(**kwargs)


def _coerce(type_str: Any, value: Any) -> Any:
    """Best-effort coerce a JSON value to the field's annotated scalar type."""
    t = str(type_str)
    if "bool" in t:
        return bool(value)
    if "int" in t:
        return int(value)
    if "float" in t:
        return float(value)
    return value
