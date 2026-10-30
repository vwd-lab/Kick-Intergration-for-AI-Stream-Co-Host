"""Minimal config loader for the Kick sharing demo.

Loads `.env` from THIS folder (not the current working directory) and exposes
only the settings the Kick kit needs. This is a trimmed copy of the full
project's config.py, enough to run the kit standalone.

The bridge modules themselves (input/kick_webhook.py, input/kick_sender.py)
read no config; everything is passed to their constructors. This file exists
for demo_main.py and the setup scripts.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(_HERE, ".env"))
except ImportError:
    pass  # .env optional; env vars read from the OS directly


def _path(value, default):
    """Resolve a relative path against the kit folder, not the CWD."""
    value = (value or "").strip()
    if not value:
        return default
    return value if os.path.isabs(value) else os.path.join(_HERE, value)


# --- Kick channel / receiver ---
KICK_ENABLED = os.getenv("KICK_ENABLED", "1") == "1"
KICK_CHANNEL = os.getenv("KICK_CHANNEL", "").strip().lower()
KICK_COMMAND_PREFIX = os.getenv("KICK_COMMAND_PREFIX", "!ask").strip()
KICK_WEBHOOK_PORT = int(os.getenv("KICK_WEBHOOK_PORT", "8770"))
KICK_WEBHOOK_URL = os.getenv("KICK_WEBHOOK_URL", "").strip().rstrip("/")
KICK_WEBHOOK_HEALTH_S = int(os.getenv("KICK_WEBHOOK_HEALTH_S", "30"))
KICK_TUNNEL_URL_FILE = _path(
    os.getenv("KICK_TUNNEL_URL_FILE", ""),
    os.path.join(_HERE, "data", "kick_tunnel_url.txt"),
)

# --- Text replies (optional) ---
KICK_TEXT_REPLIES = os.getenv("KICK_TEXT_REPLIES", "0") == "1"
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "").strip()
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "").strip()
KICK_TOKENS_FILE = _path(
    os.getenv("KICK_TOKENS_FILE", ""),
    os.path.join(_HERE, "kick_tokens.json"),
)
KICK_SEND_INTERVAL_S = float(os.getenv("KICK_SEND_INTERVAL_S", "2.0"))

# --- Safety ---
# The account the bot posts as. Used to sanity-check the OAuth result and,
# when set, to ignore the bot's own messages if Kick echoes them back.
KICK_BOT_ACCOUNT = os.getenv("KICK_BOT_ACCOUNT", "").strip().lower()
# Extra usernames to ignore entirely (comma-separated, case-insensitive).
KICK_IGNORED_USERS = [
    u.strip().lower()
    for u in os.getenv("KICK_IGNORED_USERS", "").split(",")
    if u.strip()
]
