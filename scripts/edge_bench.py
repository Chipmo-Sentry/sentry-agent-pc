"""P1 edge benchmark — measure decode + YOLO(OpenVINO) + overlay latency & RAM.

Run on the TARGET store PC to validate edge Stage-1 feasibility for ONE camera.

  # loop overhead only (no model needed):
  python scripts/edge_bench.py --source clip.mp4 --detector dummy

  # real YOLO latency on the Intel iGPU (needs ultralytics + openvino + exported
  # models — see edge/detector.OpenVinoYoloDetector):
  python scripts/edge_bench.py --detector openvino \
      --source "rtsp://admin:Admin123@192.168.1.64:554/Streaming/Channels/101"

`--frame-skip N` runs YOLO every Nth frame (Stage-1 doesn't need full fps).
`--show` opens a preview window with the overlays.
"""

from __future__ import annotations

import argparse
import time
from collections import deque

import cv2
import numpy as np

from sentry_agent_pc.edge import overlay as ov
from sentry_agent_pc.edge.detector import Detector, DetectResult, DummyDetector


def _rss_mb() -> float | None:
    try:
        import psutil

        return float(psutil.Process().memory_info().rss) / 1e6
    except Exception:  # noqa: BLE001 — psutil is optional, RAM read is best-effort
        return None


def _build_detector(kind: str) -> Detector:
    if kind == "openvino":
        from sentry_agent_pc.edge.detector import OpenVinoYoloDetector

        return OpenVinoYoloDetector()
    return DummyDetector()


def main() -> None:
    ap = argparse.ArgumentParser(description="Edge Stage-1 latency/RAM benchmark")
    ap.add_argument("--source", required=True, help="RTSP URL or video file path")
    ap.add_argument("--detector", choices=["dummy", "openvino"], default="dummy")
    ap.add_argument("--frame-skip", type=int, default=3, help="run YOLO every Nth frame")
    ap.add_argument("--frames", type=int, default=300, help="frames to process then stop")
    ap.add_argument("--show", action="store_true", help="preview window with overlays")
    args = ap.parse_args()

    det = _build_detector(args.detector)
    cap = cv2.VideoCapture(args.source, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit(f"cannot open source: {args.source}")

    t_dec = t_inf = t_draw = 0.0
    n = n_inf = 0
    last = DetectResult()
    trail: deque[tuple[int, int]] = deque(maxlen=ov.TRAIL_MAXLEN)
    start = time.perf_counter()

    while n < args.frames:
        t0 = time.perf_counter()
        ok, frame = cap.read()
        if not ok:
            break
        t1 = time.perf_counter()
        t_dec += t1 - t0

        if n % max(1, args.frame_skip) == 0:
            last = det.detect(frame)
            n_inf += 1
        t2 = time.perf_counter()
        t_inf += t2 - t1

        if last.persons:
            b = last.persons[0].box
            trail.append((int((b[0] + b[2]) / 2), int(b[3])))
        trails = [np.array(trail, dtype=np.int32)] if len(trail) >= 2 else None
        annotated = ov.draw_overlays(frame, last.persons, last.items, trails=trails)
        t_draw += time.perf_counter() - t2

        if args.show:
            cv2.imshow("edge bench", annotated)
            if cv2.waitKey(1) & 0xFF == 27:
                break
        n += 1

    wall = time.perf_counter() - start
    cap.release()
    if args.show:
        cv2.destroyAllWindows()

    print(f"source     {args.source}")
    print(f"detector   {args.detector}  frame-skip={args.frame_skip}")
    print(f"frames     {n}  wall {wall:.1f}s  -> {n / max(1e-6, wall):.1f} fps end-to-end")
    print(f"decode     {1000 * t_dec / max(1, n):.1f} ms/frame")
    print(f"inference  {1000 * t_inf / max(1, n_inf):.1f} ms/run  ({n_inf} runs)")
    print(f"overlay    {1000 * t_draw / max(1, n):.1f} ms/frame")
    rss = _rss_mb()
    if rss is not None:
        print(f"process RSS {rss:.0f} MB")


if __name__ == "__main__":
    main()
