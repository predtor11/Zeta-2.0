@echo off
setlocal
title Zeta
cd /d "%~dp0"
if not exist backend\.venv\Scripts\python.exe (
  echo Run install.bat first.
  pause
  exit /b 1
)
python scripts\start.py %*
