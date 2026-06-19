# Build sentry-agent-pc into a single Windows .exe via PyInstaller.
#
# Usage (from repo root):
#   .\scripts\build_exe.ps1
#
# Output: dist\ChipmoSentryAgent.exe (windowed, no console).
#
# Notes:
# - customtkinter ships data files (themes/assets) that PyInstaller can't
#   auto-detect — we add them via --add-data using the installed package path.
# - --windowed suppresses the console window (GUI app).
# - onvif-zeep + zeep need their WSDL data files collected too.

$ErrorActionPreference = "Stop"

Write-Host "==> Installing build deps (pyinstaller)..." -ForegroundColor Cyan
uv pip install pyinstaller

# --- Bundle MediaMTX (local camera fan-out hub) -----------------------------
# The agent runs a loopback MediaMTX so each camera is pulled ONCE and shared by
# the cloud push relay + the offline grid (cheap cameras cap concurrent RTSP).
# Fetch the pinned Windows binary into src\...\bin\ so PyInstaller bundles it.
# Non-fatal: if the download fails we WARN and ship without it — at runtime the
# agent then falls back to direct camera connections (no fan-out, no crash).
# Pinned to match the proven sentry-ingest binary — its config schema
# (sourceProtocol/sourceOnDemand) is what local_mediamtx.py generates.
$mtxVersion = "v1.18.2"
$mtxBinDir = "src\sentry_agent_pc\bin"
$mtxExe = Join-Path $mtxBinDir "mediamtx.exe"
if (-not (Test-Path $mtxExe)) {
    try {
        New-Item -ItemType Directory -Force -Path $mtxBinDir | Out-Null
        $mtxZip = Join-Path $env:TEMP "mediamtx_win.zip"
        $mtxUrl = "https://github.com/bluenviron/mediamtx/releases/download/$mtxVersion/mediamtx_${mtxVersion}_windows_amd64.zip"
        Write-Host "==> Downloading MediaMTX $mtxVersion ..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri $mtxUrl -OutFile $mtxZip -UseBasicParsing
        $mtxTmp = Join-Path $env:TEMP "mediamtx_extract"
        Remove-Item -Recurse -Force $mtxTmp -ErrorAction SilentlyContinue
        Expand-Archive -Path $mtxZip -DestinationPath $mtxTmp -Force
        Copy-Item (Join-Path $mtxTmp "mediamtx.exe") $mtxExe -Force
        Write-Host "==> MediaMTX bundled at $mtxExe" -ForegroundColor Green
    } catch {
        Write-Warning "MediaMTX download failed — building WITHOUT local fan-out: $_"
    }
} else {
    Write-Host "==> MediaMTX already present at $mtxExe" -ForegroundColor Cyan
}

# --- Bundle ffmpeg (REQUIRED) -----------------------------------------------
# The RTSP probe AND the cloud push relay both spawn ffmpeg, so the agent can do
# NOTHING without it. Unlike MediaMTX (optional fan-out, non-fatal) a missing
# ffmpeg must FAIL the build — shipping a release without it silently breaks
# every non-ONVIF camera + all streaming on a clean store PC. Dropped into the
# same bin\ dir that PyInstaller bundles via --add-data below.
# $ErrorActionPreference=Stop above makes the download/extract failures fatal.
$ffExe = Join-Path $mtxBinDir "ffmpeg.exe"
if (-not (Test-Path $ffExe)) {
    New-Item -ItemType Directory -Force -Path $mtxBinDir | Out-Null
    $ffZip = Join-Path $env:TEMP "ffmpeg_win.zip"
    # gyan.dev = the Windows build linked from ffmpeg.org; "essentials" includes
    # libx264 (needed by the pusher's transcode path).
    # PINNED to a versioned package (NOT the rolling ffmpeg-release-essentials.zip)
    # so builds are reproducible — every release ships the exact same ffmpeg.
    # To bump: pick a newer filename from https://www.gyan.dev/ffmpeg/builds/
    # (the "packages/ffmpeg-<ver>-essentials_build.zip" link) and update below.
    $ffUrl = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.0.1-essentials_build.zip"
    Write-Host "==> Downloading ffmpeg (essentials) ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $ffUrl -OutFile $ffZip -UseBasicParsing
    $ffTmp = Join-Path $env:TEMP "ffmpeg_extract"
    Remove-Item -Recurse -Force $ffTmp -ErrorAction SilentlyContinue
    Expand-Archive -Path $ffZip -DestinationPath $ffTmp -Force
    # Folder name carries the version (e.g. ffmpeg-7.1-essentials_build\bin) —
    # locate the exe rather than hard-coding the path.
    $ffSrc = Get-ChildItem -Path $ffTmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $ffSrc) {
        Write-Error "ffmpeg.exe not found in the downloaded archive — refusing to ship a build that cannot stream."
    }
    Copy-Item $ffSrc.FullName $ffExe -Force
    Write-Host "==> ffmpeg bundled at $ffExe" -ForegroundColor Green
} else {
    Write-Host "==> ffmpeg already present at $ffExe" -ForegroundColor Cyan
}

