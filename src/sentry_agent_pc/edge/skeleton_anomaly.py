"""Edge Stage-1.5: learned skeleton-action anomaly scorer (ADR-0030).

A compact pose-window autoencoder (trained on PoseLift, frame-AUC ~0.715 — beats
the STG-NF paper baseline) runs on the agent CPU via OpenVINO and flags unusual
motion the rule engine misses. Skeleton only (no pixels), ~270 KB, CPU-friendly.

It keeps a rolling window of normalised pose features PER tracked person; once a
person has `length` frames it reconstructs the window and turns the reconstruction
error into a 0-100 anomaly score (the training threshold maps to 50). Shadow by
default (EdgeConfig.skeleton_anomaly_enabled) — surfaced alongside the rule risk
so it can be compared before it influences alerting.

The OpenVINO call is injectable (`infer_fn`) so the windowing + scoring logic is
unit-tested without a model, exactly like ov_lean's decode tests.
"""

from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.skeleton_anomaly")

COCO_17 = 17
FEAT_DIM = COCO_17 * 2
_MIN_SCALE = 1e-3

InferFn = Callable[[NDArray[np.float32]], NDArray[np.float32]]  # (1,T,F) → (1,T,F)


def bundled_onnx(name: str = "skeleton_anomaly") -> Path | None:
    """Path to the bundled ``bin/<name>/<name>.onnx`` (mirrors ov_lean's resolver:
    frozen → <_MEIPASS>/bin, dev → <pkg>/bin). None if absent."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        base = Path(meipass) / "bin" if meipass else None
    else:
        base = Path(__file__).parent.parent / "bin"
    if base is None:
        return None
    onnx = base / name / f"{name}.onnx"
    return onnx if onnx.exists() else None


def frame_features(
    kp: NDArray[np.float32], box: tuple[float, float, float, float]
) -> NDArray[np.float32]:
    """(17,3)[x,y,conf] + person box (x1,y1,x2,y2) → (34,) features, normalised by
    the box centre + height (stable every frame; mirrors the sentry-ai trainer).
    Invalid joints (conf <= 0) collapse to the centre."""
    x1, y1, x2, y2 = (float(v) for v in box)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    scale = (y2 - y1) if (y2 - y1) > _MIN_SCALE else max(x2 - x1, 1.0)
    xy = kp[:, :2].astype(np.float32).copy()
    conf = kp[:, 2] if kp.shape[1] >= 3 else np.ones(COCO_17, dtype=np.float32)
    out = (xy - np.array([cx, cy], dtype=np.float32)) / scale
    out[conf <= 0.0] = 0.0
    return out.reshape(-1).astype(np.float32)


def anomaly_score(window: NDArray[np.float32], recon: NDArray[np.float32], threshold: float) -> float:
    """Window reconstruction error → 0-100 anomaly. The training threshold (95th
    percentile of normal-window error) maps to 50; monotonic, so it preserves the
    model's ranking (the ROC-AUC) while reading on the same scale as rule risk."""
    err = float(((recon - window) ** 2).mean())
    return min(100.0, 50.0 * err / max(threshold, 1e-9))


class SkeletonAnomalyScorer:
    """Per-track rolling-window anomaly scorer over the bundled ONNX (OpenVINO)."""

    def __init__(
        self,
        onnx_path: str | Path | None = None,
        *,
        device: str = "GPU",
        infer_fn: InferFn | None = None,
        length: int | None = None,
        threshold: float | None = None,
    ) -> None:
        if infer_fn is not None:
            # Test / injection path: caller supplies the model fn + window spec.
            self._infer = infer_fn
            self.length = int(length or 24)
            self.threshold = float(threshold if threshold is not None else 1.0)
        else:
            path = Path(onnx_path) if onnx_path else bundled_onnx()
            if path is None:
                raise FileNotFoundError("bundled skeleton_anomaly.onnx not found in bin/")
            meta = _load_meta(path)
            self.length = int(meta.get("length", 24))
            self.threshold = float(meta.get("threshold", 1.0))
            self._infer = _openvino_infer(path, device)
        self._buffers: dict[int, deque[NDArray[np.float32]]] = {}

    def score(
        self, tracker_id: int, kp: NDArray[np.float32] | None, box: tuple[float, float, float, float]
    ) -> float | None:
        """Append this frame's pose to the track's window; return a 0-100 anomaly
        once the window is full, else None (still warming up / no keypoints)."""
        if kp is None:
            return None
        buf = self._buffers.get(tracker_id)
        if buf is None:
            buf = deque(maxlen=self.length)
            self._buffers[tracker_id] = buf
        buf.append(frame_features(kp, box))
        if len(buf) < self.length:
            return None
        window = np.stack(buf).astype(np.float32)
        recon = self._infer(window[np.newaxis, ...])
        return anomaly_score(window, recon[0], self.threshold)

    def cleanup(self, active_ids: set[int]) -> None:
        """Drop window buffers for tracks no longer present (bounded memory)."""
        for tid in [t for t in self._buffers if t not in active_ids]:
            del self._buffers[tid]


def _load_meta(onnx_path: Path) -> dict[str, Any]:
    meta_path = onnx_path.with_suffix(".meta.json")
    if not meta_path.exists():
        log.warning("skeleton_anomaly.no_meta_using_defaults", path=str(meta_path))
        return {}
    try:
        data: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError) as e:
        log.warning("skeleton_anomaly.meta_read_failed", error=str(e))
        return {}


def _openvino_infer(onnx_path: Path, device: str) -> InferFn:
    """Compile the ONNX on `device` (GPU→CPU fallback) → an infer callable."""
    import openvino as ov

    core = ov.Core()
    dev = device if device in core.available_devices else "CPU"
    try:
        compiled = core.compile_model(core.read_model(onnx_path), dev)
        log.info("skeleton_anomaly.loaded", device=dev, model=str(onnx_path))
    except Exception as e:  # noqa: BLE001 — flaky iGPU driver → CPU retry
        if dev == "CPU":
            raise
        log.warning("skeleton_anomaly.gpu_failed_retry_cpu", error=str(e))
        compiled = core.compile_model(core.read_model(onnx_path), "CPU")

    def _infer(window: NDArray[np.float32]) -> NDArray[np.float32]:
        return np.asarray(compiled(window)[compiled.output(0)], dtype=np.float32)

    return _infer
