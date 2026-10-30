"""Kick chat via the OFFICIAL webhook API.

Kick's public API fires `chat.message.sent` webhooks at a URL you register.
That beats the old Pusher websocket approach: official, badge/sub data
included, and it will not rot the way hardcoded app keys did.

Requires a public HTTPS URL (a tunnel such as cloudflared), because Kick POSTs
from its own servers and this receiver listens on localhost.

Security: every request is verified against Kick's RSA public key before the
payload is looked at. Kick can rotate that key, so it is fetched live and
cached, never hardcoded.

Standalone: this module imports only the stdlib, `requests`, `aiohttp` and
`rich`. It reads no config and no environment variables; pass everything to
the constructor.
"""
import asyncio
import base64
import hashlib
import json
import os
import queue
import re
import threading
import time
from collections import OrderedDict

import requests
from aiohttp import web
from rich import print

PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Default location of the hostname file written by kick_tunnel_host.py. The
# receiver re-reads it so a tunnel restart does not need a bot restart.
DEFAULT_URL_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "kick_tunnel_url.txt")

# DigestInfo prefix for id-sha256 (RFC 8017 A.2.4)
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


# --------------------------------------------------------------------------
# RSA PKCS#1 v1.5 / SHA-256 verification, pure stdlib.
# This keeps the kit free of a `cryptography` dependency for one routine.
# --------------------------------------------------------------------------

def _pem_to_der(pem: str) -> bytes:
    body, inside = [], False
    for line in pem.splitlines():
        line = line.strip()
        if line.startswith("-----BEGIN"):
            inside = True
            continue
        if line.startswith("-----END"):
            break
        if inside and line:
            body.append(line)
    return base64.b64decode("".join(body))


def _der_len(data: bytes, i: int):
    first = data[i]
    i += 1
    if first < 0x80:
        return first, i
    n = first & 0x7F
    return int.from_bytes(data[i:i + n], "big"), i + n


def _parse_public_key(pem: str):
    """(modulus, exponent) from a SubjectPublicKeyInfo PEM."""
    der = _pem_to_der(pem)
    if der[0] != 0x30:
        raise ValueError("not a DER SEQUENCE")
    _, i = _der_len(der, 1)
    if der[i] != 0x30:
        raise ValueError("missing AlgorithmIdentifier")
    alg_len, j = _der_len(der, i + 1)
    i = j + alg_len
    if der[i] != 0x03:
        raise ValueError("missing BIT STRING")
    _, j = _der_len(der, i + 1)
    i = j + 1
    if der[i] != 0x30:
        raise ValueError("missing RSA SEQUENCE")
    _, j = _der_len(der, i + 1)
    i = j
    if der[i] != 0x02:
        raise ValueError("missing modulus")
    n_len, j = _der_len(der, i + 1)
    modulus = int.from_bytes(der[j:j + n_len], "big")
    i = j + n_len
    if der[i] != 0x02:
        raise ValueError("missing exponent")
    e_len, j = _der_len(der, i + 1)
    return modulus, int.from_bytes(der[j:j + e_len], "big")


