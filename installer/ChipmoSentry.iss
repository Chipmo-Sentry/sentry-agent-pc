; Inno Setup script for Sentry agent.
; Build:  iscc /DAppVersion=0.3.4 installer\ChipmoSentry.iss
; (CI passes the version from the git tag; defaults below for local builds.)
;
; Produces dist\ChipmoSentryAgent-Setup.exe — an installer that:
;   • lets the user choose the install folder
;   • installs ChipmoSentryAgent.exe + icon there
;   • creates Start Menu (+ optional Desktop) shortcuts
;   • optionally registers auto-start at login (HKCU Run, --minimized)
;   • offers to launch the app after install
;   • registers a clean uninstaller

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Sentry"
#define AppPublisher "Chipmo"
#define AppExeName "ChipmoSentryAgent.exe"
#define AppId "{{B5F4B6A2-7C3E-4E2A-9D1F-CHIPMOSENTRY01}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Sentry
DefaultGroupName=Sentry
DisableProgramGroupPage=yes
; Keep the directory-selection page so the user picks where to install.
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename=ChipmoSentryAgent-Setup
SetupIconFile=..\src\sentry_agent_pc\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install — no admin prompt required.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop дээр товчлол үүсгэх"; GroupDescription: "Нэмэлт товчлол:"
Name: "autostart"; Description: "Компьютер асахад автоматаар эхлүүлэх (tray-д)"; GroupDescription: "Автостарт:"

[Files]
; PyInstaller --onedir output: the exe + _internal\* tree.
Source: "..\dist\ChipmoSentryAgent\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\src\sentry_agent_pc\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Sentry"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Sentry-г устгах"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Sentry"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
; Auto-start at login — same HKCU Run value the in-app toggle manages, so the
; two never conflict. Launches into the tray with --minimized.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "ChipmoSentry"; \
  ValueData: """{app}\{#AppExeName}"" --minimized"; \
  Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Sentry-г одоо ажиллуулах"; \
  Flags: nowait postinstall skipifsilent
