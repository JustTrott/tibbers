; Inno Setup script for the tibbers Windows installer.
;
; Wraps the PyInstaller onedir (dist\Tibbers) into a per-user setup.exe:
;   * installs into %LOCALAPPDATA%\Programs\Tibbers -- no admin, no UAC, which
;     suits an injector the user runs against their own game;
;   * a Start Menu entry, an optional desktop icon, and an optional run-at-login
;     (tibbers lives in the tray watching champ select);
;   * fetches the injection tools right after install (Tibbers.exe --fetch-tools)
;     so the first real launch is ready, with the app self-fetching as a fallback.
;
; Build:  ISCC.exe scripts\tibbers.iss   (or build_windows.ps1 -Installer)
; The version is passed in by build_windows.ps1; it defaults for a hand run.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.1"
#endif

#define MyAppName "Tibbers"
#define MyAppPublisher "tibbers"
#define MyAppExeName "Tibbers.exe"
#define MyAppURL "https://github.com/JustTrott/tibbers"

[Setup]
AppId={{9C6C0B7E-4E2E-4E2B-9E2A-7C1BBE175000}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install: no administrator rights required.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Tibbers-{#MyAppVersion}-setup
SetupIconFile=..\assets\tibbers.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "startup"; Description: "Start tibbers when I sign in (runs in the tray)"; GroupDescription: "Startup:"

[Files]
; The whole PyInstaller onedir. tools\ is deliberately absent -- fetched below.
Source: "..\dist\Tibbers\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; Run-at-login: a quiet launch that comes up in the tray without stealing focus.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--quiet"; Tasks: startup

[Run]
; The patcher binaries are fetched by the app itself on first launch, with a
; progress bar in the window -- not here. Doing it in the installer left the
; wizard's progress bar sitting at 100% during a ~50 MB download with no
; feedback, which read as a hang.
; Offer to launch after a normal (non-silent) install.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The user's library and preferences live in the data dir, not here, so they
; survive an uninstall on purpose. Only the fetched tools, were they ever put
; beside the app, are ours to remove.
Type: filesandordirs; Name: "{app}\tools"
