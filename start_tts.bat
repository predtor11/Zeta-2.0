@echo off
setlocal
title Zeta voice (Chatterbox)
cd /d "%~dp0"

if not exist ".venv-tts\Scripts\python.exe" (
  echo The voice environment is missing. Run install_tts.bat first.
  pause
  exit /b 1
)

echo Starting the Chatterbox voice server on http://127.0.0.1:8766
echo The first run downloads the model; later starts take a few seconds.
echo Leave this window open, or close it to stop the voice.
echo.
.venv-tts\Scripts\python scripts\chatterbox_server.py %*
pause
