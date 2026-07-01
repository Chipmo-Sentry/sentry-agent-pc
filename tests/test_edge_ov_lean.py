"""Lean raw-OpenVINO decode tests — verify the numpy post-processing (letterbox
undo, NMS, keypoint decode) against synthetic YOLO11 output tensors. No openvino
/ GPU / model needed, so the shipped decode path is covered in CI."""

from __future__ import annotations

import json

import numpy as np

from sentry_agent_pc.edge.ov_lean import (
    _nms,
    _xywh_to_xyxy_scaled,
    decode_det_output,
    decode_openvocab_output,
    decode_pose_output,
    letterbox,
    load_vocab,
)


def test_nms_suppresses_overlap_keeps_separate() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    keep = _nms(boxes, scores, iou_thresh=0.5)
    assert keep[0] == 0  # highest score first
    assert 1 not in keep  # overlapping box suppressed
    assert 2 in keep  # far box survives


def test_letterbox_shape_scale_pad() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)  # 640x480
    blob, scale, pad_x, pad_y = letterbox(frame, 640)
    assert blob.shape == (1, 3, 640, 640)
    assert abs(scale - 1.0) < 1e-6  # min(640/640, 640/480) = 1
    assert abs(pad_x - 0.0) < 1e-6
    assert abs(pad_y - 80.0) < 1e-6  # (640-480)/2


def test_xywh_to_xyxy_scaled_undoes_letterbox() -> None:
    x1, y1, x2, y2 = _xywh_to_xyxy_scaled(
        np.array([320, 320, 40, 40], dtype=np.float32), scale=0.5, pad_x=10.0, pad_y=20.0
    )
    assert abs(x1 - (320 - 20 - 10) / 0.5) < 1e-3
    assert abs(y2 - (320 + 20 - 20) / 0.5) < 1e-3


def test_decode_pose_filters_nms_and_decodes_keypoints() -> None:
    raw = np.zeros((1, 56, 3), dtype=np.float32)
    # anchor 0 — a confident person at (cx,cy,w,h)=(100,100,40,120) → xyxy (80,40,120,160)
    raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0] = 100, 100, 40, 120
    raw[0, 4, 0] = 0.9
    raw[0, 5, 0], raw[0, 6, 0], raw[0, 7, 0] = 110, 90, 0.8  # keypoint 0 (x, y, v)
    # anchor 1 — overlapping person, lower score → NMS-suppressed
    raw[0, 0, 1], raw[0, 1, 1], raw[0, 2, 1], raw[0, 3, 1] = 102, 101, 40, 120
    raw[0, 4, 1] = 0.85
    # anchor 2 — below conf → dropped
    raw[0, 4, 2] = 0.1

    persons = decode_pose_output(raw, scale=1.0, pad_x=0.0, pad_y=0.0, conf=0.35, iou=0.5)
    assert len(persons) == 1
    p = persons[0]
    assert abs(p.box[0] - 80) < 1 and abs(p.box[2] - 120) < 1
    assert abs(p.score - 0.9) < 1e-6  # the higher-scored anchor kept
    assert p.keypoints is not None and p.keypoints.shape == (17, 3)
    assert abs(p.keypoints[0, 0] - 110) < 1 and abs(p.keypoints[0, 1] - 90) < 1


def test_decode_det_keeps_only_item_classes() -> None:
    raw = np.zeros((1, 84, 2), dtype=np.float32)
    # anchor 0 — handbag (class 26) at (200,200,30,30) → xyxy (185,185,215,215)
    raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0] = 200, 200, 30, 30
    raw[0, 4 + 26, 0] = 0.8
    # anchor 1 — person (class 0), high score, but NOT an item → filtered out
    raw[0, 4 + 0, 1] = 0.95

    items = decode_det_output(raw, scale=1.0, pad_x=0.0, pad_y=0.0, conf=0.40)
    assert len(items) == 1
    assert items[0].label == "handbag"
    assert abs(items[0].box[0] - 185) < 1


def test_decode_pose_zeros_low_confidence_keypoints() -> None:
    raw = np.zeros((1, 56, 2), dtype=np.float32)  # 2 anchors so squeeze keeps 2-D
    raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0] = 100, 100, 40, 120
    raw[0, 4, 0] = 0.9
    # keypoint 0 confident (v=0.8), keypoint 1 below threshold (v=0.1)
    raw[0, 5, 0], raw[0, 6, 0], raw[0, 7, 0] = 110, 90, 0.8
    raw[0, 8, 0], raw[0, 9, 0], raw[0, 10, 0] = 120, 95, 0.1
    raw[0, 4, 1] = 0.1  # anchor 1 below conf → dropped

    persons = decode_pose_output(raw, 1.0, 0.0, 0.0, conf=0.35, min_kp_conf=0.30)
    assert len(persons) == 1
    kp = persons[0].keypoints
    assert kp is not None
    assert kp[0, 0] != 0.0  # confident keypoint kept
    assert tuple(kp[1]) == (0.0, 0.0, 0.0)  # low-conf keypoint zeroed out


