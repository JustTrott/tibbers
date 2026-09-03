# End-to-end check of the Windows self-update path, without touching the real
# install. Run by hand on a Windows machine with Inno Setup:
#
#   pwsh -File tests\test_installer_e2e.ps1
#
# It compiles scripts\tibbers.iss under another name and AppId ("TibbersTest")
# from a stand-in payload -- the current dist\Tibbers with Tibbers.exe replaced
# by sort.exe, so nothing it launches is a real tibbers -- then:
#
#   1. holds the instance mutex from a helper process, as a running app would;
#   2. starts the installer exactly as tibbers/update.py does (installer_command);
#   3. checks it *waited* for the mutex, installed only after it was released,
#      reopened the app --quiet because /RELAUNCH=1 was passed, and opened no
#      console window doing any of it;
#   4. uninstalls TibbersTest again.
#
# The real Tibbers install, its registry entries and its data directory are not
# involved: a different AppId means a different uninstall key and folder.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$work = Join-Path $env:TEMP "tibbers-installer-e2e"
$appName = "TibbersTest"
$appId = "{7A1E4C22-5D0B-4F8E-9C3A-0E1F2A3B4C5D}"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\$appName"

function Fail($msg) { Write-Host "FAIL: $msg" -ForegroundColor Red; exit 1 }
function Ok($msg) { Write-Host "  ok: $msg" }

# Inno's compiler, wherever winget or the installer put it.
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
              "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { Fail "ISCC.exe not found" }
if (-not (Test-Path "$root\dist\Tibbers\Tibbers.exe")) { Fail "build dist\Tibbers first (scripts\build_windows.ps1)" }

# --- the stand-in payload -----------------------------------------------------
Write-Host "==> Payload"
if (Test-Path $work) { [System.IO.Directory]::Delete($work, $true) }
New-Item -ItemType Directory -Force "$work\payload" | Out-Null
Copy-Item -Recurse "$root\dist\Tibbers\*" "$work\payload"
Copy-Item "$env:SystemRoot\System32\sort.exe" "$work\payload\Tibbers.exe" -Force
if (Test-Path $installDir) { [System.IO.Directory]::Delete($installDir, $true) }

Write-Host "==> Compiling the test installer"
& $iscc /Q "/DMyAppVersion=9.9.9" "/DMyAppName=$appName" "/DMyAppId=$appId" `
    "/DMySourceDir=$work\payload" "/DMyOutputBase=$appName-setup" "/O$work" "$root\scripts\tibbers.iss"
if ($LASTEXITCODE -ne 0) { Fail "ISCC failed" }
$setup = Join-Path $work "$appName-setup.exe"
Ok "compiled $setup"

# --- a stand-in for the running app: holds the mutex for a while ---------------
$holdSeconds = 8
@"
import sys, time
sys.path.insert(0, r'$root')
from tibbers import _system_windows as w
assert w.claim_instance(), 'mutex already held'
time.sleep($holdSeconds)
"@ | Set-Content "$work\holder.py"
$holder = Start-Process -FilePath $py -PassThru -WindowStyle Hidden -ArgumentList "`"$work\holder.py`""
Start-Sleep -Milliseconds 800
if ($holder.HasExited) { Fail "the mutex holder did not start (exit $($holder.ExitCode))" }
Ok "a stand-in holds the instance mutex for ${holdSeconds}s"

# --- the update, as update.py launches it -------------------------------------
$log = Join-Path $work "update.log"
$t0 = Get-Date
# Launched from Python with the very call update.launch_swap makes, then waited on.
$exit = & $py -c @"
import subprocess, sys
sys.path.insert(0, r'$root')
from pathlib import Path
from tibbers import update
cmd = update.installer_command(Path(r'$setup'), Path(r'$log'))
print('==>', cmd, file=sys.stderr)
p = subprocess.Popen(cmd, close_fds=True, creationflags=0x08000000)
print(p.wait())
"@
$elapsed = ((Get-Date) - $t0).TotalSeconds
$exit = [int]$exit

# --- what happened ------------------------------------------------------------
Write-Host "==> Results"
if ($exit -ne 0) { Get-Content $log | Select-Object -Last 15; Fail "setup exit code $exit" }
Ok "setup exit code 0"
if ($elapsed -lt ($holdSeconds - 1.5)) { Fail "setup finished in ${elapsed}s: it did not wait for the mutex" }
Ok ("setup waited for the mutex (" + [int]$elapsed + "s)")
if (-not (Test-Path "$installDir\Tibbers.exe")) { Fail "nothing installed at $installDir" }
if ((Get-Item "$installDir\Tibbers.exe").Length -ne (Get-Item "$env:SystemRoot\System32\sort.exe").Length) { Fail "the installed exe is not the payload" }
Ok "payload installed to $installDir"
# The stand-in exits at once when handed --quiet, so the relaunch is read from
# the installer's own log: a Run entry for our exe with those parameters.
$logText = Get-Content $log -Raw
if ($logText -notmatch "(?s)-- Run entry --.*?Filename: [^\r\n]*\\$appName\\Tibbers\.exe\s*\r?\n\s*[\d\-: .]*\s*Parameters: --quiet") {
    Get-Content $log | Select-String -Pattern "Run entry" -Context 0,3
    Fail "the app was not reopened --quiet (/RELAUNCH=1)"
}
Ok "reopened --quiet by the installer"
$relaunched = @(Get-CimInstance Win32_Process -Filter "Name = 'Tibbers.exe'" | Where-Object { $_.ExecutablePath -like "$installDir*" })
$terminals = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'OpenConsole.exe','WindowsTerminal.exe' -and $_.CreationDate -gt $t0 })
if ($terminals.Count -gt 0) { Fail "a terminal window was opened" }
Ok "no console window"
if (-not (Select-String -Path $log -Pattern "Installation process succeeded" -Quiet)) { Fail "the log does not say the installation succeeded" }
Ok "installer log written"

# --- clean up -----------------------------------------------------------------
Write-Host "==> Cleaning up"
$relaunched | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue
$unins = Get-ChildItem "$installDir\unins*.exe" | Select-Object -First 1
if ($unins) { Start-Process -FilePath $unins.FullName -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES" -Wait }
if (Test-Path $installDir) { Fail "uninstall left $installDir" }
Ok "uninstalled"
Write-Host "PASS" -ForegroundColor Green
