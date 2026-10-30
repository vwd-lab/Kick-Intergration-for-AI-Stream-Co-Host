"""One-time Kick OAuth setup for the bot's Kick account.

Run ONCE: python setup_kick_auth.py
1. Opens your browser to Kick's authorization page (approve as the BOT account!)
2. Catches the redirect on http://localhost:4519/callback
3. Exchanges the code for access + refresh tokens
4. Verifies identity and saves the tokens file next to this script

Requires KICK_CLIENT_ID / KICK_CLIENT_SECRET in .env.

The redirect URI must be registered on your Kick app:
    http://localhost:4519/callback
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import config  # noqa: E402

REDIRECT_HOST = "localhost"
REDIRECT_PORT = 4519
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
SCOPES = "user:read chat:write events:subscribe"
AUTH_URL = "https://id.kick.com/oauth/authorize"
TOKEN_URL = "https://id.kick.com/oauth/token"
USERS_URL = "https://api.kick.com/public/v1/users"

captured = {}


def make_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        captured["code"] = (qs.get("code") or [""])[0]
        captured["state"] = (qs.get("state") or [""])[0]
        captured["error"] = (qs.get("error") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = bool(captured["code"]) and not captured["error"]
        msg = ("<h2>Authorization captured. You can close this tab "
               "and return to the terminal.</h2>" if ok else
               f"<h2>Authorization FAILED: {captured['error']}</h2>")
        self.wfile.write(msg.encode())

    def log_message(self, *args):
        pass


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def post_form(url, fields):
    resp = requests.post(url, data=fields, headers={
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded",
    }, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}: {resp.text[:500]}")
    return resp.json()


def get_json(url, bearer):
    resp = requests.get(url, headers={
        "User-Agent": BROWSER_UA,
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
    }, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}: {resp.text[:500]}")
    return resp.json()


def main():
    if not config.KICK_CLIENT_ID or not config.KICK_CLIENT_SECRET:
        sys.exit("KICK_CLIENT_ID / KICK_CLIENT_SECRET missing from .env")

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code",
        "client_id": config.KICK_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("=" * 62)
    print("KICK OAUTH SETUP FOR YOUR BOT ACCOUNT")
    print("=" * 62)
    print("1. A browser window is opening. Log in as YOUR BOT ACCOUNT,")
    print("   NOT as your main/streamer account, and approve the scopes.")
    print(f"2. This script waits for the redirect on {REDIRECT_URI}")
    webbrowser.open(url)
    print("\nWaiting for authorization callback...")

    deadline = time.time() + 300
    while "code" not in captured and time.time() < deadline:
        time.sleep(0.25)
    server.shutdown()

    if "code" not in captured:
        sys.exit("Timed out waiting for authorization (5 min). Run again.")
    if captured.get("error"):
        sys.exit(f"Authorization failed: {captured['error']}")
    if captured.get("state") != state:
        sys.exit("State mismatch - possible tampering. Run again.")

    print("[OK] Authorization code received. Exchanging for tokens...")
    token_resp = post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": config.KICK_CLIENT_ID,
        "client_secret": config.KICK_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": captured["code"],
        "code_verifier": verifier,
    })
    access = token_resp.get("access_token")
    refresh = token_resp.get("refresh_token")
    expires_in = int(token_resp.get("expires_in") or 0)
    granted_scope = token_resp.get("scope", "")
    if not access or not refresh:
        sys.exit(f"Token exchange failed: {json.dumps(token_resp)[:300]}")

    whoami = "?"
    try:
        users = get_json(USERS_URL, access)
        entries = users.get("data") or []
        if entries:
            # GET /public/v1/users returns "name", not "username" (verified
            # against the live API). Reading only "username" silently produced
            # "?", which also disabled the wrong-account check below.
            entry = entries[0]
            whoami = (entry.get("name") or entry.get("username")
                      or entry.get("display_name") or "?")
    except Exception as e:
        print(f"[warn] Could not verify identity: {e}")

    tokens = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + max(0, expires_in),
        "expires_in": expires_in,
        "scope": granted_scope,
        "authorized_as": whoami,
        "created_at": time.time(),
    }
    path = config.KICK_TOKENS_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    print(f"\n[OK] Authorized as: {whoami}")
    print(f"[OK] Scopes granted : {granted_scope or '(unspecified)'}")
    print(f"[OK] Tokens saved to: {path}")
    if config.KICK_BOT_ACCOUNT and whoami.lower() != config.KICK_BOT_ACCOUNT:
        print(f"\n[WARN] Expected '{config.KICK_BOT_ACCOUNT}' but got '{whoami}'.")
        print("       If this is your MAIN account, revoke the token in your")
        print("       Kick dashboard and re-run while logged in as the bot.")
    else:
        print("\nAll set. Set KICK_TEXT_REPLIES=1 in .env to let the bot post.")


if __name__ == "__main__":
    main()
