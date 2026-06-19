"""Edge Stage-1 runtime — the per-camera engine that ties everything together.

For each camera: a rolling clip recorder (segment ring) + a detect→behaviour→
overlay pipeline, sharing one bounded clip store. The live view feeds decoded
frames in via ``process()`` and shows the returned overlay; suspicious episodes
cut the −3s…+3s clip and fire ``on_clip`` (→ server upload / VLM handoff).
``apply_config`` hot-applies tunables across every camera (config-poller).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import Detector
from sentry_agent_pc.edge.pipeline import EdgePipeline
from sentry_agent_pc.edge.recorder import ClipRecord, ClipStore, EdgeClipRecorder
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.runtime")

DetectorFactory = Callable[[EdgeConfig], Detector]
ClipHandler = Callable[[ClipRecord], None]


class EdgeRuntime:
    """Owns the edge pipelines + recorders for a set of cameras."""

    def __init__(
        self,
        base_dir: Path,
        detector_factory: DetectorFactory,
        *,
        config: EdgeConfig | None = None,
        on_clip: ClipHandler | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.cfg = config or EdgeConfig()
        self._factory = detector_factory
        self._on_clip = on_clip
        self.store = ClipStore(
            self.base_dir / "clips.json",
            max_clips=self.cfg.max_clips,
            max_age_sec=self.cfg.max_age_sec,
        )
        self._pipes: dict[str, EdgePipeline] = {}
        self._recorders: dict[str, EdgeClipRecorder] = {}
        self._lock = threading.Lock()

    def start_camera(self, camera_id: str, src_url: str) -> None:
        """Begin recording + analysing one camera (idempotent)."""
        with self._lock:
            if camera_id in self._pipes:
                return
            rec = EdgeClipRecorder(
                camera_id, src_url, self.base_dir, self.store,
                pre=self.cfg.pre_sec, post=self.cfg.post_sec,
                segment_sec=self.cfg.segment_sec, keep_sec=self.cfg.keep_sec,
                on_clip=self._on_clip,
            )
            pipe = EdgePipeline(camera_id, self._factory(self.cfg), rec, config=self.cfg)
            self._recorders[camera_id] = rec
            self._pipes[camera_id] = pipe
        rec.start()
        log.info("edge.camera_started", camera_id=camera_id)

    def stop_camera(self, camera_id: str) -> None:
        with self._lock:
            self._pipes.pop(camera_id, None)
            rec = self._recorders.pop(camera_id, None)
        if rec is not None:
            rec.stop()
        log.info("edge.camera_stopped", camera_id=camera_id)

    def process(
        self, camera_id: str, frame_bgr: NDArray[np.uint8], now: float
    ) -> NDArray[np.uint8] | None:
        """Run one decoded frame through the camera's pipeline → annotated frame.

        Returns None if the camera isn't started (caller shows the raw frame)."""
        pipe = self._pipes.get(camera_id)
        if pipe is None:
            return None
        return pipe.process(frame_bgr, now)

    def clips(self) -> list[ClipRecord]:
        return self.store.records()

    def apply_config(self, config: EdgeConfig) -> None:
        """Hot-apply tunables to every camera (segment_sec/keep_sec need a restart
        to take effect, so they're left to the next start_camera)."""
        with self._lock:
            self.cfg = config
            self.store.max_clips = config.max_clips
            self.store.max_age_sec = config.max_age_sec
            for pipe in self._pipes.values():
                pipe.apply_config(config)
            for rec in self._recorders.values():
                rec.pre = config.pre_sec
                rec.post = config.post_sec
        log.info("edge.config_applied", cameras=len(self._pipes))

    def stop_all(self) -> None:
        with self._lock:
            recs = list(self._recorders.values())
            self._pipes.clear()
            self._recorders.clear()
        for rec in recs:
            rec.stop()
