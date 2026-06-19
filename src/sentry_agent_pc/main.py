"""sentry-agent-pc CLI entry point.

Run `sentry-agent-pc --help` to see commands. Most-used:
  discover       — ONVIF scan + probe + interactive select + backend register
  add-manual     — manual brand-template path
  list           — show registered cameras
  status         — connectivity + last probe summary
"""

from __future__ import annotations

import getpass
import re
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sentry_agent_pc.backend_client import BackendClient, BackendError, CameraRegistration
from sentry_agent_pc.discovery import manual as manual_mod
from sentry_agent_pc.discovery import onvif as onvif_mod
from sentry_agent_pc.discovery import rtsp_probe
from sentry_agent_pc.logging_setup import configure_logging, get_logger
from sentry_agent_pc.settings import get_settings
from sentry_agent_pc.state import CameraRecord, load_state, save_state

app = typer.Typer(
    add_completion=False,
    help="Chipmo Sentry — Windows camera discovery agent",
)
console = Console()
log = get_logger("sentry_agent_pc.main")


def _slugify(text: str) -> str:
    """ASCII-only slug for mediamtx_path. Falls back to 'cam' if all stripped."""
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return s or "cam"


def _fmt_res(width: int | None, height: int | None) -> str:
    """'WxH' when known, else '' — a probe may report a codec without resolution."""
    return f" {width}x{height}" if width and height else ""


def _next_mediamtx_path(prefix: str, existing: set[str]) -> str:
    """Allocate a unique slug like 'cam3_hik' avoiding existing names."""
    i = 1
    while True:
        candidate = f"cam{i}_{prefix}" if prefix else f"cam{i}"
        if candidate not in existing:
            return candidate
        i += 1


def _ensure_token_or_die() -> BackendClient:
    s = get_settings()
    if not s.dev_token:
        console.print(
            "[red]Backend token тохируулагдаагүй.[/red] "
            "`%APPDATA%/Chipmo/sentry-agent/.env`-д "
            "`DEV_TOKEN=<jwt>` оруулна уу.",
        )
        raise typer.Exit(2)
    client = BackendClient()
    try:
        me = client.me()
        log.info("auth.ok", user=me.get("email"))
    except BackendError as e:
        console.print(f"[red]Backend auth амжилтгүй:[/red] {e}")
        raise typer.Exit(2) from e
    return client


