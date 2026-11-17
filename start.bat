@echo off
REM Double-click to start Clipped Daily. Close this window to stop it.
title Clipped Daily
cd /d "%~dp0"
echo Getting the latest version...
git pull
call .venv\Scripts\activate.bat
if errorlevel 1 (
  echo Could not find .venv - run setup.ps1 first.
  pause
  exit /b 1
)
pip install -q -r requirements.txt
:loop
python -m clipped_daily run
echo.
echo Bot stopped. Restarting in 10 seconds - close this window to quit.
timeout /t 10
goto loop
