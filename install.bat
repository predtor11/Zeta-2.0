@echo off
setlocal
title Zeta installer
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11+ is required. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
python scripts\setup.py %*
if errorlevel 1 (
  echo.
  echo Setup failed. See the messages above.
  pause
  exit /b 1
)
echo.
echo Setup finished. Run start.bat to launch Zeta.
pause
