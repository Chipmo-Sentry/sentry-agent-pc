"""EdgePipeline.latest_tracks() — the cloud live-overlay track shape (edge-first
P2b). A fake detector feeds one person so no camera / OpenVINO is needed."""

from __future__ import annotations

import numpy as np

from sentry_agent_pc.edge.detector import DetectResult, PersonDet
from sentry_agent_pc.edge.pipeline import EdgePipeline


class _FakeDetector:
    def __init__(self, box: tuple[float, float, float, float]) -> None:
        self._box = box

    def detect(self, _frame: np.ndarray) -> DetectResult:
        kp = np.zeros((17, 3), dtype=np.float32)
        return DetectResult(persons=[PersonDet(box=self._box, score=0.9, keypoints=kp)], items=[])


def test_latest_tracks_empty_before_first_frame() -> None:
    pipe = EdgePipeline("cam", _FakeDetector((10, 20, 30, 40)), recorder=None)
    assert pipe.latest_tracks() == []


def test_latest_tracks_shape_matches_livetrack() -> None:
    box = (100.0, 50.0, 200.0, 400.0)
    pipe = EdgePipeline("cam", _FakeDetector(box), recorder=None)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    pipe.process(frame, now=1000.0)

    tracks = pipe.latest_tracks()
    assert len(tracks) == 1
    t = tracks[0]
    # Required LiveTrack fields (backend schema): person_id:int, box:[4 floats]
    assert isinstance(t["person_id"], int)
    assert [float(v) for v in t["box"]] == [100.0, 50.0, 200.0, 400.0]
    # Display fields
    assert t["color"] in {"green", "yellow", "red"}
    assert isinstance(t["risk_pct"], float)
    assert isinstance(t["behaviors"], list)


def test_latest_tracks_person_id_stable_across_frames() -> None:
    box = (100.0, 50.0, 200.0, 400.0)
    pipe = EdgePipeline("cam", _FakeDetector(box), recorder=None)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    pipe.process(frame, now=1000.0)
    pid1 = pipe.latest_tracks()[0]["person_id"]
    pipe.process(frame, now=1000.1)  # same person, next frame
    pid2 = pipe.latest_tracks()[0]["person_id"]
    assert pid1 == pid2  # stable id → no overlay flicker
