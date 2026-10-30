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

where python >nul 2>&1
if errorlevel 1 (
    echo [kick-tunnel] python not found on PATH.
    exit /b 1
)

echo [kick-tunnel] starting cloudflared quick tunnel...
start "kick-tunnel" /min python "%HERE%kick_tunnel_host.py"
endlocal
exit /b 0
