"""A library of RTSP stream paths across camera brands.

Used by the multi-protocol resolver: when ONVIF can't give us a stream URI
(disabled, no ONVIF user, auth quirk), we brute-force these paths with the
user's credentials and keep the first that returns a real video stream.

Main streams are listed before sub streams so we prefer full resolution.
`{u}`/`{p}` placeholders are filled with URL-encoded credentials where a brand
embeds them in the path (rare); most use userinfo in the URL.
"""

from __future__ import annotations

# Ordered: most-common / main-stream first. Deduped at use time.
RTSP_PATHS: list[str] = [
    # Hikvision
    "/Streaming/Channels/101",
    "/Streaming/Channels/102",
    "/h264/ch1/main/av_stream",
    "/Streaming/Channels/1",
    # Dahua / Amcrest
    "/cam/realmonitor?channel=1&subtype=0",
    "/cam/realmonitor?channel=1&subtype=1",
    # UNV (Uniview)
    "/unicast/c1/s0/live",
    "/unicast/c1/s1/live",
    "/media/video1",
    "/media/video2",
    # Skyworth / XiongMai / Tuya-style
    "/stream1",
    "/stream2",
    "/live/0/main",
    "/live/0/sub",
    "/0/av0",
    "/1/av0",
    "/av0_0",
    "/av0_1",
    # Common generic
    "/11",
    "/12",
    "/onvif1",
    "/onvif2",
    "/stream0",
    "/ch0_0.264",
    "/ch01.264",
    "/live/main",
    "/live/ch0",
    "/video1",
    "/h264",
    "/h265",
    "/1",
    "/main",
    "/sub",
]
