# Build the Windows app with PyInstaller into dist\Tibbers\.
#
# The counterpart of scripts/build_app.sh on macOS: a frozen snapshot that
# never reads this checkout. main.py, the tibbers package, tibbers\static and
# assets\ are bundled by tibbers.spec; the patcher binaries are NOT -- the
# frozen app fetches them into the data directory on first run.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Installer
#
# -Installer additionally builds the setup.exe with Inno Setup (needs ISCC on
# PATH; see scripts\tibbers.iss).

param(
    [switch]$Installer
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No venv at $root\.venv -- see the README / WINDOWS.md setup."
}

# PyInstaller lives in the venv; install it there if missing.
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Installing PyInstaller into the venv"
    & $python -m pip install pyinstaller
}

# A .ico is required for the window/exe icon; derive it from the png if absent.
$ico = Join-Path $root "assets\tibbers.ico"
if (-not (Test-Path $ico)) {
    Write-Host "==> Generating assets\tibbers.ico from tibbers.png"
    & $python -c @"
from PIL import Image
img = Image.open(r'$root\assets\tibbers.png').convert('RGBA')
img.save(r'$ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64)])
"@
}

Write-Host "==> Cleaning previous build"
Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $root "dist\Tibbers") -ErrorAction SilentlyContinue

Write-Host "==> Building dist\Tibbers with PyInstaller"
& $python -m PyInstaller --noconfirm --clean (Join-Path $root "tibbers.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\Tibbers\Tibbers.exe"
if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
Write-Host "==> Built $exe"

# From 1.0.2 the update asset is the installer itself (update.py asset_name());
# the zip is still produced because a 1.0.0 / 1.0.1 app looks for it and swaps
# it in with its own script. Drop it once no such install is left.
$zip = Join-Path $root "dist\Tibbers-windows.zip"
Write-Host "==> Packing $zip for OTA / release"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $root "dist\Tibbers") -DestinationPath $zip
Write-Host "==> Wrote $zip"

if ($Installer) {
    $iss = Join-Path $root "scripts\tibbers.iss"
    # ISCC is rarely on PATH -- winget drops it in LocalAppData -- so look in
    # the usual places as well as PATH.
    $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $iscc = @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) {
        throw ("ISCC.exe (Inno Setup) not found. Install it " +
               "(winget install JRSoftware.InnoSetup), or run it on $iss")
    }
    # Stamp the installer with the app's own version, so the two never drift.
    $version = (& $python -c "import tibbers; print(tibbers.__version__)").Trim()
    Write-Host "==> Building the installer with Inno Setup (v$version)"
    & $iscc "/DMyAppVersion=$version" $iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "==> Installer written to dist\Tibbers-windows-setup.exe (v$version)"
}

Write-Host ""
Write-Host "Done. Run dist\Tibbers\Tibbers.exe, or ship the installer."
