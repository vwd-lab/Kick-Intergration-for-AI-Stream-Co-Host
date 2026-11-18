"""Kick chat bridge — framework-agnostic.

Receives Kick chat messages via webhook, hands them to a bot adapter
(Radeon AI Co-Host, custom Python bot, Node bot, etc.), and optionally
posts the bot's reply back into Kick chat.

Three ways to connect a bot:
  1. File adapter (default): writes each chat message as its own file in the
     mailbox folder (mailbox/inbox) and posts the replies your bot drops in
     mailbox/outbox. See kick_mailbox.py for the format. Works with any
     language/framework that can read/write JSON files.
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
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

try:
    # One copy of the crypto code: reuse the production listener's checker.
    from input.kick_webhook import verify_signature, fetch_public_key, ReplayGuard
except ImportError as _e:  # aiohttp / rich not installed
    raise SystemExit(
        f"kick_bridge.py could not load the signature checker ({_e}).\n"
        "Install the kit's requirements first:  python -m pip install -r requirements.txt"
    )

from kick_mailbox import Mailbox

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOG_PATH = os.path.join(ROOT, "bridge.log")


def _mailbox_dir(cfg):
    """Mailbox folder: config.json "mailbox_dir", else KICK_MAILBOX_DIR from
    the environment, else the "mailbox" folder in this kit (the Co-Host's
    default too). Relative paths are taken from this kit's folder."""
    folder = (cfg.get("mailbox_dir") or os.getenv("KICK_MAILBOX_DIR") or "").strip()
    if not folder:
        return os.path.join(ROOT, "mailbox")
    return folder if os.path.isabs(folder) else os.path.join(ROOT, folder)

# Kick chat webhooks are a few KB. Anything far bigger is not from Kick.
MAX_BODY_BYTES = 64 * 1024


def stop_cleanly_when_gui_asks():
    """When a GUI (kick_gui.py / discord_gui.py) started this script, it keeps
    our stdin open and closes it to ask us to stop. Turn that into Ctrl+C, so
    the normal shutdown code below runs - the GUI used to kill the process
    outright, which skipped it. Does nothing when run by hand."""
    if os.environ.get("BRIDGE_GUI_CHILD") != "1" or sys.stdin is None:
        return
    import _thread

    def watch():
        try:
            while sys.stdin.readline():
                pass
        except (OSError, ValueError):
            pass
        _thread.interrupt_main()

    threading.Thread(target=watch, daemon=True).start()


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
    # Holds the app's client secret: readable only by this Windows account.
    from input.kick_sender import write_private_json
    write_private_json(CONFIG_PATH, cfg)


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
    "mailbox_dir": "",             # file adapter folder; empty = <this kit>/mailbox
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
    """The shared mailbox folder (see kick_mailbox.py): one file per message,
    so the bridge and your bot can never overwrite each other's messages.

    Kept under this name, with the same two methods, so existing adapters
    (adapter_base.py) keep working. It used to be a single messages.json that
    both programs read, changed and wrote back, which lost messages.
    """

    def __init__(self, path=None):
        self.mailbox = Mailbox(path or _mailbox_dir(load_config()))
        self.mailbox.ensure()
        self.path = self.mailbox.folder

    def push_inbox(self, username, message, msg_id):
        self.mailbox.put_inbox(username, message, msg_id)

    def pop_outbox(self):
        """Claim the waiting replies; each is delivered once."""
        return self.mailbox.take_outbox()


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
    """Writes incoming messages to the mailbox inbox. The external bot reads
    them, computes a reply, and drops it in the outbox. The bridge polls the
    outbox and posts to Kick."""

    def __init__(self, queue: MessageQueue = None):
        self.queue = queue or MessageQueue()

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


# Replies to Kick are posted by _post_reply() below, through the same
# KickSender production mode uses.


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

    def check(self, headers, raw: bytes):
        """"full" (Kick's documented scheme: id, timestamp and body signed),
        "body" (body only, as Kick's sample code does) or None (not Kick)."""
        msg_id = headers.get("Kick-Event-Message-Id", "")
        ts = headers.get("Kick-Event-Message-Timestamp", "")
        sig = headers.get("Kick-Event-Signature", "")
        if not (msg_id and ts and sig):
            return None
        key = self._current_key()
        if not key:
            return None
        signed = f"{msg_id}.{ts}.".encode("utf-8") + raw

        def check(k):
            if self._verify(k, signed, sig):
                return "full"
            # Body-only signatures are accepted too; REPLAY then takes the id
            # and time from inside the signed body, not the unsigned headers.
            if self._verify(k, raw, sig):
                return "body"
            return None

        mode = check(key)
        if mode is None:
            # Maybe Kick rotated its key: fetch it again (at most every
            # 5 minutes) rather than reject chat until the hourly refresh.
            fresh = self._refetch_if_old(key)
            if fresh and fresh != key:
                mode = check(fresh)
        return mode

    def _refetch_if_old(self, key):
        with self._lock:
            now = time.time()
            if self._key != key or now - self._key_at < 300 or now < self._next_try:
                return self._key
            try:
                self._key = self._fetch()
                self._key_at = now
            except Exception as e:
                self._next_try = now + self.RETRY_S
                _log(f"Cannot fetch Kick public key: {e} (still using the cached key)")
            return self._key


