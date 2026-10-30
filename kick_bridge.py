"""Kick chat bridge — framework-agnostic.

Receives Kick chat messages via webhook, hands them to a bot adapter
(Radeon AI Co-Host, custom Python bot, Node bot, etc.), and optionally
posts the bot's reply back into Kick chat.

Three ways to connect a bot:
  1. File adapter (default): writes inbox/messages.json, reads outbox.
     Works with any language/framework that can read/write JSON.
  2. HTTP adapter: POSTs each message to a callback URL your bot exposes.
  3. Custom adapter: subclass AdapterBase and implement handle_message().

No Kick login credentials are stored here permanently. You register a
Kick developer app, run setup once to get an access token, and the bridge
uses it to post replies back into chat.
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
MESSAGES_PATH = os.path.join(ROOT, "messages.json")
LOG_PATH = os.path.join(ROOT, "bridge.log")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


DEFAULT_CONFIG = {
    "kick_channel": "",            # your Kick channel slug
    "kick_client_id": "",          # from Kick developer dashboard
    "kick_client_secret": "",      # from Kick developer dashboard
    "kick_access_token": "",       # populated after OAuth setup
    "kick_refresh_token": "",      # populated after OAuth setup
    "public_url": "",              # https://xxxx.cloudflared_url or ngrok url
    "webhook_port": 8765,          # local port the bridge listens on
    "adapter": "file",             # file | http | custom
    "http_callback": "",           # http://localhost:5000/kick (if adapter=http)
    "text_replies": True,          # post bot replies back into Kick chat
    "command_prefix": "!ask",      # only respond to messages starting with this
    "reply_prefix": "Bot:",        # prepended to replies posted in Kick
    "max_reply_chars": 500,        # Kick message length safety
    "log_to_console": True,
}


# ---------------------------------------------------------------------------
# Message queue (file-based, works with any framework)
# ---------------------------------------------------------------------------

class MessageQueue:
    """Thread-safe JSON mailbox. Any bot can drop a reply into outbox and
    the bridge will post it to Kick."""

    def __init__(self, path=MESSAGES_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._seed()

    def _seed(self):
        if not os.path.exists(self.path):
            self.write({"inbox": [], "outbox": []})

    def read(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"inbox": [], "outbox": []}

    def write(self, data):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    def push_inbox(self, username, message, msg_id):
        data = self.read()
        data["inbox"].append({
            "id": msg_id,
            "username": username,
            "message": message,
            "received_at": time.time(),
        })
        # keep inbox bounded
        data["inbox"] = data["inbox"][-200:]
        self.write(data)

    def pop_outbox(self):
        """Return and clear the outbox so the bot's reply is delivered once."""
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return []
        outbox = data.get("outbox", [])
        data["outbox"] = []
        self.write(data)
        return outbox


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class AdapterBase:
    """Subclass this to plug the bridge into any bot framework."""

    def name(self):
        return self.__class__.__name__

    def handle_message(self, username, message, msg_id):
        """Called when a Kick message arrives. Return a reply string or None."""
        raise NotImplementedError

    def on_reply(self, reply, msg_id):
        """Called after handle_message returns a reply. Default: enqueue to
        file outbox so kick_bridge.py posts it."""
        return None


class FileAdapter(AdapterBase):
    """Writes incoming messages to messages.json inbox. The external bot
    reads inbox, computes a reply, and writes it to outbox. The bridge
    polls outbox and posts to Kick."""

    def __init__(self, queue: MessageQueue):
        self.queue = queue

    def handle_message(self, username, message, msg_id):
        self.queue.push_inbox(username, message, msg_id)
        return None  # reply comes later via outbox

    def poll_outbox(self):
        return self.queue.pop_outbox()


class HttpAdapter(AdapterBase):
    """POSTs each Kick message to a callback URL your bot owns."""

    def __init__(self, callback_url, timeout=10):
        self.callback_url = callback_url
        self.timeout = timeout

    def handle_message(self, username, message, msg_id):
        payload = {
            "id": msg_id,
            "username": username,
            "message": message,
            "platform": "kick",
        }
        try:
            r = requests.post(self.callback_url, json=payload, timeout=self.timeout)
            if r.status_code == 200:
                try:
                    data = r.json()
                    return data.get("reply")
                except Exception:
                    return r.text[:500] or None
        except Exception as e:
            _log(f"HTTP adapter error: {e}")
        return None


