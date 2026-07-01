"""Diagnose live-view lag + artefacts: hardware vs software decode, side by side.

The offline grid (`local_view.py`) decodes every camera with OpenCV's *software*
FFmpeg path. On a store PC that also runs the ffmpeg push relays, software decode
of several cameras at once can saturate the CPU — the decoder then falls behind
the stream bitrate, drops packets mid-GOP, and you see macroblock smearing
("сариналт") plus a growing delay vs the vendor app.

This script measures, per camera, BOTH ways for a few seconds and prints:

  * whether OpenCV could enable hardware (iGPU/GPU) decode at all on THIS box,
  * sustained decode FPS for software vs hardware,
  * a verdict on which fix to ship (OpenCV-hwaccel vs ffmpeg-subprocess).

Run ON THE STORE PC (same machine + LAN as the cameras):

    .venv\\Scripts\\python.exe scripts\\diagnose_hwaccel.py

Optionally pass one RTSP URL to test just that (works even without the saved
state, e.g. from a built install):

    .venv\\Scripts\\python.exe scripts\\diagnose_hwaccel.py "rtsp://user:pass@192.168.1.50/stream1"

Credentials are never printed.
"""

from __future__ import annotations

import os
import sys
import time

# Match local_view: force RTSP-over-TCP before the first VideoCapture.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

_MEASURE_SEC = 6.0  # long enough to ride past the first keyframe + warm up


def _redact(url: str) -> str:
    """host[:port] only — strip scheme, credentials and path."""
    after_scheme = url.split("://", 1)[-1]
    after_creds = after_scheme.split("@", 1)[-1]
    return after_creds.split("/", 1)[0]


def _measure(url: str, *, hwaccel: bool) -> dict[str, object]:
    """Open `url` once (HW or SW) and read for _MEASURE_SEC; return stats."""
    import cv2

    if hwaccel:
        params = [int(cv2.CAP_PROP_HW_ACCELERATION), int(cv2.VIDEO_ACCELERATION_ANY)]
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    else:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    if not cap.isOpened():
        cap.release()
        return {"opened": False}

    # What did OpenCV actually negotiate? >0 means a HW path is in use.
    try:
        hw_val = float(cap.get(cv2.CAP_PROP_HW_ACCELERATION))
    except Exception:  # noqa: BLE001
        hw_val = -1.0

    frames = 0
    size: tuple[int, int] | None = None
    t0 = time.monotonic()
    first_frame_at: float | None = None
    while time.monotonic() - t0 < _MEASURE_SEC:
        ok, frame = cap.read()
        if ok and frame is not None:
            if first_frame_at is None:
                first_frame_at = time.monotonic()
                h, w = frame.shape[:2]
                size = (w, h)
            frames += 1
        else:
            time.sleep(0.005)
    elapsed = time.monotonic() - (first_frame_at or t0)
    cap.release()

    fps = frames / elapsed if elapsed > 0 and frames else 0.0
    return {
        "opened": True,
        "hw_val": hw_val,
        "fps": fps,
        "frames": frames,
        "size": size,
        "ttf": (first_frame_at - t0) if first_frame_at else None,  # time-to-first-frame
    }


def _report(name: str, url: str) -> None:
    print(f"── {name}  [{_redact(url)}]")

    sw = _measure(url, hwaccel=False)
    if not sw.get("opened"):
        print("   SW : ❌ нээгдсэнгүй (URL/нэр-нууц/сүлжээ шалга)")
        print()
        return
    w, h = sw.get("size") or (0, 0)
    print(
        f"   SW (software): {sw['fps']:.1f} fps  ·  {w}x{h}  ·  эхний frame {sw.get('ttf', 0):.1f}s"
    )

    hw = _measure(url, hwaccel=True)
    if not hw.get("opened"):
        print("   HW (hardware): ❌ нээгдсэнгүй → ffmpeg-subprocess хувилбар хэрэгтэй")
        print()
        return
    hw_on = float(hw.get("hw_val", 0) or 0) > 0
    flag = "✅ идэвхжсэн" if hw_on else "⚠️ үл дэмжсэн (CAP_PROP_HW_ACCELERATION=0)"
    print(f"   HW (hardware): {hw['fps']:.1f} fps  ·  hwaccel {flag}")

    # Verdict for this camera.
    if hw_on and hw["fps"] >= sw["fps"] * 0.9:
        print("   → OpenCV hwaccel ажиллаж байна. Энэ замаар засвар хийнэ. ✅")
    elif not hw_on:
        print(
            "   → Энэ opencv-python build hwaccel дэмжихгүй. "
            "ffmpeg-subprocess (-hwaccel d3d11va) хувилбар руу шилжинэ."
        )
    else:
        print("   → hwaccel нээгдсэн ч fps нэмэгдсэнгүй — ffmpeg-subprocess илүү найдвартай.")
    print()


def _load_saved_urls() -> list[tuple[str, str]]:
    try:
        from sentry_agent_pc.state import load_state
    except Exception as e:  # noqa: BLE001
        print(f"(state ачаалж чадсангүй: {e})")
        return []
    return [(c.name, c.rtsp_url) for c in load_state().cameras if c.rtsp_url]


def main() -> int:
    print("\nLive-view декод оношилгоо — software vs hardware\n")
    if len(sys.argv) > 1:
        cams = [(f"arg-{i + 1}", u) for i, u in enumerate(sys.argv[1:])]
    else:
        cams = _load_saved_urls()
    if not cams:
        print("Камер алга. RTSP URL-ыг аргумент болгож дамжуулна уу, эсвэл эхлээд камер бүртгэ.")
        return 1
    for name, url in cams:
        _report(name, url)
    print(
        "Тайлбар:\n"
        "  • HW fps ≈ SW fps бөгөөд hwaccel ✅  → OpenCV hwaccel-ээр засна (хамгийн хялбар).\n"
        "  • hwaccel ⚠️/❌  → ffmpeg-subprocess -hwaccel d3d11va хувилбар.\n"
        "  • SW fps аль хэдийн камерын бодит fps-тэй тэнцүү байвал асуудал нь декод биш,\n"
        "    харин буфер/UI fps (_TARGET_FPS) → B+C+D засвар хангалттай."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
