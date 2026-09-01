# Fetch the Windows cslol mod-tools.exe into tools\.
#
# The Windows build of mod-tools is self-contained (the cslol-dll patcher is
# embedded), so tibbers needs only this one file -- the same binary cslol-
# manager and Rose use. It ships inside cslol-manager's Windows release, which
# is a 7-Zip self-extracting archive, so this downloads that and pulls the
# executable out of it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_modtools.ps1
#
# NOTE: written on macOS and not yet run on Windows. If extraction fails, run
# cslol-manager-windows.exe yourself and copy its mod-tools.exe into tools\.

$ErrorActionPreference = "Stop"

$root  = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null

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
           "$sfx by hand and copy its mod-tools.exe into $tools")
}

Copy-Item $modtools.FullName (Join-Path $tools "mod-tools.exe") -Force
Write-Host "==> Installed mod-tools.exe into $tools"
