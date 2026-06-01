"""Brand-specific RTSP URL templates for manual add.

If ONVIF discovery fails or returns no profile URI, the user picks a brand
from this list and supplies IP + credentials. The template generates main +
sub URLs; the RTSP prober tests both and picks the first H.264-capable one.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass


@dataclass(slots=True)
class BrandTemplate:
    """One brand's RTSP URL pattern. {host} and credentials filled per-camera."""

    key: str
    label: str
    main_path: str
    sub_path: str | None = None
    default_port: int = 554
    notes_mn: str = ""


BRAND_TEMPLATES: dict[str, BrandTemplate] = {
    "hikvision": BrandTemplate(
        key="hikvision",
        label="Hikvision DS-series",
        main_path="/Streaming/Channels/101",
        sub_path="/Streaming/Channels/102",
        notes_mn="Web UI → Network → Integration Protocol → RTSP идэвхжсэн байх ёстой.",
    ),
    "dahua": BrandTemplate(
        key="dahua",
        label="Dahua / Imou",
        main_path="/cam/realmonitor?channel=1&subtype=0",
        sub_path="/cam/realmonitor?channel=1&subtype=1",
    ),
    "unv_modern": BrandTemplate(
        key="unv_modern",
        label="UNV (Uniview) — шинэ firmware",
        main_path="/unicast/c1/s0/live",
        sub_path="/unicast/c1/s1/live",
        notes_mn="Encoding нь H.264 байх ёстой (web UI Video → Encoding).",
    ),
    "unv_legacy": BrandTemplate(
        key="unv_legacy",
        label="UNV — хуучин firmware",
        main_path="/media/video1",
        sub_path="/media/video2",
    ),
    "tplink": BrandTemplate(
        key="tplink",
        label="TP-Link / Tapo",
        main_path="/stream1",
        sub_path="/stream2",
        notes_mn="Tapo app дотор Advanced Settings → Camera Account идэвхжүүлэх шаардлагатай.",
    ),
    "reolink": BrandTemplate(
        key="reolink",
        label="Reolink",
        main_path="/h264Preview_01_main",
        sub_path="/h264Preview_01_sub",
        notes_mn="Зарим model cloud-only, RTSP дэмжихгүй.",
    ),
    "onvif_generic": BrandTemplate(
        key="onvif_generic",
        label="ONVIF generic (custom path)",
        main_path="/onvif1",
        sub_path="/onvif2",
        notes_mn="Хямд OEM камер ихэвчлэн энэ pattern-той. P2P-only камер (Tuya/iCSee) дэмжигдэхгүй.",
    ),
}


def list_brands() -> list[BrandTemplate]:
    return list(BRAND_TEMPLATES.values())


def get_brand(key: str) -> BrandTemplate | None:
    return BRAND_TEMPLATES.get(key)


def build_rtsp_url(
    template: BrandTemplate,
    host: str,
    username: str,
    password: str,
    *,
    use_sub: bool = False,
    port: int | None = None,
    custom_path: str | None = None,
) -> str:
    """Assemble a full `rtsp://user:pass@host:port/path` URL.

    Username and password are URL-encoded so special chars like `*` `@` `#`
    don't break the URL parsing on the camera side.
    """
    user_enc = urllib.parse.quote(username, safe="")
    pass_enc = urllib.parse.quote(password, safe="")
    actual_port = port or template.default_port
    if custom_path is not None:
        path = custom_path if custom_path.startswith("/") else "/" + custom_path
    elif use_sub and template.sub_path:
        path = template.sub_path
    else:
        path = template.main_path
    return f"rtsp://{user_enc}:{pass_enc}@{host}:{actual_port}{path}"


def candidate_urls(
    template: BrandTemplate,
    host: str,
    username: str,
    password: str,
    *,
    port: int | None = None,
) -> list[str]:
    """All URLs to try for this brand (main first, then sub if defined).

    Probe in order; pick the first H.264 result.
    """
    urls = [build_rtsp_url(template, host, username, password, port=port)]
    if template.sub_path:
        urls.append(
            build_rtsp_url(template, host, username, password, use_sub=True, port=port),
        )
    return urls