# ---------------------------------------------------------------------------
# Kick API helpers
# ---------------------------------------------------------------------------

def kick_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def post_kick_message(channel, access_token, text):
    """Send a chat message into Kick as the bot account."""
    url = f"https://api.kick.com/public/v1/channels/{channel}/chat/messages"
    try:
        r = requests.post(url, headers=kick_headers(access_token),
                          json={"content": text}, timeout=10)
        return r.status_code in (200, 201, 202), r.text
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------

class KickWebhookHandler(BaseHTTPRequestHandler):
    """Receives Kick chat.message.sent webhooks."""

    def log_message(self, format, *args):
        if load_config().get("log_to_console", True):
            _log("%s - %s" % (self.address_string(), format % args))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad json")
            return

        event_type = payload.get("type") or payload.get("event", {}).get("type")
        data = payload.get("data", payload)

        if event_type == "chat.message.sent" or "message" in str(data).lower():
            username = (data.get("sender", {}) or {}).get("username", "viewer")
            message = data.get("content") or data.get("message") or ""
            msg_id = data.get("id") or str(uuid.uuid4())

            if message and username:
                _log(f"Kick <- {username}: {message}")
                reply = BRIDGE.dispatch(username, message, msg_id)
                if reply:
                    _log(f"Kick -> {username}: {reply[:80]}...")
                    if load_config().get("text_replies"):
                        _post_reply(reply)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


# ---------------------------------------------------------------------------
# Bridge core
# ---------------------------------------------------------------------------

BRIDGE = None


class KickBridge:
    def __init__(self, config, adapter):
        self.config = config
        self.adapter = adapter
        self.queue = adapter.queue if isinstance(adapter, FileAdapter) else None
        self._server = None
        self._poller = None

    def start(self):
        cfg = self.config
        port = int(cfg.get("webhook_port", 8765))
        self._server = ThreadingHTTPServer(("0.0.0.0", port), KickWebhookHandler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        _log(f"Bridge listening on port {port}")
        pub = cfg.get("public_url", "(not set)")
        _log(f"Public URL for Kick webhook: {pub}")
        _log(f"Adapter: {self.adapter.name()}")

        if isinstance(self.adapter, FileAdapter):
            self._poller = threading.Thread(target=self._poll_loop, daemon=True)
            self._poller.start()

    def _poll_loop(self):
        while True:
            time.sleep(2)
            if not self.queue:
                continue
            for item in self.adapter.poll_outbox():
                username = item.get("username", "viewer")
                text = item.get("reply") or item.get("message") or ""
                msg_id = item.get("id")
                if not text:
                    continue
                _log(f"Outbox -> {username}: {text[:80]}")
                if self.config.get("text_replies"):
                    _post_reply(text)

    def dispatch(self, username, message, msg_id):
        # Command prefix gate
        prefix = self.config.get("command_prefix", "!ask")
        if prefix and not message.strip().lower().startswith(prefix.lower()):
            return None
        # Strip prefix so the bot sees just the question
        clean = message.strip()
        if prefix:
            clean = clean[len(prefix):].strip()
        if not clean:
            return None

        reply = self.adapter.handle_message(username, clean, msg_id)

        # If adapter returned None but uses file outbox, reply arrives via poll.
        if reply is None and isinstance(self.adapter, FileAdapter):
            return None

        return reply


def _post_reply(text):
    cfg = load_config()
    token = cfg.get("kick_access_token", "")
    channel = cfg.get("kick_channel", "")
    if not token or not channel:
        _log("Cannot post reply: missing kick_access_token or kick_channel")
        return
    prefix = cfg.get("reply_prefix", "Bot:")
    full = f"{prefix} {text}"[: cfg.get("max_reply_chars", 500)]
    ok, resp = post_kick_message(channel, token, full)
    if not ok:
        _log(f"Kick post failed: {resp}")


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_forever(config, adapter):
    global BRIDGE
    BRIDGE = KickBridge(config, adapter)
    BRIDGE.start()
    _log("Bridge running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _log("Shutting down.")
        if BRIDGE and BRIDGE._server:
            BRIDGE._server.shutdown()


if __name__ == "__main__":
    cfg = {**DEFAULT_CONFIG, **load_config()}
    adapter = FileAdapter(MessageQueue())
    run_forever(cfg, adapter)