def test_decode_handles_empty_and_malformed() -> None:
    assert decode_pose_output(np.zeros((1, 56, 4), np.float32), 1.0, 0.0, 0.0, conf=0.9) == []
    assert decode_det_output(np.zeros((1, 84, 4), np.float32), 1.0, 0.0, 0.0, conf=0.9) == []
    assert decode_pose_output(np.zeros((1, 10, 4), np.float32), 1.0, 0.0, 0.0) == []  # wrong shape


# --- open-vocabulary item decode (YOLOE / YOLO-World) -------------------------

_VOCAB = ["bottle", "snack bag", "carton"]  # K=3 → tensor is [1, 4+3, N]


def test_decode_openvocab_labels_from_vocab_and_reads_k_from_tensor() -> None:
    raw = np.zeros((1, 4 + len(_VOCAB), 2), dtype=np.float32)
    # anchor 0 — class 1 ("snack bag") at (200,200,30,30) → xyxy (185,185,215,215)
    raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0] = 200, 200, 30, 30
    raw[0, 4 + 1, 0] = 0.8
    # anchor 1 — below conf → dropped
    raw[0, 4 + 0, 1] = 0.1

    items = decode_openvocab_output(raw, 1.0, 0.0, 0.0, _VOCAB, conf=0.40)
    assert len(items) == 1
    assert items[0].label == "snack bag"  # EVERY class is an item; label = vocab[argmax]
    assert abs(items[0].box[0] - 185) < 1


def test_decode_openvocab_nms_suppresses_overlap() -> None:
    raw = np.zeros((1, 4 + len(_VOCAB), 2), dtype=np.float32)
    raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0] = 100, 100, 40, 40
    raw[0, 4 + 0, 0] = 0.9
    raw[0, 0, 1], raw[0, 1, 1], raw[0, 2, 1], raw[0, 3, 1] = 101, 101, 40, 40
    raw[0, 4 + 0, 1] = 0.8  # overlapping, lower score → suppressed
    items = decode_openvocab_output(raw, 1.0, 0.0, 0.0, _VOCAB, conf=0.40, iou=0.5)
    assert len(items) == 1
    assert abs(items[0].score - 0.9) < 1e-6


def test_decode_openvocab_tolerates_vocab_tensor_mismatch() -> None:
    # Tensor has K=3 classes but vocab.json only lists 2 — the extra class id is
    # dropped, not crashed (a stale vocab.json must not take the detector down).
    raw = np.zeros((1, 4 + 3, 2), dtype=np.float32)
    raw[0, 0, 0], raw[0, 1, 0], raw[0, 2, 0], raw[0, 3, 0] = 50, 50, 20, 20
    raw[0, 4 + 2, 0] = 0.9  # class 2 — out of range for a 2-entry vocab
    raw[0, 0, 1], raw[0, 1, 1], raw[0, 2, 1], raw[0, 3, 1] = 150, 150, 20, 20
    raw[0, 4 + 0, 1] = 0.9  # class 0 — in range
    items = decode_openvocab_output(raw, 1.0, 0.0, 0.0, ["bottle", "carton"], conf=0.40)
    assert len(items) == 1
    assert items[0].label == "bottle"


def test_decode_openvocab_empty_and_malformed() -> None:
    assert (
        decode_openvocab_output(np.zeros((1, 7, 4), np.float32), 1.0, 0.0, 0.0, _VOCAB, conf=0.9)
        == []
    )
    assert (
        decode_openvocab_output(np.zeros((1, 3, 4), np.float32), 1.0, 0.0, 0.0, _VOCAB) == []
    )  # <5 rows


def test_load_vocab_reads_sibling_json(tmp_path) -> None:  # noqa: ANN001 — pytest fixture
    d = tmp_path / "yoloe_items_openvino_model"
    d.mkdir()
    xml = d / "yoloe_items_openvino_model.xml"
    xml.write_text("<net/>", encoding="utf-8")
    (d / "vocab.json").write_text(json.dumps(["bottle", "carton"]), encoding="utf-8")
    assert load_vocab(xml) == ["bottle", "carton"]


def test_load_vocab_missing_or_malformed_returns_none(tmp_path) -> None:  # noqa: ANN001
    d = tmp_path / "m"
    d.mkdir()
    xml = d / "m.xml"
    xml.write_text("<net/>", encoding="utf-8")
    assert load_vocab(xml) is None  # no vocab.json
    (d / "vocab.json").write_text('{"not": "a list"}', encoding="utf-8")
    assert load_vocab(xml) is None  # malformed
