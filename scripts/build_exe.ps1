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
uv run pyinstaller `
    --name ChipmoSentryAgent `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    --add-data "$ctkPath;customtkinter" `
    --add-data "$onvifPath;wsdl" `
    --collect-submodules customtkinter `
    --collect-all webview `
    --collect-all clr_loader `
    --hidden-import PIL._tkinter_finder `
    src\sentry_agent_pc\gui_main.py

Write-Host ""
Write-Host "==> Done. Output: dist\ChipmoSentryAgent.exe" -ForegroundColor Green
Write-Host "    Double-click to launch, or distribute via GitHub Releases (M2)." -ForegroundColor Green
