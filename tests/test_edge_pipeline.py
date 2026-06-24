"""Edge pipeline tests — frame-skip, overlay output, episode→recorder handoff."""

from __future__ import annotations

import numpy as np

from sentry_agent_pc.edge.config import EdgeConfig
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
        self.floors: list[float | None] = []

    def submit(self, episode: SuspiciousEpisode) -> None:
        self.episodes.append(episode)

    def set_protect_floor(self, oldest_open_start: float | None) -> None:
        self.floors.append(oldest_open_start)


class _ConfDetector:
    """ConfTunable detector — records the thresholds the pipeline pushes in."""

    def __init__(self) -> None:
        self.pushed: list[tuple[float, float, float]] = []

    def apply_conf(
        self, *, person_conf: float, item_conf: float, min_kp_conf: float
    ) -> None:
        self.pushed.append((person_conf, item_conf, min_kp_conf))

    def detect(self, frame_bgr: np.ndarray) -> DetectResult:
        return DetectResult()


def test_pipeline_pushes_conf_to_detector_on_init_and_apply() -> None:
    det = _ConfDetector()
    EdgePipeline("cam01", det, config=EdgeConfig(person_conf=0.5, item_conf=0.6, min_kp_conf=0.2))
    assert det.pushed[-1] == (0.5, 0.6, 0.2)  # threaded on construction
    pipe = EdgePipeline("cam01", det)
    pipe.apply_config(EdgeConfig(person_conf=0.9, item_conf=0.8, min_kp_conf=0.7))
    assert det.pushed[-1] == (0.9, 0.8, 0.7)  # hot-applied, no longer dead


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
    # Gate-free conceal so the short 8-frame burst opens an episode (this checks the
    # pipeline→recorder handoff, not the score tuning).
    pipe = EdgePipeline(
        "cam03", _SeqDetector(conceal_for=8), spy, frame_skip=1,
        config=EdgeConfig(interval_conceal=0.0, mindur_conceal=0.0),
    )
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
