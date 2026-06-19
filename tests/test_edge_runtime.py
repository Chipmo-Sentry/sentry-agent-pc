"""EdgeRuntime tests — lifecycle, process(), hot-apply. ffmpeg-free: the segment
recorder's ffmpeg spawn is monkeypatched to a no-op."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sentry_agent_pc.edge import recorder as rec_mod
from sentry_agent_pc.edge.config import EdgeConfig
from sentry_agent_pc.edge.detector import Detector, DummyDetector
from sentry_agent_pc.edge.runtime import EdgeRuntime


def _factory(_cfg: EdgeConfig) -> Detector:
    return DummyDetector()


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec_mod.SegmentRecorder, "start", lambda self: None)
    monkeypatch.setattr(rec_mod.SegmentRecorder, "stop", lambda self: None)


def _frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


def test_process_none_until_started_then_overlay(tmp_path: Path) -> None:
    rt = EdgeRuntime(tmp_path, _factory)
    assert rt.process("cam01", _frame(), 0.0) is None  # not started
    rt.start_camera("cam01", "rtsp://x/y")
    out = rt.process("cam01", _frame(), 0.1)
    assert out is not None and out.shape == (480, 640, 3)


def test_start_is_idempotent_and_stop_removes(tmp_path: Path) -> None:
    rt = EdgeRuntime(tmp_path, _factory)
    rt.start_camera("cam01", "rtsp://x")
    rt.start_camera("cam01", "rtsp://x")  # idempotent
    assert len(rt._pipes) == 1
    rt.stop_camera("cam01")
    assert rt.process("cam01", _frame(), 0.2) is None


def test_apply_config_hot_applies_everywhere(tmp_path: Path) -> None:
    rt = EdgeRuntime(tmp_path, _factory)
    rt.start_camera("cam01", "rtsp://x")
    rt.apply_config(EdgeConfig(frame_skip=7, max_clips=9, pre_sec=5.0))
    assert rt._pipes["cam01"].frame_skip == 7
    assert rt._pipes["cam01"].behavior.cfg.frame_skip == 7
    assert rt.store.max_clips == 9
    assert rt._recorders["cam01"].pre == 5.0


def test_clips_reads_store(tmp_path: Path) -> None:
    rt = EdgeRuntime(tmp_path, _factory)
    assert rt.clips() == []