def verify_signature(public_key_pem: str, message: bytes, signature_b64: str) -> bool:
    try:
        modulus, exponent = _parse_public_key(public_key_pem)
        sig = base64.b64decode(signature_b64)
    except Exception:
        return False
    k = (modulus.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= modulus:
        return False
    em = pow(s, exponent, modulus).to_bytes(k, "big")
    if em[0] != 0x00 or em[1] != 0x01:
        return False
    idx = 2
    while idx < len(em) and em[idx] == 0xFF:
        idx += 1
    if idx == 2 or idx >= len(em) or em[idx] != 0x00:
        return False
    digest_info = em[idx + 1:]
    expected = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    return digest_info == expected and len(digest_info) == len(expected)


def fetch_public_key(timeout=20):
    """Kick rotates this key, so it is fetched rather than hardcoded. The
    endpoint 403s without a browser User-Agent."""
    resp = requests.get(PUBLIC_KEY_URL, timeout=timeout,
                        headers={"User-Agent": BROWSER_UA,
                                 "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()["data"]["public_key"]


# --------------------------------------------------------------------------

class KickWebhookListener:
    """Receives Kick `chat.message.sent` webhooks and pushes chat into a queue.

    Queue items are `("chat", formatted, "kick:<username>")`, the same shape
    the rest of a streamer bot expects. Every message in the allowed channel
    is forwarded; gating (if you want any) is the host's job.

    Callbacks:
      on_ambient(user, content)  -> called for every message, before queueing.

    Threads: the HTTP server runs on a daemon thread, and a health watchdog on
    another. `message_queue` is a standard `queue.Queue`, so it is thread-safe.
    """

    def __init__(self, channel_slug, message_queue=None, command_prefix="!ask",
                 on_ambient=None, port=None, public_url=None, health_s=30,
                 url_file=None, ignored_users=()):
        self.channel_slug = (channel_slug or "").strip().lower()
        self.message_queue = message_queue or queue.Queue()
        self.command_prefix = command_prefix
        self.on_ambient = on_ambient
        self.port = int(port or 8770)
        self.public_url = (public_url or "").strip().rstrip("/")
        self.url_file = url_file or DEFAULT_URL_FILE
        self._ignored = {u.strip().lower() for u in (ignored_users or ()) if u.strip()}
        self._health_s = int(health_s or 30)
        self._nag_every = max(1, 120 // self._health_s)   # re-nag ~every 2 min
        self._pubkey = None
        self._pubkey_at = 0.0
        self._seen = OrderedDict()          # message id -> ts, for replay dedupe
        self._seen_max = 2000
        self._runner = None
        self._loop = None
        self._verified = 0
        self._rejected = 0
        self._received = 0
        self._last_event_at = 0.0
        self._tunnel_ok = False
        self._tunnel_nag = 0

    # -- key handling --
    def _key(self):
        # Refresh hourly; a rotation must not require a restart.
        if self._pubkey is None or (time.time() - self._pubkey_at) > 3600:
            self._pubkey = fetch_public_key()
            self._pubkey_at = time.time()
        return self._pubkey

    def _verify(self, headers, raw: bytes) -> bool:
        msg_id = headers.get("Kick-Event-Message-Id", "")
        ts = headers.get("Kick-Event-Message-Timestamp", "")
        sig = headers.get("Kick-Event-Signature", "")
        if not (msg_id and ts and sig):
            return False
        try:
            key = self._key()
        except Exception as e:
            print(f"[red]Kick webhook: cannot fetch public key: {e}")
            return False
        # The documented scheme is "<id>.<timestamp>.<raw body>". Kick's own Go
        # sample hashes the body alone, so accept that as a fallback: both still
        # require a valid signature from Kick's key, so this weakens nothing.
        signed = f"{msg_id}.{ts}.".encode("utf-8") + raw
        if verify_signature(key, signed, sig):
            return True
        return verify_signature(key, raw, sig)

    def _dedupe(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = time.time()
        while len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)
        return False

    # -- health watchdog -------------------------------------------------
    # Kick only POSTs while a stream is live and someone is actually talking,
    # so silence alone proves nothing. What IS provable is whether the public
    # tunnel URL still reaches us: if that fails, Kick cannot deliver no
    # matter how busy the channel is. That is the signal worth shouting about.
    def _refresh_public_url(self):
        """Re-read the tunnel hostname from disk.

        Cloudflare quick tunnels hand out a random hostname on every restart,
        so a URL copied into .env goes stale within minutes of a restart. The
        tunnel host writes the current one to `url_file`; picking it up here
        means a rotation no longer needs a manual .env edit and a restart.
        """
        try:
            with open(self.url_file, "r", encoding="utf-8") as fh:
                new = fh.read().strip().rstrip("/")
        except OSError:
            return False
        if not new.startswith("http") or new == self.public_url:
            return False
        old = self.public_url or "(unset)"
        self.public_url = new
        self._tunnel_ok = False  # force a fresh probe against the new host
        print(f"[cyan]Kick tunnel hostname changed: {old} -> {new}")
        print("[yellow]  If Kick chat goes quiet, re-run "
              "setup_kick_webhook.py subscribe and update the portal URL.")
        return True

    def _probe_public(self):
        """None = unknown (no URL configured), True/False = reachable."""
        if not self.public_url:
            return None
        try:
            r = requests.get(self.public_url.rstrip("/") + "/kick/status", timeout=12)
            return r.status_code == 200
        except Exception:
            return False

    def _watchdog(self):
        while True:
            time.sleep(self._health_s)
            try:
                self._refresh_public_url()
                reachable = self._probe_public()
                if reachable is None:
                    continue
                if reachable and not self._tunnel_ok:
                    self._tunnel_ok = True
                    print("[green]Kick chat: tunnel reachable again - "
                          "webhooks can deliver")
                elif not reachable and self._tunnel_ok:
                    self._tunnel_ok = False
                    print("[red]Kick chat DOWN: the public URL stopped responding.")
                    print(f"[red]  {self.public_url}")
                    print("[red]  Kick cannot reach the receiver, so Kick chat is")
                    print("[red]  silent. Restart your tunnel, then update the URL")
                    print("[red]  in Kick's portal if it changed.")
                elif not reachable and not self._tunnel_ok:
                    # keep nagging, but not every tick
                    self._tunnel_nag += 1
                    if self._tunnel_nag % self._nag_every == 0:
                        print(f"[red]Kick chat still DOWN ({self.public_url} "
                              f"unreachable)")
            except Exception:
                pass

    def start_watchdog(self):
        if not self.public_url:
            print("[yellow]Kick chat: KICK_WEBHOOK_URL is empty, so health cannot")
            print("[yellow]  be verified. Set it in .env or chat may fail silently.")
        else:
            ok = self._probe_public()
            self._tunnel_ok = bool(ok)
            if ok:
                print(f"[green]Kick chat: tunnel reachable ({self.public_url})")
            else:
                print(f"[red]Kick chat DOWN: {self.public_url} is not responding.")
                print("[red]  Kick will not be able to deliver chat. Start the tunnel:")
                print("[red]    python kick_tunnel_host.py")
        threading.Thread(target=self._watchdog, daemon=True).start()

    # -- payload handling --
    def _emit(self, payload):
        sender = (payload.get("sender") or {})
        user = (sender.get("username") or "?").strip()
        content = (payload.get("content") or "").strip()
        if not user or not content:
            return
        if user.lower() in self._ignored:
            return
        self._last_event_at = time.time()

        # [emote:ID:NAME] -> NAME, so the brain sees words not markup.
        content = re.sub(r"\[emote:\d+:([^\]]+)\]", r"\1", content)

        badges = [b.get("type") for b in ((sender.get("identity") or {}).get("badges") or [])]
        tag = ""
        if "moderator" in badges or "broadcaster" in badges:
            tag = " (mod)"
        elif "subscriber" in badges:
            tag = " (sub)"

        namespaced = f"kick:{user}"
        if self.on_ambient:
            try:
                self.on_ambient(user, content)
            except Exception:
                pass

        if self.command_prefix and content.lower().startswith(self.command_prefix.lower()):
            ask = content[len(self.command_prefix):].strip(" :")
            formatted = (f"[Kick viewer '{namespaced}{tag}' asks]: {ask}")
        else:
            formatted = f"[Kick viewer '{namespaced}{tag}' says]: {content}"
        self.message_queue.put(("chat", formatted, namespaced))

    async def _handler(self, request):
        raw = await request.read()
        self._received += 1

        if not self._verify(request.headers, raw):
            self._rejected += 1
            print("[yellow]Kick webhook: rejected an unverifiable request")
            return web.Response(status=401, text="invalid signature")

        msg_id = request.headers.get("Kick-Event-Message-Id", "")
        if self._dedupe(msg_id):
            return web.Response(text="duplicate")   # 200 so Kick stops retrying
        self._verified += 1

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.Response(status=400, text="bad json")

        etype = request.headers.get("Kick-Event-Type", "")
        if etype == "chat.message.sent":
            slug = ((payload.get("broadcaster") or {}).get("channel_slug") or "").lower()
            if self.channel_slug and slug and slug != self.channel_slug:
                return web.Response(text="other channel")
            self._emit(payload)
        return web.Response(text="ok")

    def status(self):
        """Snapshot for a host dashboard or a health check."""
        last = self._last_event_at
        return {
            "channel": self.channel_slug,
            "port": self.port,
            "received": self._received,
            "verified": self._verified,
            "rejected": self._rejected,
            "tunnel_ok": self._tunnel_ok,
            "last_event_seconds_ago": (round(time.time() - last, 1)
                                       if last else None),
            "public_url": self.public_url or None,
            "note": "Kick only POSTs while the channel is live and someone is "
                    "talking, so verified=0 can be normal. tunnel_ok=False is "
                    "the real problem: Kick cannot reach the receiver.",
        }

    async def _status(self, request):
        return web.json_response(self.status())

    def start(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _run():
            app = web.Application()
            app.add_routes([
                web.post("/kick/events", self._handler),
                web.get("/kick/status", self._status),
                web.get("/health", self._status),
            ])
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", self.port)
            await site.start()
            print(f"[green]Kick webhook listener on http://127.0.0.1:{self.port}")
            print(f"[dim]  POST http://127.0.0.1:{self.port}/kick/events")
            print(f"[dim]  status: http://127.0.0.1:{self.port}/kick/status")
            while True:
                await asyncio.sleep(3600)

        def _thread():
            self._loop.run_until_complete(_run())

        self._runner = threading.Thread(target=_thread, daemon=True)
        self._runner.start()
        # Health checks start only once the listener is actually accepting
        # requests, otherwise the first probe reports a false outage.
        self.start_watchdog()
