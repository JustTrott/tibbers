@echo off
rem Optional launcher for a source checkout: double-clicking Tibbers.exe needs
rem no script. Tibbers picks the LTK Manager backend on its own when LTK
rem Manager is installed, and builds or installs its bridge on first launch.
setlocal
cd /d "%~dp0" || exit /b 1
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