# --- Bundle the edge-AI YOLO models (OpenVINO IR) ---------------------------
# The shipped edge Stage-1 detector (edge/ov_lean.py) loads these with the
# bundled OpenVINO runtime — so the SINGLE installer is self-contained: a clean
# store PC just runs the .exe, no pip / no model export by the user. Export is
# done HERE at build time (needs ultralytics+openvino on the BUILD machine only;
# the product never ships ultralytics). Non-fatal: if export fails we WARN and
# ship without edge AI — the scan/push agent still works; the detector raises a
# clear error and edge AI stays off until a build includes the models.
$poseModel = Join-Path $mtxBinDir "yolo11n-pose_openvino_model"
$itemModel = Join-Path $mtxBinDir "yolo11n_openvino_model"
if (-not (Test-Path (Join-Path $poseModel "yolo11n-pose_openvino_model.xml"))) {
    try {
        Write-Host "==> Exporting YOLO11 models to OpenVINO IR ..." -ForegroundColor Cyan
        uv pip install "ultralytics>=8.4,<9.0" "openvino>=2024.0"
        uv run yolo export model=yolo11n-pose.pt format=openvino imgsz=640
        uv run yolo export model=yolo11n.pt      format=openvino imgsz=640
        Move-Item -Force "yolo11n-pose_openvino_model" $poseModel
        Move-Item -Force "yolo11n_openvino_model" $itemModel
        Write-Host "==> Edge models bundled under $mtxBinDir" -ForegroundColor Green
    } catch {
        Write-Warning "YOLO OpenVINO export failed — building WITHOUT edge AI: $_"
    }
} else {
    Write-Host "==> Edge models already present at $poseModel" -ForegroundColor Cyan
}

# Resolve customtkinter package dir (contains assets/ themes the GUI loads)
$ctkPath = uv run python -c "import customtkinter, os; print(os.path.dirname(customtkinter.__file__))"
Write-Host "==> customtkinter at: $ctkPath" -ForegroundColor Cyan

# Resolve onvif wsdl dir — onvif-zeep installs it at site-packages/wsdl
# (NOT inside the onvif package). Default wsdl_dir = dirname(dirname(onvif))/wsdl.
$onvifPath = uv run python -c "import onvif, os; print(os.path.join(os.path.dirname(os.path.dirname(onvif.__file__)), 'wsdl'))"
Write-Host "==> onvif wsdl at: $onvifPath" -ForegroundColor Cyan

Write-Host "==> Running PyInstaller..." -ForegroundColor Cyan
# wsdl bundled to ./wsdl inside the bundle; onvif resolves it via our runtime
# patch in settings.py (sets wsdl_dir for the frozen case).
# App icon bundled for the tray + window + .exe resource.
$iconPath = "src\sentry_agent_pc\assets\icon.ico"

# Ensure the bin\ dir exists even if the MediaMTX download was skipped/failed,
# so the --add-data below never errors (it just bundles an empty dir → runtime
# falls back to direct camera connections).
New-Item -ItemType Directory -Force -Path $mtxBinDir | Out-Null

# --onedir (NOT --onefile): a one-file exe unpacks python311.dll + deps to a
# temp _MEI dir at every launch; when the app self-updates (replaces its own
# exe) that extraction races and fails with "Failed to load Python DLL". A
# onedir build ships the DLLs alongside the exe (no runtime extraction), so
# self-update is reliable and startup is faster. Output: dist\ChipmoSentryAgent\.
uv run pyinstaller `
    --name ChipmoSentryAgent `
    --onedir `
    --windowed `
    --noconfirm `
    --clean `
    --icon "$iconPath" `
    --add-data "$ctkPath;customtkinter" `
    --add-data "$onvifPath;wsdl" `
    --add-data "src\sentry_agent_pc\assets;assets" `
    --add-data "src\sentry_agent_pc\bin;bin" `
    --collect-submodules customtkinter `
    --collect-submodules pystray `
    --collect-all webview `
    --collect-all clr_loader `
    --collect-all cv2 `
    --collect-all openvino `
    --hidden-import PIL._tkinter_finder `
    --hidden-import pystray._win32 `
    src\sentry_agent_pc\gui_main.py

Write-Host ""
Write-Host "==> Done. Output: dist\ChipmoSentryAgent\ChipmoSentryAgent.exe (onedir folder)" -ForegroundColor Green
Write-Host "    Distribute via the installer (Setup.exe) or the onedir zip." -ForegroundColor Green
