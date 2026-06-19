"""Edge Stage-1 detector: contract + implementations.

Output mirrors what the (ported) behaviour engine expects — per-person box +
COCO-17 keypoints, plus item boxes — so the engine is reused unchanged.

  * DummyDetector        — synthetic output, no model/GPU (tests + loop bench)
  * OpenVinoYoloDetector — YOLO pose + item via OpenVINO (Intel iGPU), the P1
                           latency/RAM measurement path on the target store PC.

The OpenVINO impl uses the ultralytics OpenVINO backend for P1 (export +
post-processing handled, inference on the iGPU). The lean raw-OpenVINO
replacement — no ultralytics/torch, for the shipped lean installer — is P5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

# COCO class IDs relevant to retail shrink (mirror sentry-ai yolo_det.COCO_ITEM_CLASSES).
COCO_ITEM_CLASSES: dict[int, str] = {
    24: "backpack",
    26: "handbag",
    28: "suitcase",
    39: "bottle",
    63: "laptop",
    64: "mouse",
    65: "remote",
    66: "keyboard",
    67: "cell phone",
    73: "book",
}


@dataclass(slots=True)
class PersonDet:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 (pixels)
    score: float
    keypoints: NDArray[np.float32] | None = None  # (17, 3): x, y, conf — COCO order


@dataclass(slots=True)
class ItemDet:
    label: str
    box: tuple[float, float, float, float]
    score: float


@dataclass(slots=True)
class DetectResult:
    persons: list[PersonDet] = field(default_factory=list)
    items: list[ItemDet] = field(default_factory=list)


class Detector(Protocol):
    """Anything that turns a BGR frame into persons (+keypoints) and item boxes."""

    def detect(self, frame_bgr: NDArray[np.uint8]) -> DetectResult: ...


def synthetic_person_kp(cx: float, top: float, person_h: float) -> NDArray[np.float32]:
    """A rough COCO-17 standing pose, scaled to (cx, top, person_h). conf=0.9."""
    ph = person_h

    def pt(dx: float, fy: float) -> tuple[float, float, float]:
        return (cx + dx, top + fy * ph, 0.9)

    rows = [
        pt(0.0, 0.06),  # 0 nose
        pt(-0.03, 0.05),  # 1 l eye
        pt(0.03, 0.05),  # 2 r eye
        pt(-0.05, 0.06),  # 3 l ear
        pt(0.05, 0.06),  # 4 r ear
        pt(-0.13, 0.20),  # 5 l shoulder
        pt(0.13, 0.20),  # 6 r shoulder
        pt(-0.17, 0.34),  # 7 l elbow
        pt(0.17, 0.34),  # 8 r elbow
        pt(-0.18, 0.46),  # 9 l wrist
        pt(0.18, 0.46),  # 10 r wrist
        pt(-0.09, 0.52),  # 11 l hip
        pt(0.09, 0.52),  # 12 r hip
        pt(-0.10, 0.72),  # 13 l knee
        pt(0.10, 0.72),  # 14 r knee
        pt(-0.09, 0.96),  # 15 l ankle
        pt(0.09, 0.96),  # 16 r ankle
    ]
    return np.array(rows, dtype=np.float32)


class DummyDetector:
    """Deterministic synthetic detector — one standing person + one item near the
    right wrist. No model/GPU; used by tests and the loop-overhead benchmark."""

    def detect(self, frame_bgr: NDArray[np.uint8]) -> DetectResult:
        h, w = frame_bgr.shape[:2]
        cx = w * 0.5
        top = h * 0.18
        ph = h * 0.92 - top
        kp = synthetic_person_kp(cx, top, ph)
        person = PersonDet(
            box=(cx - ph * 0.20, top, cx + ph * 0.20, top + ph),
            score=0.9,
            keypoints=kp,
        )
        wx, wy = float(kp[10, 0]), float(kp[10, 1])
        item = ItemDet(label="handbag", box=(wx + 6, wy - 8, wx + 30, wy + 14), score=0.7)
        return DetectResult(persons=[person], items=[item])


class OpenVinoYoloDetector:
    """YOLO pose + item via the ultralytics OpenVINO backend (P1 measurement).

    Inference runs on the Intel iGPU through OpenVINO; ultralytics handles the
    export + post-processing. NOT a core agent dependency — install for the
    bench only: ``pip install ultralytics openvino``. Export the models once::

        yolo export model=yolo11n-pose.pt format=openvino
        yolo export model=yolo11n.pt      format=openvino

    P5 swaps this for raw OpenVINO (no ultralytics/torch) behind the same
    `Detector` contract, so the worker/overlay never change.
    """

    def __init__(
        self,
        pose_model: str = "yolo11n-pose_openvino_model",
        item_model: str = "yolo11n_openvino_model",
        *,
        device: str = "intel:gpu",
        person_conf: float = 0.35,
        item_conf: float = 0.40,
        imgsz: int = 640,
    ) -> None:
        from ultralytics import YOLO  # lazy — keep ultralytics off the core agent

        self._pose = YOLO(pose_model, task="pose")
        self._item = YOLO(item_model, task="detect")
        self._device = device
        self._person_conf = person_conf
        self._item_conf = item_conf
        self._imgsz = imgsz
        self._item_classes = list(COCO_ITEM_CLASSES.keys())

    def detect(self, frame_bgr: NDArray[np.uint8]) -> DetectResult:
        persons = self._detect_persons(frame_bgr)
        items = self._detect_items(frame_bgr)
        return DetectResult(persons=persons, items=items)

    def _detect_persons(self, frame_bgr: NDArray[np.uint8]) -> list[PersonDet]:
        res = self._pose.predict(
            frame_bgr,
            conf=self._person_conf,
            imgsz=self._imgsz,
            device=self._device,
            verbose=False,
        )
        out: list[PersonDet] = []
        if not res:
            return out
        r = res[0]
        boxes = r.boxes
        if boxes is None or len(boxes) == 0:
            return out
        xyxy = boxes.xyxy.cpu().numpy()
        conf = boxes.conf.cpu().numpy()
        kdata = r.keypoints.data.cpu().numpy() if r.keypoints is not None else None
        for i in range(xyxy.shape[0]):
            kp = kdata[i].astype(np.float32) if kdata is not None else None
            x1, y1, x2, y2 = (float(v) for v in xyxy[i].tolist())
            out.append(PersonDet((x1, y1, x2, y2), float(conf[i]), kp))
        return out

    def _detect_items(self, frame_bgr: NDArray[np.uint8]) -> list[ItemDet]:
        res = self._item.predict(
            frame_bgr,
            conf=self._item_conf,
            imgsz=self._imgsz,
            device=self._device,
            classes=self._item_classes,
            verbose=False,
        )
        out: list[ItemDet] = []
        if not res:
            return out
        r = res[0]
        boxes = r.boxes
        if boxes is None or len(boxes) == 0:
            return out
        xyxy = boxes.xyxy.cpu().numpy()
        conf = boxes.conf.cpu().numpy()
        cls = boxes.cls.cpu().numpy().astype(int)
        for i in range(xyxy.shape[0]):
            label = COCO_ITEM_CLASSES.get(int(cls[i]))
            if label is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in xyxy[i].tolist())
            out.append(ItemDet(label, (x1, y1, x2, y2), float(conf[i])))
        return out
