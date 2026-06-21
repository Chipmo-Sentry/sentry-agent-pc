"""Lean raw-OpenVINO YOLO detector — the SHIPPED edge detector (no ultralytics/
torch). This is what the single sentry-agent-pc installer bundles: the OpenVINO
runtime + the exported model IR live inside the app, so a fresh store PC just
runs the .exe — no pip, no model export by the user.

ultralytics is used ONLY by the P1 bench (detector.OpenVinoYoloDetector) to get
latency numbers; the product never ships it. Here we load the IR with
``openvino`` directly and do the YOLO post-processing (letterbox-undo + NMS +
keypoints) in numpy — unit-tested against synthetic output tensors so the decode
logic is verified without a GPU/model in CI.

YOLO11 export tensor layouts (ultralytics ``yolo export format=openvino``):
  * pose:   [1, 56, N]  → 4 box (cx,cy,w,h) + 1 conf + 17*3 keypoints
  * detect: [1, 84, N]  → 4 box + 80 class scores
Boxes/keypoints are in the letterboxed input space (e.g. 0-640); we scale back.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from sentry_agent_pc.edge.detector import COCO_ITEM_CLASSES, DetectResult, ItemDet, PersonDet
from sentry_agent_pc.logging_setup import get_logger

log = get_logger("sentry_agent_pc.edge.ov_lean")

_IMGSZ = 640


def bundled_model_xml(name: str) -> Path | None:
    """Path to a bundled OpenVINO IR ``bin/<name>/<name>.xml``, or None if absent.

    Mirrors resources.bundled_binary: frozen → <_MEIPASS>/bin, dev → <pkg>/bin.
    build_exe.ps1 drops the exported IR there at build time."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        base = Path(meipass) / "bin" if meipass else None
    else:
        base = Path(__file__).parent.parent / "bin"
    if base is None:
        return None
    xml = base / name / f"{name}.xml"
    return xml if xml.exists() else None


def letterbox(frame_bgr: NDArray[np.uint8], size: int = _IMGSZ) -> tuple[NDArray[np.float32], float, float, float]:
    """Resize keeping aspect ratio + pad to size×size. Returns the NCHW RGB blob
    (0-1) plus (scale, pad_x, pad_y) to map model coords back to the frame."""
    import cv2

    h, w = frame_bgr.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    pad_x, pad_y = (size - nw) / 2.0, (size - nh) / 2.0
    resized = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    top, left = int(round(pad_y)), int(round(pad_x))
    canvas[top : top + nh, left : left + nw] = resized
    rgb = canvas[:, :, ::-1].astype(np.float32) / 255.0
    blob = np.transpose(rgb, (2, 0, 1))[np.newaxis, ...]  # [1,3,H,W]
    return np.ascontiguousarray(blob), scale, pad_x, pad_y


def _nms(boxes: NDArray[np.float32], scores: NDArray[np.float32], iou_thresh: float) -> list[int]:
    """Greedy NMS. boxes are xyxy. Returns kept indices (highest score first)."""
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(1e-9, areas[i] + areas[rest] - inter)
        order = rest[iou <= iou_thresh]
    return keep


def _xywh_to_xyxy_scaled(
    box: NDArray[np.float32], scale: float, pad_x: float, pad_y: float
) -> tuple[float, float, float, float]:
    cx, cy, w, h = box
    x1 = (cx - w / 2 - pad_x) / scale
    y1 = (cy - h / 2 - pad_y) / scale
    x2 = (cx + w / 2 - pad_x) / scale
    y2 = (cy + h / 2 - pad_y) / scale
    return float(x1), float(y1), float(x2), float(y2)


def decode_pose_output(
    raw: NDArray[np.float32], scale: float, pad_x: float, pad_y: float,
    *, conf: float = 0.35, iou: float = 0.5, min_kp_conf: float = 0.30,
) -> list[PersonDet]:
    """[1,56,N] (or [56,N]) → persons with COCO-17 keypoints, frame coords.

    Keypoints below ``min_kp_conf`` are zeroed so downstream consumers
    (overlay.kp_point's x>1/y>1 gate) treat them as undetected — this is where
    the EdgeConfig.min_kp_conf knob actually bites."""
    arr = np.squeeze(raw)
    if arr.ndim != 2 or arr.shape[0] < 56:
        return []
    arr = arr.T  # [N, 56]
    scores = arr[:, 4]
    keep = scores >= conf
    arr, scores = arr[keep], scores[keep]
    if arr.shape[0] == 0:
        return []
    xyxy = np.array(
        [_xywh_to_xyxy_scaled(b, scale, pad_x, pad_y) for b in arr[:, :4]], dtype=np.float32
    )
    out: list[PersonDet] = []
    for idx in _nms(xyxy, scores, iou):
        kp = arr[idx, 5:56].reshape(17, 3).astype(np.float32).copy()
        kp[:, 0] = (kp[:, 0] - pad_x) / scale
        kp[:, 1] = (kp[:, 1] - pad_y) / scale
        kp[kp[:, 2] < min_kp_conf] = 0.0  # drop low-confidence keypoints
        out.append(PersonDet(tuple(xyxy[idx].tolist()), float(scores[idx]), kp))
    return out


