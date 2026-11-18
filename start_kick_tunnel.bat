@echo off
REM ---------------------------------------------------------------------------
REM Starts a cloudflared quick tunnel for the Kick webhook in its own window.
REM The webhook receiver is your bot (demo_main.py or your own process) and
REM must already be running on the port in .env (default 8770).
REM It prints the current public URL and writes it to data\kick_tunnel_url.txt.
REM When the URL changes: put it in Kick's developer portal and re-run
REM   python setup_kick_webhook.py subscribe
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"

REM Use the kit's own venv when there is one (that's where requirements.txt
REM was installed); otherwise whatever "python" is on PATH.
set "PY=python"
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if exist "%HERE%venv\Scripts\python.exe" set "PY=%HERE%venv\Scripts\python.exe"
if "%PY%"=="python" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [kick-tunnel] python not found on PATH, and no venv in this folder.
        exit /b 1
    )
)

echo [kick-tunnel] starting cloudflared quick tunnel with "%PY%"...
echo [kick-tunnel] its window is minimised on the taskbar ("kick-tunnel") - the
echo              public URL is shown there and written to data\kick_tunnel_url.txt
start "kick-tunnel" /min "%PY%" "%HERE%kick_tunnel_host.py"
endlocal
exit /b 0
