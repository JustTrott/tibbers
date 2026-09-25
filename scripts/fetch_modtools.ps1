# Fetch the Windows injection tools into tools\.
#
# Windows needs TWO pairs, for the reason spelled out in tibbers/_system_windows
# .py: cslol's injection DLL is a dead end on Windows (a kill-switch, and
# Vanguard names it incompatible), but its *overlay builder* still works and is
# open. So the overlay is built with cslol and the injection is done by LTK's
# patcher, the maintained successor whose hook Vanguard accepts.
#
#   mod-tools.exe + cslol-dll.dll         from cslol-manager  -> mkoverlay only
#   ltk_patcher_host.exe + ltk_patcher_dll.dll  from LTK Manager  -> the injection
#
# Each is a load-time pair (the exe loads the dll beside it), so both halves of
# each are installed together.
#
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_modtools.ps1
#
# If either download fails, install that manager yourself and copy its two
# files into tools\ by hand (see the per-section notes below).

$ErrorActionPreference = "Stop"

$root  = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null

# ---------------------------------------------------------------------------
# cslol mod-tools -- the overlay builder (never injects)
# ---------------------------------------------------------------------------

Write-Host "==> Resolving latest cslol-manager release"
$api   = "https://api.github.com/repos/LeagueToolkit/cslol-manager/releases/latest"
$rel   = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "tibbers" }
$asset = $rel.assets | Where-Object { $_.name -eq "cslol-manager-windows.exe" } | Select-Object -First 1
if (-not $asset) { throw "cslol-manager-windows.exe not found in the latest release" }

$tmp = Join-Path $env:TEMP "tibbers-cslol"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$sfx = Join-Path $tmp $asset.name

Write-Host "==> Downloading $($asset.name)"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $sfx

Write-Host "==> Extracting"
$extract = Join-Path $tmp "extracted"
Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $extract | Out-Null

$sevenzip = Get-Command 7z.exe -ErrorAction SilentlyContinue
if ($sevenzip) {
    & 7z.exe x "-o$extract" -y $sfx | Out-Null
} else {
    # The 7-Zip console SFX honours -o to extract without launching a GUI.
    & $sfx "-o$extract" "-y" | Out-Null
}

$modtools = Get-ChildItem -Path $extract -Recurse -Filter "mod-tools.exe" |
    Select-Object -First 1
if (-not $modtools) {
    throw ("mod-tools.exe was not found after extraction. Run " +
           "$sfx by hand and copy mod-tools.exe and cslol-dll.dll out of " +
           "its cslol-tools\ into $tools")
}

# The DLL has to be the one from the same cslol-tools\ as the exe: it is a
# load-time import, so a missing or mismatched one is a loader failure with
# no output at all rather than an error tibbers can report.
$dll = Join-Path $modtools.Directory.FullName "cslol-dll.dll"
if (-not (Test-Path $dll)) {
    throw ("cslol-dll.dll was not found beside mod-tools.exe in " +
           "$($modtools.Directory.FullName) -- mod-tools.exe cannot start " +
           "without it. Run $sfx by hand and copy both files into $tools")
}

Copy-Item $modtools.FullName (Join-Path $tools "mod-tools.exe") -Force
Copy-Item $dll (Join-Path $tools "cslol-dll.dll") -Force
Write-Host "==> Installed mod-tools.exe and cslol-dll.dll into $tools"

# ---------------------------------------------------------------------------
# LTK patcher -- the injection host + hook DLL
# ---------------------------------------------------------------------------
#
# LTK ships only an NSIS setup (its MSI went away in v1.20.0). Running that
# would install LTK Manager, so its two patcher files are read out of the
# setup instead by tibbers' own unpacker -- the one the packaged app uses --
# which never runs it: no install, no registry, no Vanguard interaction.

Write-Host "==> Resolving latest LTK Manager release"
$ltkApi = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest"
$ltkRel = Invoke-RestMethod -Uri $ltkApi -Headers @{ "User-Agent" = "tibbers" }
$setup = $ltkRel.assets | Where-Object { $_.name -like "*-setup.exe" } | Select-Object -First 1
if (-not $setup) {
    throw ("no -setup.exe in the latest ltk-manager release. Install LTK " +
           "Manager yourself and copy ltk_patcher_host.exe and " +
           "ltk_patcher_dll.dll from its install folder into $tools")
}

$ltkTmp = Join-Path $env:TEMP "tibbers-ltk"
New-Item -ItemType Directory -Force -Path $ltkTmp | Out-Null
$setupPath = Join-Path $ltkTmp $setup.name

Write-Host "==> Downloading $($setup.name)"
Invoke-WebRequest -Uri $setup.browser_download_url -OutFile $setupPath

Write-Host "==> Extracting LTK patcher (read out of the setup; nothing is run)"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python -c "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from tibbers import wintools; wintools.unpack_nsis(Path(sys.argv[2]), wintools.LTK_FILES, Path(sys.argv[3]))" $root $setupPath $tools
if ($LASTEXITCODE -ne 0) {
    throw ("could not unpack $setupPath. Install LTK Manager yourself and " +
           "copy ltk_patcher_host.exe and ltk_patcher_dll.dll into $tools")
}
Write-Host "==> Installed ltk_patcher_host.exe and ltk_patcher_dll.dll into $tools"

Write-Host ""
Write-Host "All Windows injection tools installed into $tools"
