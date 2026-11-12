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

Security: every POST must carry a valid Kick RSA signature, checked with the
same code the production listener uses (input/kick_webhook.py). The server
binds to 127.0.0.1 by default; the tunnel (cloudflared/ngrok) connects from
this machine, so it still works. Binding to localhost alone is NOT enough
protection, because tunnel traffic also arrives from localhost - that is why
the signature check is required and cannot be switched off.
"""

import json
import os
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

try:
    # One copy of the crypto code: reuse the production listener's checker.
    from input.kick_webhook import verify_signature, fetch_public_key
except ImportError as _e:  # aiohttp / rich not installed
    raise SystemExit(
        f"kick_bridge.py could not load the signature checker ({_e}).\n"
        "Install the kit's requirements first:  python -m pip install -r requirements.txt"
    )

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
MESSAGES_PATH = os.path.join(ROOT, "messages.json")
LOG_PATH = os.path.join(ROOT, "bridge.log")

# Kick chat webhooks are a few KB. Anything far bigger is not from Kick.
MAX_BODY_BYTES = 64 * 1024


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
    "bind_host": "127.0.0.1",      # keep local; the tunnel connects from this PC
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
# Webhook security
# ---------------------------------------------------------------------------

class SignatureChecker:
    """Checks Kick's RSA signature on each webhook request.

    Same scheme as KickWebhookListener._verify in input/kick_webhook.py.
    Kick's public key is fetched live (Kick can rotate it), cached for an
    hour, and the old key is kept if a refresh fails, so one network blip
    does not reject every message.
    """

    KEY_TTL_S = 3600
    RETRY_S = 60

    def __init__(self, fetch=fetch_public_key, verify=verify_signature):
        self._fetch = fetch
        self._verify = verify
        self._lock = threading.Lock()
        self._key = None
        self._key_at = 0.0
        self._next_try = 0.0

    def _current_key(self):
        with self._lock:
            now = time.time()
            stale = self._key is None or (now - self._key_at) > self.KEY_TTL_S
            if stale and now >= self._next_try:
                try:
                    self._key = self._fetch()
                    self._key_at = now
                except Exception as e:
                    self._next_try = now + self.RETRY_S
                    note = " (still using the cached key)" if self._key else ""
                    _log(f"Cannot fetch Kick public key: {e}{note}")
            return self._key

    def check(self, headers, raw: bytes) -> bool:
        msg_id = headers.get("Kick-Event-Message-Id", "")
        ts = headers.get("Kick-Event-Message-Timestamp", "")
        sig = headers.get("Kick-Event-Signature", "")
        if not (msg_id and ts and sig):
            return False
        key = self._current_key()
        if not key:
            return False
        signed = f"{msg_id}.{ts}.".encode("utf-8") + raw
        if self._verify(key, signed, sig):
            return True
        # Same body-only fallback as the production listener. Tightening it
        # belongs to the replay-protection fix, not this one.
        return self._verify(key, raw, sig)


class SeenIds:
    """Remembers recent Kick message ids so a retried delivery is handled once."""

    def __init__(self, max_items=2000):
        self._max = max_items
        self._ids = OrderedDict()
        self._lock = threading.Lock()

    def seen_before(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        with self._lock:
            if msg_id in self._ids:
                return True
            self._ids[msg_id] = time.time()
            while len(self._ids) > self._max:
                self._ids.popitem(last=False)
            return False


CHECKER = SignatureChecker()
SEEN = SeenIds()
STATS = {"received": 0, "accepted": 0, "rejected": 0}
_STATS_LOCK = threading.Lock()


def _count(key):
    with _STATS_LOCK:
        STATS[key] += 1
        return STATS[key]


# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------

class KickWebhookHandler(BaseHTTPRequestHandler):
    """Receives Kick chat.message.sent webhooks.

    Order matters: size check, then signature, then dedupe, then parsing.
    Nothing from an unsigned request is ever parsed or queued.
    """

    def log_message(self, format, *args):
        if load_config().get("log_to_console", True):
            _log("%s - %s" % (self.address_string(), format % args))

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        _count("received")

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, b"length required")
        if length < 0 or length > MAX_BODY_BYTES:
            return self._reply(413, b"too large")
        raw = self.rfile.read(length)

        if not CHECKER.check(self.headers, raw):
            n = _count("rejected")
            if n <= 3 or n % 50 == 0:   # don't let junk traffic flood the log
                _log(f"Rejected a request without a valid Kick signature (total {n})")
            return self._reply(401, b"invalid signature")

        msg_id = self.headers.get("Kick-Event-Message-Id", "")
        if SEEN.seen_before(msg_id):
            return self._reply(200, b"duplicate")   # 200 so Kick stops retrying

        if self.headers.get("Kick-Event-Type", "") != "chat.message.sent":
            return self._reply(200, b"ignored")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, b"bad json")
        if not isinstance(payload, dict):
            return self._reply(400, b"bad json")

        cfg = load_config()
        want = (cfg.get("kick_channel") or "").strip().lower()
        slug = ((payload.get("broadcaster") or {}).get("channel_slug") or "").strip().lower()
        if want and slug and slug != want:
            return self._reply(200, b"other channel")

        username = ((payload.get("sender") or {}).get("username") or "").strip()
        message = (payload.get("content") or "").strip()
        if message and username:
            _count("accepted")
            _log(f"Kick <- {username}: {message}")
            reply = BRIDGE.dispatch(username, message, msg_id)
            if reply:
                _log(f"Kick -> {username}: {reply[:80]}...")
                if cfg.get("text_replies"):
                    _post_reply(reply)

        self._reply(200, b"ok")


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
        host = (cfg.get("bind_host") or "127.0.0.1").strip()
        self._server = ThreadingHTTPServer((host, port), KickWebhookHandler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        _log(f"Bridge listening on http://{host}:{port} (Kick signature required)")
        if host not in ("127.0.0.1", "localhost", "::1"):
            _log(f"Note: bind_host is {host}, so other machines can reach this port. "
                 "Signatures are still checked, but 127.0.0.1 is safer.")
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
