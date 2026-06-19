"""Edge Stage-1 pipeline (P1+): run YOLO + tracker + behaviour ON the store PC so
only suspicious clips travel to the cloud sentry-ai (VLM) host.

Modules:
  * detector — YOLO pose + item contract; OpenVINO impl (Intel iGPU) + a Dummy.
  * overlay  — pose-polygon mask / trail / wrist→item / risk drawing (cv2 only).

The behaviour engine + clip recorder land in later steps; the detector is kept
swappable so the lean raw-OpenVINO path (P5) drops in without touching callers.
"""