def decode_det_output(
    raw: NDArray[np.float32], scale: float, pad_x: float, pad_y: float,
    *, conf: float = 0.40, iou: float = 0.5,
) -> list[ItemDet]:
    """[1,84,N] (or [84,N]) → retail-relevant item boxes, frame coords."""
    arr = np.squeeze(raw)
    if arr.ndim != 2 or arr.shape[0] < 84:
        return []
    arr = arr.T  # [N, 84]
    cls = arr[:, 4:84].argmax(axis=1)
    scores = arr[:, 4:84].max(axis=1)
    item_ids = np.array(sorted(COCO_ITEM_CLASSES))
    keep = (scores >= conf) & np.isin(cls, item_ids)
    arr, scores, cls = arr[keep], scores[keep], cls[keep]
    if arr.shape[0] == 0:
        return []
    xyxy = np.array(
        [_xywh_to_xyxy_scaled(b, scale, pad_x, pad_y) for b in arr[:, :4]], dtype=np.float32
    )
    out: list[ItemDet] = []
    for idx in _nms(xyxy, scores, iou):
        label = COCO_ITEM_CLASSES.get(int(cls[idx]))
        if label is None:
            continue
        out.append(ItemDet(label, tuple(xyxy[idx].tolist()), float(scores[idx])))
    return out


class LeanOpenVinoDetector:
    """Shipped detector: YOLO pose + item via the bundled OpenVINO runtime + IR."""

    def __init__(
        self,
        pose_xml: str | Path | None = None,
        item_xml: str | Path | None = None,
        *,
        device: str = "GPU",
        person_conf: float = 0.35,
        item_conf: float = 0.40,
        min_kp_conf: float = 0.30,
    ) -> None:
        import openvino as ov  # bundled at runtime — lazy so imports work in CI

        pose = Path(pose_xml) if pose_xml else bundled_model_xml("yolo11n-pose_openvino_model")
        item = Path(item_xml) if item_xml else bundled_model_xml("yolo11n_openvino_model")
        if pose is None or item is None:
            raise FileNotFoundError("bundled OpenVINO model IR not found (build_exe must drop it in bin/)")
        core = ov.Core()
        dev = device if device in core.available_devices else "CPU"
        self._pose = self._compile(core, pose, dev)
        self._item = self._compile(core, item, dev)
        self._person_conf = person_conf
        self._item_conf = item_conf
        self._min_kp_conf = min_kp_conf

    @staticmethod
    def _compile(core: Any, xml: Path, dev: str) -> Any:
        """Compile on `dev`, falling back to CPU if the GPU plugin/driver fails.

        ``dev in available_devices`` isn't enough — compile_model can still throw
        on a flaky iGPU driver or OOM. A silent crash would take edge AI down for
        the whole store, so retry on CPU (slower but always present) and surface
        which device actually loaded."""
        try:
            cm = core.compile_model(core.read_model(xml), dev)
            log.info("ov_lean.loaded", device=dev, model=str(xml))
            return cm
        except Exception as e:  # noqa: BLE001 — any OV/driver error → CPU retry
            if dev == "CPU":
                log.error("ov_lean.compile_failed", device=dev, model=str(xml), error=str(e))
                raise
            log.warning("ov_lean.gpu_compile_failed_retry_cpu", model=str(xml), error=str(e))
            cm = core.compile_model(core.read_model(xml), "CPU")
            log.info("ov_lean.loaded", device="CPU", model=str(xml))
            return cm

    def apply_conf(
        self, *, person_conf: float, item_conf: float, min_kp_conf: float
    ) -> None:
        """Hot-apply detection thresholds (config-poller → pipeline.apply_config)."""
        self._person_conf = person_conf
        self._item_conf = item_conf
        self._min_kp_conf = min_kp_conf

    def detect(self, frame_bgr: NDArray[np.uint8]) -> DetectResult:
        blob, scale, pad_x, pad_y = letterbox(frame_bgr)
        pose_raw = np.asarray(self._pose(blob)[self._pose.output(0)], dtype=np.float32)
        item_raw = np.asarray(self._item(blob)[self._item.output(0)], dtype=np.float32)
        persons = decode_pose_output(
            pose_raw, scale, pad_x, pad_y, conf=self._person_conf, min_kp_conf=self._min_kp_conf
        )
        items = decode_det_output(item_raw, scale, pad_x, pad_y, conf=self._item_conf)
        return DetectResult(persons=persons, items=items)
