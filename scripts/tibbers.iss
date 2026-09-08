; Inno Setup script for the tibbers Windows installer.
;
; Wraps the PyInstaller onedir (dist\Tibbers) into a per-user setup.exe:
;   * installs into %LOCALAPPDATA%\Programs\Tibbers -- no admin, no UAC, which
;     suits an injector the user runs against their own game;
;   * a Start Menu entry, an optional desktop shortcut, and an optional
;     run-at-sign-in (tibbers lives in the tray watching champ select);
;   * downloads the injection tools and the build-data fetcher as part of
;     Setup, on the wizard's own download page, and unpacks them into the
;     app's data directory -- so the first launch is ready to apply a skin;
;   * speaks English and Russian, chosen from the Windows display language
;     and changeable on Setup's first page;
;   * is also the update: the running app downloads this same file from the
;     release, runs it /VERYSILENT with /RELAUNCH=1, and quits. PrepareToInstall
;     below waits for the app's instance mutex to go before a file is touched,
;     and [Run] reopens the app --quiet when /RELAUNCH=1 was passed.
;
; Build:  scripts\build_windows.ps1 -Installer
; The version and the three download URLs are passed in by the build script
; (it resolves the newest release of each tool); a hand run of ISCC gets the
; defaults below and builds an installer that downloads nothing.

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
; Where the app keeps its data; the tools go under it. Overridable for the
; stand-in test, which must not write into the real data directory.
#ifndef MyDataDir
  #define MyDataDir "{localappdata}\tibbers"
#endif

; The tools Setup downloads. Empty URL = that tool is not fetched by Setup
; (the app fetches what is missing on first launch, so nothing breaks).
#ifndef LtkUrl
  #define LtkUrl ""
#endif
#ifndef LtkSize
  #define LtkSize 20000000
#endif
#ifndef CslolUrl
  #define CslolUrl ""
#endif
#ifndef CslolSize
  #define CslolSize 38000000
#endif
#ifndef CurlUrl
  #define CurlUrl ""
#endif
#ifndef CurlSize
  #define CurlSize 2000000
#endif

; The instance mutex the running app holds (tibbers/_system_windows.py
; INSTANCE_MUTEX). Overridable so a stand-in install can be exercised while
; the real app is running.
#ifndef MyAppMutex
  #define MyAppMutex "TibbersRunning"
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
; The language dialog opens with the Windows display language selected, so
; the default is the system's and the choice is still the user's.
ShowLanguageDialog=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
; Everything Setup says in its own words. The task and launch lines use Inno's
; stock messages ({cm:CreateDesktopIcon}, {cm:AutoStartProgram} ...) so they
; read the way every other installer on the machine does, in both languages.
english.StillRunning=%1 is still running.%n%nQuit it from the tray icon, then click Retry.
english.StillRunningGiveUp=%1 is still running. Quit it from the tray icon and run Setup again.
english.ToolsGroup=Game tools:
english.ToolsTask=Download the injection tools and the build-data fetcher (about %1 MB)
english.ToolsUnpacking=Unpacking the game tools...
english.ToolsFailed=The game tools could not be unpacked (%1).%n%n%2 will download them itself the first time it runs.
russian.StillRunning=%1 всё ещё запущен.%n%nЗакройте его через значок в трее и нажмите «Повторить».
russian.StillRunningGiveUp=%1 всё ещё запущен. Закройте его через значок в трее и запустите установку заново.
russian.ToolsGroup=Игровые инструменты:
russian.ToolsTask=Скачать инструменты внедрения и загрузчик сборок (около %1 МБ)
russian.ToolsUnpacking=Распаковка игровых инструментов...
russian.ToolsFailed=Не удалось распаковать игровые инструменты (%1).%n%n%2 скачает их самостоятельно при первом запуске.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "{cm:AutoStartProgram,{#MyAppName}}"; GroupDescription: "{cm:AutoStartProgramGroupDescription}"
; Offered only when there is something to download and the tools are not
; already in place (an update over a working install skips the whole thing).
#define ToolsMB (Int(LtkSize) + Int(CslolSize) + Int(CurlSize)) / 1048576
Name: "tools"; Description: "{cm:ToolsTask,{#ToolsMB}}"; GroupDescription: "{cm:ToolsGroup}"; Check: ToolsOffered

