"""Edge Stage-1 pipeline — one place that wires detector → behaviour → recorder
→ overlay for a single camera. The live view (P3) calls ``process(frame, now)``
each decoded frame and shows the returned annotated frame.

Latency strategy (8GB / iGPU): YOLO runs only every ``frame_skip``-th frame; on
the in-between frames the last detection's boxes/bands/trails are re-drawn so the
overlay looks smooth without inferring every frame. Suspicious episodes that
CLOSE are handed to the clip recorder (which cut the −3s…+3s clip).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.edge.behavior import BehaviorFrame, EdgeBehavior
from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import ConfTunable, Detector, DetectResult
from sentry_agent_pc.edge.overlay import draw_overlays
from sentry_agent_pc.edge.recorder import EdgeClipRecorder


class EdgePipeline:
    """Per-camera: detect (frame-skipped) → behaviour gate → recorder + overlay."""

    def __init__(
        self,
        camera_id: str,
        detector: Detector,
        recorder: EdgeClipRecorder | None = None,
        *,
        config: EdgeConfig | None = None,
        frame_skip: int | None = None,
        zones: list[dict[str, object]] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.detector = detector
        self.cfg = config or EdgeConfig()
        # docs/29 P1c — per-camera detection zones (from the local CameraRecord).
        self.behavior = EdgeBehavior(camera_id, self.cfg, zones=zones)
        self.recorder = recorder
        # explicit frame_skip overrides the config (tests/callers); else config.
        self.frame_skip = max(1, frame_skip if frame_skip is not None else self.cfg.frame_skip)
        self._n = 0
        self._last = DetectResult()
        self._frame: BehaviorFrame | None = None
        self._push_detector_conf()  # honour cfg confidences from the first frame

    def _push_detector_conf(self) -> None:
        """Thread the EdgeConfig detection thresholds into the detector (if it
        supports it) — otherwise person/item/keypoint conf would be a dead knob."""
        if isinstance(self.detector, ConfTunable):
            self.detector.apply_conf(
                person_conf=self.cfg.person_conf,
                item_conf=self.cfg.item_conf,
                min_kp_conf=self.cfg.min_kp_conf,
            )

    def apply_config(self, config: EdgeConfig) -> None:
        """Hot-apply tunables (behaviour gate + frame-skip + detector conf)."""
        self.cfg = config
        self.behavior.apply_config(config)
        self.frame_skip = max(1, config.frame_skip)
        self._push_detector_conf()

    def process(self, frame_bgr: NDArray[np.uint8], now: float) -> NDArray[np.uint8]:
        """Run (or reuse) detection + behaviour for this frame, return the overlay."""
        if self._n % self.frame_skip == 0:
            self._last = self.detector.detect(frame_bgr)
            h, w = frame_bgr.shape[:2]
            self._frame = self.behavior.update(
                self._last.persons, self._last.items, now, frame_wh=(w, h)
            )
            if self.recorder is not None:
                # Protect pre-roll segments of any in-flight episode from pruning,
                # then hand closed episodes to the recorder OFF this thread.
                self.recorder.set_protect_floor(self.behavior.oldest_open_episode_start())
                for ep in self._frame.episodes:
                    self.recorder.submit(ep)
        self._n += 1
        bands = self._frame.bands if self._frame is not None else None
        trails = self._frame.trails if self._frame is not None else None
        risks = self._frame.person_risks if self._frame is not None else None
        behaviors = self._frame.person_behaviors if self._frame is not None else None
        return draw_overlays(
            frame_bgr,
            self._last.persons,
            self._last.items,
            bands=bands,
            trails=trails,
            person_risks=risks,
            person_behaviors=behaviors,
        )
