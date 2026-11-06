param([string]$ManagerPath = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install requirements-windows.txt first.' }
$env:TIBBERS_LTK_BACKEND = 'manager'
# Locate installs on other drives even when LeagueClient is not running.
if (-not $env:TIBBERS_LEAGUE_ROOT) {
    foreach ($drive in [System.IO.DriveInfo]::GetDrives()) {
        if (-not $drive.IsReady) { continue }
        $candidate = Join-Path $drive.RootDirectory.FullName 'Riot Games\League of Legends'
        if ((Test-Path -LiteralPath (Join-Path $candidate 'LeagueClient.exe')) -and
            (Test-Path -LiteralPath (Join-Path $candidate 'Game\League of Legends.exe'))) {
            $env:TIBBERS_LEAGUE_ROOT = $candidate
            break
        }
    }
}
if ($env:TIBBERS_LEAGUE_ROOT) { Write-Host "League installation: $env:TIBBERS_LEAGUE_ROOT" }
if ($ManagerPath) { $env:TIBBERS_LTK_MANAGER = $ManagerPath }
# Install a staged bridge only on the next user-requested start.
$pending = Join-Path $root 'tools\ltf\pending'
if (Test-Path -LiteralPath (Join-Path $pending 'LeagueToolFlop.dll')) {
    try {
        Copy-Item -LiteralPath (Join-Path $pending 'LeagueToolFlop.dll') -Destination (Join-Path $root 'tools\ltf\LeagueToolFlop.dll') -Force
        Copy-Item -LiteralPath (Join-Path $pending 'ltk_launcher.exe') -Destination (Join-Path $root 'tools\ltf\ltk_launcher.exe') -Force
        Remove-Item -LiteralPath (Join-Path $pending 'LeagueToolFlop.dll'),(Join-Path $pending 'ltk_launcher.exe')
        Write-Host 'Updated LTK bridge installed (OPT_OUT_AH_V1=4).'
    } catch {
        throw 'Close Tibbers and LTK Manager, then run Start-Bridge.cmd again to install the bridge update.'
    }
}
$launcher = Join-Path $root 'tools\ltf\ltk_launcher.exe'
$dll = Join-Path $root 'tools\ltf\LeagueToolFlop.dll'
if (-not ((Test-Path -LiteralPath $launcher) -and (Test-Path -LiteralPath $dll))) {
    & $python scripts/build_ltf.py
    if ($LASTEXITCODE -ne 0) { throw 'Bridge build failed. Install Visual Studio C++ Build Tools (x64 + Windows SDK).' }
}
Write-Host 'Tibbers: LTK Manager bridge enabled. Select a skin before starting a practice game.'
Write-Host 'Close any other Tibbers and LTK Manager instance first.'
& $python main.py
exit $LASTEXITCODE
