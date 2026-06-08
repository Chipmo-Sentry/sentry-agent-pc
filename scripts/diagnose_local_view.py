"""Diagnose the offline LAN live view: why is a camera tile black?

The live grid decodes each stored RTSP URL with OpenCV. A stream that ffmpeg
probed fine at registration can still fail to render here — most often because
the camera caps concurrent RTSP sessions, or OpenCV's prebuilt FFMPEG chokes on
a codec/profile. This script opens every saved camera BOTH ways and prints a
side-by-side verdict so the cause is obvious.

Run on the agent PC (same machine, same LAN as the cameras):

    .venv\\Scripts\\python.exe scripts\\diagnose_local_view.py

Credentials are never printed.
"""

from __future__ import annotations

import os
import time

# Match local_view: force RTSP-over-TCP before the first VideoCapture.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

from sentry_agent_pc.discovery import rtsp_probe  # noqa: E402
from sentry_agent_pc.gui.local_view import _redact_host  # noqa: E402
from sentry_agent_pc.state import load_state  # noqa: E402

_OPENCV_READ_BUDGET_SEC = 10.0


def _try_opencv(url: str) -> str:
    """Open with OpenCV exactly like local_view does; return a one-line verdict."""
    import cv2

    t0 = time.monotonic()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        return "❌ OpenCV нээж чадсангүй (VideoCapture.isOpened()=False)"

    frames = 0
    first_size = None
    while time.monotonic() - t0 < _OPENCV_READ_BUDGET_SEC:
        ok, frame = cap.read()
        if ok and frame is not None:
            frames += 1
            if first_size is None:
                h, w = frame.shape[:2]
                first_size = (w, h)
            if frames >= 5:  # enough to call it live
                break
    cap.release()

    if frames == 0:
        return (
            f"❌ Нээгдсэн ч {_OPENCV_READ_BUDGET_SEC:.0f}s дотор НЭГ Ч frame уншаагүй "
            "(зэрэгцээ session хязгаар / codec / keyframe хүлээж байх магадлал)"
        )
    w, h = first_size or (0, 0)
    return f"✅ OpenCV-ээр {frames} frame уншсан, {w}x{h}"


def main() -> int:
    cams = [c for c in load_state().cameras if c.rtsp_url]
    if not cams:
        print("RTSP-тэй камер алга. Эхлээд 'Камер хайх (Scan)' хийгээрэй.")
        return 1

    print(f"\n{len(cams)} камер шалгаж байна (ffmpeg ба OpenCV хоёроор)...\n")
    bad = 0
    for cam in cams:
        host = _redact_host(cam.rtsp_url)
        print(f"── {cam.name}  [{host}]")

        pr = rtsp_probe.probe(cam.rtsp_url, timeout_sec=8)
        if pr.ok:
            print(f"   ffmpeg : ✅ {pr.codec} {pr.width}x{pr.height}")
        elif pr.is_auth_error:
            print("   ffmpeg : ❌ 401 — нэр/нууц үг буруу")
        else:
            print(f"   ffmpeg : ❌ {pr.error or 'тодорхойгүй алдаа'}")

        verdict = _try_opencv(cam.rtsp_url)
        print(f"   OpenCV : {verdict}")
        if verdict.startswith("❌"):
            bad += 1
        print()

    if bad:
        print(
            f"{bad}/{len(cams)} камер OpenCV-ээр гарсангүй.\n"
            "  • ffmpeg ✅ + OpenCV ❌  → зэрэгцээ RTSP session хязгаар эсвэл codec.\n"
            "    Шийдэл: sub-stream рүү шилжих, эсвэл agent push-ыг түр зогсоох.\n"
            "  • ffmpeg ❌ + OpenCV ❌  → камер/сүлжээ/нэр-нууц үг."
        )
    else:
        print("Бүх камер OpenCV-ээр амжилттай — live view дүрс гаргах ёстой.")
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
