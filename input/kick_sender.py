"""Posts chat messages to Kick through the official public API.

Tokens come from a JSON file written by `setup_kick_auth.py` and are refreshed
transparently. Every failure path degrades to "don't send, log, stay alive" -
a Kick outage can never take the host bot down.

Chat sends use type "user": Kick's public API 500s on type "bot" unless a bot
is created/approved on the app, but a "user"-typed token from the bot account
can post to any channel via broadcaster_user_id.

Standalone: no config and no environment variables are read. Pass credentials,
tokens path and target channel to the constructor.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse

import requests
from rich import print


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Kick access tokens live ~2 hours. Hold back a proportional slice of that life
# so a send which starts just inside the window cannot expire mid-flight; a flat
# 60s cushion is thin against a 7200s token. The floor covers absurdly short
# lifetimes so the margin can never reach the full token life.
EXPIRY_SAFETY_RATIO = 0.10
MIN_EXPIRY_SAFETY_SECONDS = 1.0
# A token whose *usable* window is shorter than this cannot be worked with:
# every send would find it expired and re-refresh, which is exactly the retry
# loop we are trying to avoid.
MIN_USABLE_SECONDS = 30.0


def _default_tokens_file():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "kick_tokens.json")


class KickSender:
    """Posts chat messages to Kick as the authorized bot account.

    Tokens are refreshed transparently. Construction never raises: missing
    credentials or a missing tokens file simply leave `enabled` False.
    """

    TOKEN_URL = "https://id.kick.com/oauth/token"
    CHAT_URL = "https://api.kick.com/public/v1/chat"
    USERS_URL = "https://api.kick.com/public/v1/users"
    CHANNEL_LOOKUP_URL = "https://kick.com/api/v2/channels/"

    def __init__(self, client_id=None, client_secret=None, tokens_file=None,
                 min_interval_s=2.0, target_slug="", broadcaster_user_id=None,
                 transport=None):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.tokens_file = tokens_file or _default_tokens_file()
        self.min_interval_s = 2.0 if min_interval_s is None else float(min_interval_s)
        self._transport = transport or self._http_post_json
        self._lock = threading.Lock()
        self._access = None
        self._refresh = None
        self._expires_at = 0.0
        self._lifetime = 0.0
        self._last_send = 0.0
        self._target_slug = (target_slug or "").strip().lower()
        self._broadcaster_user_id = broadcaster_user_id
        self._scope = ""
        self._who = "?"
        self._created = 0
        self.enabled = False
        if not (self.client_id and self.client_secret):
            print("[yellow]Kick sender disabled: KICK_CLIENT_ID / "
                  "KICK_CLIENT_SECRET not set")
            return
        self._load_tokens()
        self._load_broadcaster()

    # ---- token management ----

    def _load_tokens(self):
        try:
            with open(self.tokens_file, "r", encoding="utf-8") as f:
                t = json.load(f)
            self._access = t.get("access_token")
            self._refresh = t.get("refresh_token")
            self._expires_at = float(t.get("expires_at") or 0)
            self._lifetime = float(t.get("expires_in") or 0)
            # Preserve metadata across refreshes; otherwise every save would
            # overwrite the identity/scope with blanks.
            self._scope = t.get("scope") or ""
            self._who = t.get("authorized_as") or "?"
            self._created = t.get("created_at") or 0
            self.enabled = bool(self._access and self._refresh)
            if not self.enabled:
                print("[yellow]Kick sender disabled: tokens file has no token pair")
                return
            # Kick access tokens are good for 2 hours only. Say plainly whether
            # the one on disk is alive or is about to be swapped, instead of
            # looking "ready" and then failing on the first send.
            left = self._expires_at - time.time()
            if left > 60:
                print(f"[green]Kick sender ready (authorized as "
                      f"{self._who}); access token valid for "
                      f"{left/60:.0f} more min")
            else:
                print(f"[yellow]Kick sender: stored access token expired "
                      f"{abs(left)/3600:.1f} h ago - will refresh before use")
        except FileNotFoundError:
            print("[yellow]Kick sender disabled: no tokens file - run setup_kick_auth.py")
        except Exception as e:
            print(f"[red]Kick sender disabled: bad tokens file: {e}")

    @classmethod
    def _safety_margin(cls, expires_in):
        return max(MIN_EXPIRY_SAFETY_SECONDS, expires_in * EXPIRY_SAFETY_RATIO)

    def _do_refresh(self, reason):
        """Single refresh path. Caller must hold self._lock. Never raises.

        The deadline is anchored at the moment the request *starts*, not when it
        returns. Anchoring on the response would hand the token the full
        round-trip latency as extra life, which is how a token silently expires
        mid-send.
        """
        if not self._refresh:
            print("[yellow]Kick: no refresh_token available - run "
                  "setup_kick_auth.py to re-authorize")
            self.enabled = False
            return None
        try:
            started_at = time.time()  # anchor BEFORE the call
            resp = self._transport(self.TOKEN_URL, {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._refresh,
            })
            access = resp.get("access_token")
            refresh = resp.get("refresh_token")
            expires_in = int(resp.get("expires_in") or 0)
            if not access or not refresh:
                raise ValueError(f"bad refresh response: {json.dumps(resp)[:200]}")
            if expires_in <= 0:
                raise ValueError("refresh response has no usable expires_in")
            margin = self._safety_margin(expires_in)
            # Fail closed rather than loop on a token we cannot actually use.
            if expires_in - margin < MIN_USABLE_SECONDS:
                raise ValueError(
                    f"usable window {expires_in - margin:.0f}s is below the "
                    f"{MIN_USABLE_SECONDS:.0f}s minimum")
            self._access = access
            self._refresh = refresh
            self._expires_at = started_at + expires_in
            self._lifetime = float(expires_in)
            self._save_tokens()
            self.enabled = True
            print(f"[green]Kick: access token refreshed ({reason}), good for "
                  f"{expires_in/60:.0f} min, {margin/60:.0f} min reserved")
            return self._access
        except Exception as e:
            self.enabled = False
            print(f"[red]Kick: token refresh FAILED ({reason}) - "
                  f"{type(e).__name__}: {str(e)[:200]}")
            print("[red]Kick: chat sending is now OFF. Re-run "
                  "setup_kick_auth.py (approve as the bot account) to fix.")
            return None

    def refresh_now(self):
        """Refresh the access token at startup rather than on first send.

        Kick access tokens live only 2 hours, so the file on disk is stale
        whenever the bot has been off for any length of time. Doing it here
        keeps the first outgoing message off the refresh round-trip, and makes
        a dead refresh_token obvious at boot instead of mid-stream.

        Returns True when a usable access token is held. Never raises.
        """
        with self._lock:
            return self._do_refresh("startup") is not None

    def _load_broadcaster(self):
        """Resolve the target channel slug to its user_id for type:"user" sends."""
        if self._broadcaster_user_id or not self._target_slug:
            return
        try:
            url = self.CHANNEL_LOOKUP_URL + urllib.parse.quote(self._target_slug)
            resp = requests.get(url, headers={"Accept": "application/json",
                                              "User-Agent": BROWSER_UA}, timeout=20)
            resp.raise_for_status()
            self._broadcaster_user_id = int(resp.json().get("user_id") or 0)
            if self._broadcaster_user_id:
                print(f"[green]Kick sender target: {self._target_slug} "
                      f"(user_id {self._broadcaster_user_id})")
            else:
                print(f"[red]Kick sender: no user_id for channels/{self._target_slug}")
        except Exception as e:
            print(f"[red]Kick sender: could not resolve {self._target_slug}: "
                  f"{type(e).__name__}: {str(e)[:120]}")

    def _save_tokens(self):
        tmp = self.tokens_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "access_token": self._access,
                "refresh_token": self._refresh,
                "expires_at": self._expires_at,
                "expires_in": self._lifetime,
                "scope": self._scope,
                "authorized_as": self._who,
                "created_at": self._created,
            }, f, indent=2)
        os.replace(tmp, self.tokens_file)
        try:
            os.chmod(self.tokens_file, 0o600)
        except OSError:
            pass

    def _ensure_access(self):
        """Returns a valid access token or None. Thread-safe."""
        with self._lock:
            # Margin is derived from the token's own lifetime, not from however
            # much of it happens to be left.
            margin = self._safety_margin(self._lifetime) if self._lifetime else 60.0
            if self._access and time.time() < self._expires_at - margin:
                return self._access
            if not self._refresh:
                return None
            return self._do_refresh("on send")

    @staticmethod
    def _http_post_json(url, fields):
        data = urllib.parse.urlencode(fields).encode()
        resp = requests.post(
            url, data=data,
            headers={"User-Agent": BROWSER_UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30)
        if resp.status_code != 200:
            raise urllib.error.HTTPError(
                url, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:200]}", {}, None)
        return resp.json()

    def _post_chat_api(self, payload, bearer):
        resp = requests.post(
            self.CHAT_URL, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {bearer}",
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": BROWSER_UA},
            timeout=30)
        if resp.status_code != 200:
            raise urllib.error.HTTPError(
                self.CHAT_URL, resp.status_code,
                f"HTTP {resp.status_code}: {resp.text[:200]}", {}, None)
        return resp.json()

    # ---- public API ----

    @staticmethod
    def split_message(text, limit=500):
        """Split at <=500 chars on sentence/word boundaries."""
        text = (text or "").strip()
        if len(text) <= limit:
            return [text] if text else []
        chunks = []
        while len(text) > limit:
            cut = text.rfind(". ", 0, limit)
            if cut < limit // 2:
                cut = text.rfind(" ", 0, limit)
            if cut < 1:
                # No boundary: take a hard cut without dropping a character.
                chunks.append(text[:limit].strip())
                text = text[limit:].lstrip()
                continue
            chunks.append(text[:cut].strip())
            # step past the separator so the next chunk never starts with ". "
            if text[cut] == ".":
                text = text[cut + 2:].lstrip()
            else:
                text = text[cut + 1:].lstrip()
        if text:
            chunks.append(text)
        return [c for c in chunks if c]

    def _throttle(self):
        wait = self.min_interval_s - (time.time() - self._last_send)
        if wait > 0:
            time.sleep(wait)

    def send(self, text):
        """Post text (auto-split at 500 chars). Returns count sent."""
        if not self.enabled:
            return 0
        chunks = self.split_message(text)
        sent = 0
        for chunk in chunks:
            if self._send_one(chunk):
                sent += 1
            else:
                break  # auth/rate problem - don't hammer
        return sent

    def _send_one(self, content):
        bearer = self._ensure_access()
        if not bearer:
            return False
        if not self._broadcaster_user_id:
            print(f"[red]Kick sender: no target channel (KICK_CHANNEL={self._target_slug or '(empty)'})")
            return False
        self._throttle()
        payload = {"broadcaster_user_id": self._broadcaster_user_id,
                   "content": content[:500], "type": "user"}
        try:
            resp = self._post_chat_api(payload, bearer)
            self._last_send = time.time()
            data = resp.get("data") or {}
            if data.get("is_sent"):
                return True
            print(f"[red]Kick send refused: {json.dumps(resp)[:200]}")
            return False
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            if e.code == 429:
                print("[yellow]Kick rate limited (429) - backing off")
                self._last_send = time.time() + 25
            else:
                print(f"[red]Kick send failed ({e.code}): {body}")
            return False
        except Exception as e:
            print(f"[red]Kick send error: {type(e).__name__}: {str(e)[:200]}")
            return False
