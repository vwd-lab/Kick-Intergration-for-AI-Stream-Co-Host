@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ========================================================================
REM Kick Bridge Setup - interactive configuration before launching kick_gui
REM ========================================================================
REM This prompts for your Kick settings, writes config.json, then launches
REM kick_gui.py so you can start/stop the bridge from the GUI.
REM ========================================================================

REM Prefer project venv if present
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo.
echo ============================================================
echo   Kick Bridge Setup
echo ============================================================
echo.

set "CHANNEL="
set /p CHANNEL="Kick channel slug (e.g. mychannel): "

set "PUBLIC_URL="
set /p PUBLIC_URL="Public webhook URL (https://..., press Enter to skip): "

set "CLIENT_ID="
set /p CLIENT_ID="Kick Client ID (press Enter to skip): "

set "CLIENT_SECRET="
set /p CLIENT_SECRET="Kick Client Secret (press Enter to skip): "

echo.
echo Adapter options:
echo   file   = shared messages.json mailbox (Radeon AI Co-Host, any language)
echo   http   = POST each message to your bot's callback URL
set "ADAPTER="
set /p ADAPTER="Adapter [file/http] (default file): "
if not defined ADAPTER set ADAPTER=file

echo.
echo Bridge mode options:
echo   production = demo_main.py with signature verification + token refresh (recommended)
echo   simple     = kick_bridge.py single file, signature checked, no token refresh
set "MODE="
set /p MODE="Bridge mode [production/simple] (default production): "
if not defined MODE set MODE=production

set "PORT="
set /p PORT="Webhook port (default 8765): "
if not defined PORT set PORT=8765

set "PREFIX="
set /p PREFIX="Command prefix (default !ask): "
if not defined PREFIX set PREFIX=!ask

set "REPLIES="
set /p REPLIES="Post bot replies back into Kick chat? [yes/no] (default yes): "
if not defined REPLIES set REPLIES=yes

REM Build config.json with Python for safe quoting
%PY% -c "
import json, os

channel  = os.environ.get('CHANNEL', '').strip()
public   = os.environ.get('PUBLIC_URL', '').strip()
cid      = os.environ.get('CLIENT_ID', '').strip()
csecret  = os.environ.get('CLIENT_SECRET', '').strip()
adapter  = os.environ.get('ADAPTER', 'file').strip() or 'file'
mode     = os.environ.get('MODE', 'production').strip() or 'production'
try:
    port   = int(os.environ.get('PORT', '8765').strip())
except ValueError:
    port = 8765
prefix   = os.environ.get('PREFIX', '!ask').strip() or '!ask'
replies  = os.environ.get('REPLIES', 'yes').strip().lower() in ('1', 'y', 'yes', 'true')

cur = {}
try:
    cur = json.load(open('config.json', encoding='utf-8'))
except Exception:
    pass

cfg = {
    'kick_channel': channel,
    'kick_client_id': cid,
    'kick_client_secret': csecret,
    'kick_access_token': cur.get('kick_access_token', ''),
    'kick_refresh_token': cur.get('kick_refresh_token', ''),
    'public_url': public,
    'webhook_port': port,
    'adapter': adapter,
    'http_callback': cur.get('http_callback', ''),
    'bridge_mode': mode,
    'text_replies': replies,
    'command_prefix': prefix,
    'reply_prefix': 'Bot:',
    'max_reply_chars': 500,
    'log_to_console': True,
}

with open('config.json', 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2)

print('config.json written.')
"

echo.
echo ============================================================
echo   Setup complete. Launching Kick GUI...
echo ============================================================
echo.

"%PY%" kick_gui.py
pause
endlocal
