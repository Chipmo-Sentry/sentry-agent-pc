"""Edge pipeline tests — frame-skip, overlay output, episode→recorder handoff."""

from __future__ import annotations

import numpy as np

from sentry_agent_pc.edge.detector import DetectResult, DummyDetector, ItemDet, PersonDet
from sentry_agent_pc.edge.pipeline import EdgePipeline
from sentry_agent_pc.edge.recorder import EdgeClipRecorder, SuspiciousEpisode


class _CountingDetector:
    def __init__(self) -> None:
        self.calls = 0
        self._inner = DummyDetector()

    def detect(self, frame_bgr: np.ndarray) -> DetectResult:
        self.calls += 1
        return self._inner.detect(frame_bgr)


class _SeqDetector:
    """Conceal posture for the first `conceal_for` detect calls, then no signal."""

    def __init__(self, conceal_for: int) -> None:
        self.i = 0
        self.conceal_for = conceal_for

    def detect(self, frame_bgr: np.ndarray) -> DetectResult:
        self.i += 1
        box = (300.0, 100.0, 500.0, 400.0)
        kp = np.zeros((17, 3), dtype=np.float32)
        if self.i <= self.conceal_for:
            kp[10] = (400.0, 300.0, 0.9)  # right wrist
            kp[12] = (400.0, 300.0, 0.9)  # right hip → concealment
            person = PersonDet(box=box, score=0.9, keypoints=kp)
            return DetectResult([person], [ItemDet("handbag", (390.0, 290.0, 415.0, 315.0), 0.8)])
        return DetectResult([PersonDet(box=box, score=0.9, keypoints=kp)], [])


class _SpyRecorder(EdgeClipRecorder):
    def __init__(self) -> None:  # no super().__init__ — avoid ffmpeg/dirs
        self.episodes: list[SuspiciousEpisode] = []

    def on_episode(self, episode: SuspiciousEpisode) -> None:  # type: ignore[override]
        self.episodes.append(episode)


def test_pipeline_frame_skip_and_overlay_shape() -> None:
    det = _CountingDetector()
    pipe = EdgePipeline("cam01", det, frame_skip=3)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    out = frame
    for i in range(9):
        out = pipe.process(frame, now=i * 0.1)
    assert det.calls == 3  # detected on frames 0, 3, 6
    assert out.shape == frame.shape
    assert bool((frame == 0).all())  # source untouched (drew on a copy)


def test_pipeline_hands_closed_episode_to_recorder() -> None:
    spy = _SpyRecorder()
    pipe = EdgePipeline("cam03", _SeqDetector(conceal_for=8), spy, frame_skip=1)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    t = 0.0
    for _ in range(120):
        pipe.process(frame, now=t)
        t += 0.2
        if spy.episodes:
            break
    assert len(spy.episodes) == 1
    ep = spy.episodes[0]
    assert ep.camera_id == "cam03"
    assert "conceal" in ep.behaviors