CHECKER = SignatureChecker()
# Same replay protection as the production listener (input/kick_webhook.py):
# signed time must be recent, and handled ids are remembered across restarts.
REPLAY = ReplayGuard(seen_file=os.path.join(ROOT, "data", "kick_seen_ids_simple.json"),
                     log=lambda msg: _log(msg))
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

        mode = CHECKER.check(self.headers, raw)
        if not mode:
            n = _count("rejected")
            if n <= 3 or n % 50 == 0:   # don't let junk traffic flood the log
                _log(f"Rejected a request without a valid Kick signature (total {n})")
            return self._reply(401, b"invalid signature")

        msg_id = self.headers.get("Kick-Event-Message-Id", "")
        if self.headers.get("Kick-Event-Type", "") != "chat.message.sent":
            return self._reply(200, b"ignored")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, b"bad json")
        if not isinstance(payload, dict):
            return self._reply(400, b"bad json")

        # A resent (replayed) message: answered 200 so Kick stops retrying,
        # but not handled again.
        verdict = REPLAY.check(mode, self.headers, payload)
        if verdict != "ok":
            return self._reply(200, verdict.encode())

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


def _adapter_name(adapter):
    """Adapters in this file have a name() method; the ones in adapter_base.py
    have a plain `name` string. Calling the string crashed the bridge."""
    name = getattr(adapter, "name", None)
    if callable(name):
        name = name()
    return name or adapter.__class__.__name__


def _uses_outbox(adapter):
    """Any adapter with poll_outbox() gets its replies from the mailbox -
    including the FileAdapter in adapter_base.py, which isn't a subclass of
    the one in this file."""
    return callable(getattr(adapter, "poll_outbox", None))


class KickBridge:
    def __init__(self, config, adapter):
        self.config = config
        self.adapter = adapter
        self.queue = getattr(adapter, "queue", None) if _uses_outbox(adapter) else None
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
        _log(f"Adapter: {_adapter_name(self.adapter)}")

        if _uses_outbox(self.adapter):
            if self.queue is not None:
                _log(f"Mailbox: {getattr(self.queue, 'path', '?')}")
            self._poller = threading.Thread(target=self._poll_loop, daemon=True)
            self._poller.start()

    def _poll_loop(self, interval_s=2.0):
        # Must never die: before, one error here stopped all replies until
        # the bridge was restarted.
        while True:
            time.sleep(interval_s)
            try:
                self._post_outbox_once()
            except Exception as e:
                _log(f"Outbox error (will retry): {type(e).__name__}: {e}")

    def _post_outbox_once(self):
        for item in self.adapter.poll_outbox():
            username = item.get("username", "viewer")
            text = item.get("reply") or item.get("message") or ""
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

        # If adapter returned None but uses the mailbox, reply arrives via poll.
        if reply is None and _uses_outbox(self.adapter):
            return None

        return reply


_SENDER = None
_SENDER_LOCK = threading.Lock()


def _kick_sender(cfg):
    """The same sender production mode uses (input/kick_sender.py): Kick's
    documented chat API, with the login token refreshed automatically.

    Simple mode used to post a fixed access token from config.json to an
    address Kick doesn't document (.../channels/<slug>/chat/messages), and
    never refreshed it - Kick tokens last about 2 hours."""
    global _SENDER
    with _SENDER_LOCK:
        if _SENDER is None:
            from input.kick_sender import KickSender
            _SENDER = KickSender(client_id=cfg.get("kick_client_id", ""),
                                 client_secret=cfg.get("kick_client_secret", ""),
                                 target_slug=cfg.get("kick_channel", ""))
            if not _SENDER.enabled:
                _log("Text replies need a one-time login for the bot account: run "
                     "setup_kick_auth.py (it writes kick_tokens.json). The access token "
                     "field in config.json isn't used any more.")
        return _SENDER


def _post_reply(text):
    cfg = load_config()
    if not cfg.get("kick_channel", ""):
        _log("Cannot post reply: kick_channel is not set")
        return
    prefix = cfg.get("reply_prefix", "Bot:")
    full = f"{prefix} {text}".strip()[: cfg.get("max_reply_chars", 500)]
    sender = _kick_sender(cfg)
    if not sender.enabled:
        _log("Kick reply not sent (text replies aren't set up - see above)")
        return
    if not sender.send(full):
        _log("Kick post failed (see the messages above)")


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


def make_adapter(cfg):
    """The adapter chosen in the GUI / setup ("file" or "http")."""
    if (cfg.get("adapter") or "file").strip().lower() == "http":
        if not cfg.get("http_callback"):
            _log("adapter is 'http' but http_callback is empty - using the file mailbox instead.")
        else:
            return HttpAdapter(cfg["http_callback"])
    return FileAdapter(MessageQueue(_mailbox_dir(cfg)))


if __name__ == "__main__":
    stop_cleanly_when_gui_asks()
    cfg = {**DEFAULT_CONFIG, **load_config()}
    run_forever(cfg, make_adapter(cfg))
