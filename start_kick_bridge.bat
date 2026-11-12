@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Kick AI Bridge launcher
REM -----------------------
REM Starts kick_bridge.py as a subprocess. The GUI (kick_gui.py) shows the
REM live log and lets you configure channel, credentials, and adapter.

REM Prefer a project venv if present, otherwise fall back to PATH python.
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM kick_bridge.py needs requests, aiohttp and rich (it reuses the production
REM signature checker). Install once: %PY% -m pip install -r requirements.txt

echo Starting Kick AI Bridge...
"%PY%" kick_gui.py
pause
endlocal