def _print_devices(devices: list[onvif_mod.OnvifDevice]) -> None:
    table = Table(title=f"ONVIF discovery — {len(devices)} төхөөрөмж", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("IP")
    table.add_column("XAddr")
    for i, d in enumerate(devices, 1):
        table.add_row(str(i), d.ip, d.xaddr)
    console.print(table)


def _print_profiles(device: onvif_mod.OnvifDevice) -> None:
    if device.error:
        console.print(f"[yellow]⚠ {device.ip}:[/yellow] {device.error}")
        return
    header = f"{device.manufacturer or '?'} {device.model or ''} @ {device.ip}".strip()
    console.print(f"\n[bold]{header}[/bold]")
    for p in device.profiles:
        console.print(
            f"  • token={p.token}  {p.encoding}  {p.width}x{p.height}  uri={p.rtsp_uri}",
        )


@app.command()
def discover(
    timeout: Annotated[float, typer.Option(help="ONVIF probe wait (sec)")] = 5.0,
    auto_select: Annotated[
        bool,
        typer.Option("--auto", help="Skip interactive — register every reachable H.264 camera"),
    ] = False,
) -> None:
    """ONVIF WS-Discovery + per-camera RTSP probe + interactive register."""
    configure_logging()
    client = _ensure_token_or_die()

    console.print("[cyan]Камер хайж байна (ONVIF)...[/cyan]")
    devices = onvif_mod.discover(timeout_sec=timeout)
    if not devices:
        console.print("[yellow]Нэг ч ONVIF төхөөрөмж олдсонгүй.[/yellow]")
        console.print("Гараар нэмэх: `sentry-agent-pc add-manual`")
        return
    _print_devices(devices)

    state = load_state()
    existing_paths = {c.mediamtx_path for c in state.cameras if c.mediamtx_path}
    existing_ips = {c.ip for c in state.cameras}

    stores = client.list_stores()
    if not stores:
        console.print("[red]Backend дээр store байхгүй байна.[/red]")
        raise typer.Exit(1)
    store_id = stores[0]["id"]
    console.print(f"[dim]Default store: {stores[0].get('name')} ({store_id})[/dim]")

    for dev in devices:
        if dev.ip in existing_ips:
            console.print(f"[dim]· {dev.ip} аль хэдийн бүртгэгдсэн — алгаслаа[/dim]")
            continue
        if dev.ip in state.ignored_devices:
            console.print(f"[dim]· {dev.ip} ignore жагсаалтад байна — алгаслаа[/dim]")
            continue

        console.print(f"\n[bold]{dev.ip}[/bold] — фит хэрэглэгчийн нэр/нууц үг:")
        if not auto_select:
            keep = typer.confirm(f"  Бүртгэх үү ({dev.ip})?", default=True)
            if not keep:
                state.ignored_devices.append(dev.ip)
                # Persist the decline NOW — otherwise (if no later camera is
                # registered) save_state never runs and the user is re-prompted
                # for the same device on the next run.
                save_state(state)
                continue

        username = typer.prompt(
            "  Хэрэглэгч",
            default=get_settings().onvif_default_user,
        )
        password = getpass.getpass("  Нууц үг: ")

        dev = onvif_mod.fetch_profiles(dev, username, password)
        _print_profiles(dev)
        if dev.error or not dev.profiles:
            console.print(f"[yellow]ONVIF profile авч чадсангүй: {dev.error}[/yellow]")
            continue

        # Pick first H.264 profile with a URI
        chosen = next(
            (
                p for p in dev.profiles
                if p.rtsp_uri and (p.encoding or "").lower() == "h264"
            ),
            None,
        )
        if chosen is None:
            console.print(
                "[yellow]H.264 profile олдсонгүй. Камерын web UI-аас H.264-руу солих.[/yellow]",
            )
            continue

        # Embed creds into URI (onvif-zeep often returns без auth)
        rtsp_url = _embed_creds(chosen.rtsp_uri, username, password)  # type: ignore[arg-type]
        probe_result = rtsp_probe.probe(rtsp_url)
        if not probe_result.ok:
            console.print(f"[red]RTSP probe амжилтгүй: {probe_result.error}[/red]")
            continue
        console.print(
            f"[green]✅ {probe_result.codec}{_fmt_res(probe_result.width, probe_result.height)}[/green]",
        )

        brand_hint = _slugify(dev.manufacturer or "cam")[:8]
        mediamtx_path = _next_mediamtx_path(brand_hint, existing_paths)
        existing_paths.add(mediamtx_path)

        name = f"{dev.manufacturer or 'Camera'} — {dev.ip}"
        try:
            reg = CameraRegistration(
                store_id=store_id,
                name=name,
                rtsp_url=rtsp_url,
                mediamtx_path=mediamtx_path,
            )
            created = client.register_camera(reg)
            cam_uuid = str(created.get("id"))
            console.print(f"[green]Backend бүртгэгдэв: {mediamtx_path} ({cam_uuid[:8]}...)[/green]")
        except BackendError as e:
            console.print(f"[red]Backend register амжилтгүй: {e}[/red]")
            continue

        state.cameras.append(
            CameraRecord(
                uuid=cam_uuid,
                name=name,
                ip=dev.ip,
                rtsp_url=rtsp_url,
                mediamtx_path=mediamtx_path,
                codec=probe_result.codec,
                resolution=(probe_result.width or 0, probe_result.height or 0),
            ),
        )
        save_state(state)


@app.command(name="add-manual")
def add_manual() -> None:
    """Manually add a camera by brand template (when ONVIF doesn't help)."""
    configure_logging()
    client = _ensure_token_or_die()
    state = load_state()
    existing_paths = {c.mediamtx_path for c in state.cameras if c.mediamtx_path}

    brands = manual_mod.list_brands()
    table = Table(title="Brand templates", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Key")
    table.add_column("Brand")
    table.add_column("Main path")
    for i, b in enumerate(brands, 1):
        table.add_row(str(i), b.key, b.label, b.main_path)
    console.print(table)

    idx = typer.prompt("Brand сонгоно уу (#)", type=int)
    if not (1 <= idx <= len(brands)):
        console.print("[red]Буруу дугаар.[/red]")
        raise typer.Exit(1)
    template = brands[idx - 1]
    console.print(f"[dim]Сонгосон: {template.label}[/dim]")
    if template.notes_mn:
        console.print(f"[yellow]Анхаар:[/yellow] {template.notes_mn}")

    host = typer.prompt("Камерын IP")
    port_str = typer.prompt(
        "Port",
        default=str(template.default_port),
    )
    username = typer.prompt(
        "Хэрэглэгч",
        default=get_settings().onvif_default_user,
    )
    password = getpass.getpass("Нууц үг: ")
    custom_path = typer.prompt(
        "Custom RTSP path (хэрэв шаардлагатай, default-ыг хэрэглэхэд хоосон үлдээ)",
        default="",
    )

    try:
        port = int(port_str)
    except ValueError:
        console.print("[red]Port бүхэл тоо байх ёстой.[/red]")
        raise typer.Exit(1) from None

    if custom_path:
        urls = [
            manual_mod.build_rtsp_url(
                template, host, username, password,
                port=port, custom_path=custom_path,
            ),
        ]
    else:
        urls = manual_mod.candidate_urls(template, host, username, password, port=port)

    console.print(f"[cyan]RTSP probe ({len(urls)} URL)...[/cyan]")
    result = rtsp_probe.probe_first_h264(urls)
    if not result.ok:
        console.print(f"[red]Probe амжилтгүй: {result.error}[/red]")
        raise typer.Exit(1)
    if not result.is_h264:
        console.print(
            f"[yellow]Камер H.{result.codec[1:] if result.codec else '?'} буцаалаа. "
            "Web UI-аас H.264 болгох — browser HLS дэмжихгүй.[/yellow]",
        )
        raise typer.Exit(1)
    console.print(
        f"[green]✅ {result.codec}{_fmt_res(result.width, result.height)} — {result.url}[/green]",
    )

    stores = client.list_stores()
    if not stores:
        console.print("[red]Backend дээр store байхгүй байна.[/red]")
        raise typer.Exit(1)
    store_id = stores[0]["id"]
    mediamtx_path = _next_mediamtx_path(template.key, existing_paths)
    name = f"{template.label} — {host}"
    try:
        created = client.register_camera(
            CameraRegistration(
                store_id=store_id,
                name=name,
                rtsp_url=result.url,
                mediamtx_path=mediamtx_path,
            ),
        )
    except BackendError as e:
        console.print(f"[red]Backend register амжилтгүй: {e}[/red]")
        raise typer.Exit(1) from e

    cam_uuid = str(created.get("id"))
    console.print(f"[green]Бүртгэгдэв: {mediamtx_path} ({cam_uuid[:8]}...)[/green]")

    state.cameras.append(
        CameraRecord(
            uuid=cam_uuid,
            name=name,
            ip=host,
            rtsp_url=result.url,
            mediamtx_path=mediamtx_path,
            codec=result.codec,
            resolution=(result.width or 0, result.height or 0),
        ),
    )
    save_state(state)


@app.command(name="list")
def list_cameras() -> None:
    """Show locally-known cameras."""
    state = load_state()
    if not state.cameras:
        console.print("[dim]Бүртгэгдсэн камер байхгүй. `discover` эсвэл `add-manual` ажиллуул.[/dim]")
        return
    table = Table(title=f"Камерууд ({len(state.cameras)})")
    table.add_column("#")
    table.add_column("Name")
    table.add_column("IP")
    table.add_column("Path")
    table.add_column("Codec")
    table.add_column("Resolution")
    for i, c in enumerate(state.cameras, 1):
        res = f"{c.resolution[0]}x{c.resolution[1]}" if c.resolution else "?"
        table.add_row(
            str(i),
            c.name,
            c.ip,
            c.mediamtx_path or "—",
            c.codec or "?",
            res,
        )
    console.print(table)


@app.command()
def gui() -> None:
    """Launch the desktop window (Scan / Add / camera list)."""
    configure_logging()
    from sentry_agent_pc.gui.app import run

    run()


@app.command()
def status() -> None:
    """Backend connectivity + camera count."""
    configure_logging()
    s = get_settings()
    state = load_state()
    console.print(f"Backend URL: [bold]{s.backend_url}[/bold]")
    console.print(f"Token configured: {'✅' if s.dev_token else '❌'}")
    console.print(f"State file: {s.state_path}")
    console.print(f"Cameras locally: {len(state.cameras)}")
    if s.dev_token:
        try:
            client = BackendClient()
            me = client.me()
            console.print(f"[green]Backend OK[/green] — logged in as {me.get('email')}")
            backend_cams = client.list_cameras()
            console.print(f"Cameras in backend: {len(backend_cams)}")
        except BackendError as e:
            console.print(f"[red]Backend error:[/red] {e}")


def _embed_creds(url: str, username: str, password: str) -> str:
    """Insert `user:pass@` into a `rtsp://host/...` URL if not already present."""
    import urllib.parse

    m = re.match(r"(rtsp://)(?:[^/@]+@)?(.*)", url)
    if not m:
        return url
    scheme = m.group(1)
    rest = m.group(2)
    user_enc = urllib.parse.quote(username, safe="")
    pass_enc = urllib.parse.quote(password, safe="")
    return f"{scheme}{user_enc}:{pass_enc}@{rest}"


if __name__ == "__main__":
    sys.exit(app())
