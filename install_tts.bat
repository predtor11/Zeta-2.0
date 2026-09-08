@echo off
setlocal
title Zeta - install the local voice (Chatterbox)
cd /d "%~dp0"

echo.
echo  Zeta local voice installer
echo  --------------------------
echo  This creates a separate environment (.venv-tts) for Chatterbox TTS.
echo  It downloads about 3 GB (PyTorch with CUDA) plus the model on first run.
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11+ is required and must be on PATH.
  pause
  exit /b 1
)

if not exist ".venv-tts\Scripts\python.exe" (
  echo Creating .venv-tts ...
  python -m venv .venv-tts
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

echo Installing PyTorch with CUDA support ...
.venv-tts\Scripts\python -m pip install --upgrade pip
.venv-tts\Scripts\python -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
if errorlevel 1 (
  echo CUDA wheels failed; falling back to the CPU build ^(slower^).
  .venv-tts\Scripts\python -m pip install torch==2.6.0 torchaudio==2.6.0
)

echo Installing Chatterbox ...
.venv-tts\Scripts\python -m pip install chatterbox-tts fastapi uvicorn
if errorlevel 1 (
  echo Chatterbox failed to install. See the messages above.
  pause
  exit /b 1
)

.venv-tts\Scripts\python -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"

echo.
echo  Done. The voice server starts by itself when Zeta first speaks,
echo  or you can run it yourself with start_tts.bat.
echo.
echo  To use your own voice: put a clean 7-15 second WAV recording in the
echo  voice\ folder and set CHATTERBOX_VOICE=voice\your_clip.wav in .env
echo.
pause
