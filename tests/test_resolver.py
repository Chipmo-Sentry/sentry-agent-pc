"""Multi-protocol resolver scoring + H.265 transcode decision."""

from __future__ import annotations

from sentry_agent_pc.discovery.rtsp_paths import RTSP_PATHS
from sentry_agent_pc.services import discovery_service as svc
from sentry_agent_pc.streaming.pusher import PushTarget


def test_score_prefers_h264_over_hevc() -> None:
    # Same resolution → H.264 wins (browser-friendly).
    assert svc._score("h264", 1920, 1080) > svc._score("hevc", 1920, 1080)


def test_score_prefers_higher_resolution() -> None:
    assert svc._score("h264", 2688, 1520) > svc._score("h264", 720, 576)
    # Resolution beats codec only at equal codec; H.264 main vs H.264 sub:
    assert svc._score("h264", 2560, 1440) > svc._score("h264", 352, 288)


def test_rtsp_paths_cover_known_brands() -> None:
    # The paths that actually worked on the live cameras must be present.
    assert "/Streaming/Channels/101" in RTSP_PATHS  # Hikvision
    assert "/media/video1" in RTSP_PATHS            # UNV
    assert "/stream1" in RTSP_PATHS                 # Skyworth (H.265)


def test_push_target_transcodes_only_h265() -> None:
    assert PushTarget("p", "rtsp://x", codec="hevc").needs_transcode is True
    assert PushTarget("p", "rtsp://x", codec="h265").needs_transcode is True
    assert PushTarget("p", "rtsp://x", codec="h264").needs_transcode is False
    assert PushTarget("p", "rtsp://x", codec=None).needs_transcode is False


def test_resolved_stream_is_h264_flag() -> None:
    assert svc.ResolvedStream(ok=True, codec="h264").is_h264 is True
    assert svc.ResolvedStream(ok=True, codec="hevc").is_h264 is False
