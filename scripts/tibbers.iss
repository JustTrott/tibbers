; Inno Setup script for the tibbers Windows installer.
;
; Wraps the PyInstaller onedir (dist\Tibbers) into a per-user setup.exe:
;   * installs into %LOCALAPPDATA%\Programs\Tibbers -- no admin, no UAC, which
;     suits an injector the user runs against their own game;
;   * a Start Menu entry, an optional desktop icon, and an optional run-at-login
;     (tibbers lives in the tray watching champ select);
;   * is also the update: the running app downloads this same file from the
;     release, runs it /VERYSILENT with /RELAUNCH=1, and quits. PrepareToInstall
;     below waits for the app's instance mutex to go before a file is touched,
;     and [Run] reopens the app --quiet when /RELAUNCH=1 was passed.
;
; Build:  ISCC.exe scripts\tibbers.iss   (or build_windows.ps1 -Installer)
; The version is passed in by build_windows.ps1; it defaults for a hand run.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.1"
#endif

; These three are overridable too, so a test compile can install a stand-in
; under another name and AppId without touching the real install or its
; uninstall registration (tests/test_installer_e2e.ps1).
#ifndef MyAppName
  #define MyAppName "Tibbers"
#endif
#ifndef MyAppId
  #define MyAppId "{9C6C0B7E-4E2E-4E2B-9E2A-7C1BBE175000}"
#endif
#ifndef MySourceDir
  #define MySourceDir "..\dist\Tibbers"
#endif
#ifndef MyOutputBase
  #define MyOutputBase "Tibbers-windows-setup"
#endif
#define MyAppPublisher "tibbers"
#define MyAppExeName "Tibbers.exe"
#define MyAppURL "https://github.com/JustTrott/tibbers"

[Setup]
AppId={{#MyAppId}
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
; A fixed name (no version), so releases/latest/download/Tibbers-windows-setup
; .exe is a stable link the README download button can point at -- the same way
; the macOS Tibbers.zip is version-independent. The version lives in AppVersion.
OutputBaseFilename={#MyOutputBase}
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
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

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
; A self-update (silent, /RELAUNCH=1) reopens the app in the tray without
; taking the foreground -- the user may be doing something else by now.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--quiet"; Flags: nowait; Check: WantsRelaunch

[Code]
// The running app holds this mutex (tibbers/_system_windows.py INSTANCE_MUTEX)
// and releases it by exiting. A self-update starts this installer and then
// quits, so the app is usually still alive when we get here: wait for it,
// rather than have CloseApplications kill it mid-shutdown or a copy fail on
// a file it still holds. A minute is far longer than the app takes to quit.
const
  AppMutex = 'TibbersRunning';
  WaitForAppMs = 60000;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  waited: Integer;
begin
  Result := '';
  waited := 0;
  // Only a silent run (the self-update) has an app on its way out to wait
  // for; someone running Setup by hand is told at once.
  while WizardSilent and CheckForMutexes(AppMutex) and (waited < WaitForAppMs) do
  begin
    Sleep(250);
    waited := waited + 250;
  end;
  if CheckForMutexes(AppMutex) then
    Result := 'Tibbers is still running. Quit it from the tray icon and run Setup again.';
end;

function WantsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

[UninstallDelete]
; The user's library and preferences live in the data dir, not here, so they
; survive an uninstall on purpose. Only the fetched tools, were they ever put
; beside the app, are ours to remove.
Type: filesandordirs; Name: "{app}\tools"
