@echo off
setlocal
cd /d "%~dp0" || exit /b 1
set "TIBBERS_LOCAL_HOST_TEST=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; if (Get-Process -Name ltk-manager,'LTK Manager','League of Legends',ltk_patcher_host -ErrorAction SilentlyContinue) { throw 'Close the running game, LTK Manager and patcher before this test.' }; $d=Join-Path (Get-Location) 'tools\ltf-test'; $pins=@{'ltk_patcher_dll.dll'='5fb14201e23bd13e694b049a6fc275c8d3205318bf0288bdb52fb6c42be6728c';'ltk_patcher_host.exe'='27de1bfdbf37129e42c12b18cc7b4f75c15d39bcd7afefe65c4c33d0e99fe856'}; foreach($name in $pins.Keys){if((Get-FileHash -LiteralPath (Join-Path $d $name)).Hash -ne $pins[$name]){throw ('Unexpected test file: '+$name)}}; Write-Host 'Tibbers local host test: DLL 5fb14201'; & '.\scripts\run_ltk_bridge.ps1'; exit $LASTEXITCODE"
if errorlevel 1 pause