[Files]
; The whole PyInstaller onedir. tools\ is deliberately absent: the tools live
; in the data directory, fetched below or by the app.
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; The tools, downloaded on Setup's own download page with its progress bar.
; Landed in {tmp} and unpacked in CurStepChanged(ssPostInstall) below: an MSI
; (administrative install, no service, no registry), a 7-Zip console
; self-extractor, and a tarball Windows' own tar.exe opens.
#if LtkUrl != ""
Source: "{#LtkUrl}"; DestDir: "{tmp}"; DestName: "ltk.msi"; ExternalSize: {#LtkSize}; Flags: external download ignoreversion deleteafterinstall; Tasks: tools
#endif
#if CslolUrl != ""
Source: "{#CslolUrl}"; DestDir: "{tmp}"; DestName: "cslol.exe"; ExternalSize: {#CslolSize}; Flags: external download ignoreversion deleteafterinstall; Tasks: tools
#endif
#if CurlUrl != ""
Source: "{#CurlUrl}"; DestDir: "{tmp}"; DestName: "curl.tar.gz"; ExternalSize: {#CurlSize}; Flags: external download ignoreversion deleteafterinstall; Tasks: tools
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; Run at sign-in: a quiet launch that comes up in the tray without stealing focus.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--quiet"; Tasks: startup

[Run]
; Offer to launch after a normal (non-silent) install.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; A self-update (silent, /RELAUNCH=1) reopens the app in the tray without
; taking the foreground -- the user may be doing something else by now.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--quiet"; Flags: nowait; Check: WantsRelaunch

[UninstallDelete]
; The user's library and preferences live in the data dir, not here, so they
; survive an uninstall on purpose. Only the fetched tools, were they ever put
; beside the app, are ours to remove.
Type: filesandordirs; Name: "{app}\tools"

[Code]
// The running app holds this mutex (tibbers/_system_windows.py INSTANCE_MUTEX)
// and releases it by exiting. A self-update starts this installer and then
// quits, so the app is usually still alive when we get here: wait for it,
// rather than have CloseApplications kill it mid-shutdown or a copy fail on
// a file it still holds. A minute is far longer than the app takes to quit.
const
  AppMutex = '{#MyAppMutex}';
  WaitForAppMs = 60000;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  waited: Integer;
begin
  Result := '';
  waited := 0;
  // A silent run (the self-update) has an app on its way out: wait for it.
  while WizardSilent and CheckForMutexes(AppMutex) and (waited < WaitForAppMs) do
  begin
    Sleep(250);
    waited := waited + 250;
  end;
  // Someone running Setup by hand is asked to quit the app and can try
  // again from here. The old message sent them back to run Setup over:
  // this page checks once, so quitting the app after it appeared did not
  // let the wizard continue.
  while (not WizardSilent) and CheckForMutexes(AppMutex) do
  begin
    if MsgBox(FmtMessage(CustomMessage('StillRunning'), ['{#MyAppName}']),
              mbError, MB_RETRYCANCEL) <> IDRETRY then
      break;
    // The mutex is released as the process exits, a moment after the tray
    // icon goes; give it that moment rather than fail a click that was
    // right.
    Sleep(500);
  end;
  if CheckForMutexes(AppMutex) then
    Result := FmtMessage(CustomMessage('StillRunningGiveUp'), ['{#MyAppName}']);
end;

function WantsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

// -- the game tools ---------------------------------------------------------
//
// Five files, in the data directory's tools\ (tibbers/wintools.py names the
// same five): cslol's overlay builder pair, LTK's patcher pair, and the
// browser-handshake curl the build pages read u.gg through.

function ToolsDir: String;
begin
  Result := ExpandConstant('{#MyDataDir}\tools');
end;

function HaveTools: Boolean;
begin
  Result := FileExists(ToolsDir + '\mod-tools.exe')
        and FileExists(ToolsDir + '\cslol-dll.dll')
        and FileExists(ToolsDir + '\ltk_patcher_host.exe')
        and FileExists(ToolsDir + '\ltk_patcher_dll.dll')
        and FileExists(ToolsDir + '\curl-impersonate.exe');
end;

function ToolsOffered: Boolean;
begin
  Result := ('{#LtkUrl}' <> '') and not HaveTools;
end;

// The one file we want out of an unpacked tree, wherever the packer put it.
function FindFile(Dir, Name: String): String;
var
  rec: TFindRec;
  sub: String;
begin
  Result := '';
  if FileExists(Dir + '\' + Name) then
  begin
    Result := Dir + '\' + Name;
    exit;
  end;
  if FindFirst(Dir + '\*', rec) then
  begin
    try
      repeat
        if (rec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0)
           and (rec.Name <> '.') and (rec.Name <> '..') then
        begin
          sub := FindFile(Dir + '\' + rec.Name, Name);
          if sub <> '' then
          begin
            Result := sub;
            exit;
          end;
        end;
      until not FindNext(rec);
    finally
      FindClose(rec);
    end;
  end;
end;

// Copy a file found by name into the tools directory; the pairs must land
// together, so a missing one is an error rather than a partial install.
procedure Take(FromDir, Name: String);
var
  src: String;
begin
  src := FindFile(FromDir, Name);
  if src = '' then
    RaiseException(Name + ' not found after unpacking');
  if not FileCopy(src, ToolsDir + '\' + Name, False) then
    RaiseException('could not copy ' + Name);
end;

procedure RunHidden(Exe, Params: String);
var
  code: Integer;
begin
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, code) then
    RaiseException(ExtractFileName(Exe) + ' could not be started');
  if code <> 0 then
    RaiseException(ExtractFileName(Exe) + ' exited with code ' + IntToStr(code));
end;

procedure UnpackTools;
var
  tmp: String;
begin
  tmp := ExpandConstant('{tmp}');
  ForceDirectories(ToolsDir);

  // LTK's patcher: an administrative install unpacks the MSI's payload
  // with no install -- no service, no registry, no Vanguard interaction.
  if FileExists(tmp + '\ltk.msi') then
  begin
    RunHidden(ExpandConstant('{sys}\msiexec.exe'),
              '/a "' + tmp + '\ltk.msi" /qn TARGETDIR="' + tmp + '\ltk"');
    Take(tmp + '\ltk', 'ltk_patcher_host.exe');
    Take(tmp + '\ltk', 'ltk_patcher_dll.dll');
  end;

  // cslol's overlay builder: the Windows release is a 7-Zip console
  // self-extractor; -o/-y unpack it without a window.
  if FileExists(tmp + '\cslol.exe') then
  begin
    RunHidden(tmp + '\cslol.exe', '-o"' + tmp + '\cslol" -y');
    Take(tmp + '\cslol', 'mod-tools.exe');
    Take(tmp + '\cslol', 'cslol-dll.dll');
  end;

  // The browser-handshake curl: a tarball, opened by the tar.exe every
  // Windows since 10 ships in System32.
  if FileExists(tmp + '\curl.tar.gz') then
  begin
    ForceDirectories(tmp + '\curl');
    RunHidden(ExpandConstant('{sys}\tar.exe'),
              '-xzf "' + tmp + '\curl.tar.gz" -C "' + tmp + '\curl"');
    Take(tmp + '\curl', 'curl-impersonate.exe');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('tools') then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('ToolsUnpacking');
    WizardForm.ProgressGauge.Style := npbstMarquee;
    try
      try
        UnpackTools;
      except
        // Not fatal: the app fetches whatever is missing on first launch,
        // with its own progress bar. Said once, and never in a silent run.
        // (Kept on one line: a line beginning with "[" reads as a section
        // tag to the script parser, even inside [Code].)
        if not WizardSilent then
          MsgBox(FmtMessage(CustomMessage('ToolsFailed'), [GetExceptionMessage, '{#MyAppName}']), mbInformation, MB_OK);
        Log('tools: ' + GetExceptionMessage);
      end;
    finally
      WizardForm.ProgressGauge.Style := npbstNormal;
    end;
  end;
end;
