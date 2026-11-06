@echo off
powershell.exe -NoProfile -Command "$p = Start-Process -FilePath '%~dp0.local-tools\vs_BuildTools.exe' -ArgumentList '--passive','--wait','--norestart','--nocache','--add','Microsoft.VisualStudio.Workload.VCTools','--includeRecommended' -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
if errorlevel 1 (
  echo Build Tools installation did not complete. If a restart was requested, restart Windows.
  pause
  exit /b 1
)
echo Build Tools installed. Launch Tibbers; it builds its bridge on first start.
pause
