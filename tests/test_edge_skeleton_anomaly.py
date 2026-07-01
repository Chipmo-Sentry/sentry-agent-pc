"""Edge Stage-1.5 skeleton-anomaly scorer — windowing + scoring logic, model-free
(the OpenVINO call is injected), exactly like the ov_lean decode tests."""

from __future__ import annotations

import numpy as np
import pytest

from sentry_agent_pc.edge.detector import DummyDetector
from sentry_agent_pc.edge.pipeline import EdgePipeline
from sentry_agent_pc.edge.skeleton_anomaly import (
    SkeletonAnomalyScorer,
    anomaly_score,
    frame_features,
)


def _kp() -> np.ndarray:
    rng = np.random.default_rng(3)
    xy = rng.uniform(0, 200, size=(17, 2)).astype(np.float32)
    return np.concatenate([xy, np.ones((17, 1), np.float32)], axis=1)


# --- features ---------------------------------------------------------------


def test_frame_features_translation_invariant() -> None:
    kp = _kp()
    base = frame_features(kp, (100.0, 50.0, 140.0, 250.0))
    kp2 = kp.copy()
    kp2[:, :2] += np.array([30.0, -20.0], np.float32)
    moved = frame_features(kp2, (130.0, 30.0, 170.0, 230.0))  # box shifts too
    assert np.allclose(base, moved, atol=1e-5)


def test_frame_features_scale_invariant() -> None:
    kp = _kp()
    base = frame_features(kp, (100.0, 50.0, 140.0, 250.0))  # h=200
    kp3 = kp.copy()
    kp3[:, :2] *= 2.0
    scaled = frame_features(kp3, (200.0, 100.0, 280.0, 500.0))  # h=400
    assert np.allclose(base, scaled, atol=1e-5)


def test_frame_features_missing_joint_to_center() -> None:
    kp = _kp()
    kp[9, 2] = 0.0  # invalid left wrist
    feat = frame_features(kp, (0.0, 0.0, 30.0, 200.0)).reshape(17, 2)
    assert tuple(feat[9]) == (0.0, 0.0)


# --- anomaly score ----------------------------------------------------------


def test_anomaly_score_threshold_maps_to_50_and_caps() -> None:
    w = np.ones((4, 34), np.float32)
    assert anomaly_score(w, w, threshold=1.0) == 0.0  # perfect recon → 0
    # err = mean((0-1)^2) = 1 → 50 * 1/1 = 50 (threshold maps to 50)
    assert anomaly_score(w, np.zeros_like(w), threshold=1.0) == pytest.approx(50.0)
    # err = (3-1)^2 = 4 → 50*4 = 200, capped at 100
    assert anomaly_score(w, np.full_like(w, 3.0), threshold=1.0) == 100.0


# --- scorer windowing (injected model) --------------------------------------


def test_scorer_returns_none_until_window_full() -> None:
    sc = SkeletonAnomalyScorer(infer_fn=lambda w: np.zeros_like(w), length=3, threshold=1.0)
    box = (0.0, 0.0, 30.0, 200.0)
    assert sc.score(1, _kp(), box) is None  # 1/3
    assert sc.score(1, _kp(), box) is None  # 2/3
    s = sc.score(1, _kp(), box)  # 3/3 → a value
    assert s is not None and s >= 0.0


def test_scorer_none_keypoints_is_none() -> None:
    sc = SkeletonAnomalyScorer(infer_fn=lambda w: w, length=1, threshold=1.0)
    assert sc.score(1, None, (0.0, 0.0, 30.0, 200.0)) is None


def test_scorer_cleanup_drops_absent_tracks() -> None:
    sc = SkeletonAnomalyScorer(infer_fn=lambda w: w, length=2, threshold=1.0)
    sc.score(1, _kp(), (0.0, 0.0, 30.0, 200.0))
    sc.score(2, _kp(), (0.0, 0.0, 30.0, 200.0))
    sc.cleanup({2})
    assert 1 not in sc._buffers and 2 in sc._buffers


# --- pipeline integration (shadow) ------------------------------------------


def test_pipeline_no_scorer_when_flag_off() -> None:
    pipe = EdgePipeline("cam", DummyDetector())  # default cfg: flag OFF
    assert pipe._anomaly_scorer is None
    pipe.process(np.zeros((240, 320, 3), np.uint8), now=0.0)
    tracks = pipe.latest_tracks()
    assert tracks and tracks[0]["anomaly"] is None


def test_pipeline_surfaces_anomaly_with_injected_scorer() -> None:
    pipe = EdgePipeline("cam", DummyDetector())
    # Inject a length-1 scorer so it scores on the first analysed frame.
    pipe._anomaly_scorer = SkeletonAnomalyScorer(
        infer_fn=lambda w: np.zeros_like(w), length=1, threshold=1.0
    )
    pipe.process(np.zeros((480, 640, 3), np.uint8), now=0.0)
    tracks = pipe.latest_tracks()
    assert tracks
    assert isinstance(tracks[0]["anomaly"], float)
