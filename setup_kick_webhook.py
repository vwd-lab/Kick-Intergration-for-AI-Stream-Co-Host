"""Manage Kick's chat.message.sent webhook subscription.

One command handles everything that used to be manual:

  * uses an APP access token (client_credentials), not the user token.
    With a user token Kick infers the broadcaster from whoever authorised, so
    you end up subscribed to the BOT's chatroom instead of your channel.
  * resolves the channel slug to the broadcaster_user_id the API wants.
  * lists, creates, prunes and removes subscriptions.

Usage
  setup_kick_webhook.py            # show current state
  setup_kick_webhook.py subscribe  # (re)create for this channel
  setup_kick_webhook.py prune      # drop subscriptions for other broadcasters
  setup_kick_webhook.py delete     # remove every chat.message.sent subscription
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import config
import requests
from rich import print

TOKEN_URL = "https://id.kick.com/oauth/token"
API = "https://api.kick.com/public/v1/events/subscriptions"
SLUG_API = "https://kick.com/api/v2/channels/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def app_token():
    """App access token: long-lived (60 days) and able to target any channel."""
    r = requests.post(TOKEN_URL, timeout=30, headers={"User-Agent": UA}, data={
        "grant_type": "client_credentials",
        "client_id": config.KICK_CLIENT_ID,
        "client_secret": config.KICK_CLIENT_SECRET,
    })
    r.raise_for_status()
    return r.json()["access_token"]


def _headers():
    return {"Authorization": f"Bearer {app_token()}", "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": UA}


def broadcaster_id(slug=None):
    """The broadcaster's USER id for a channel slug.

    The v2 endpoint returns two ids and they are not interchangeable:
      id      -> the channel record
      user_id -> the human/bot account that owns it
    Kick's events API wants the user_id in broadcaster_user_id. Reading "id"
    creates a subscription for an id that is not a broadcaster, which accepts
    cleanly and then never receives anything - a silent failure.
    """
    slug = (slug or config.KICK_CHANNEL or "").strip().lower()
    if not slug:
        sys.exit("KICK_CHANNEL is not set in .env")
    r = requests.get(SLUG_API + slug, timeout=25,
                     headers={"User-Agent": UA, "Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    uid = int(data.get("user_id") or 0)
    cid = int(data.get("id") or 0)
    if not uid:
        if not cid:
            sys.exit(f"Could not resolve channel '{slug}'")
        print(f"[yellow]  v2 returned no user_id for '{slug}'; "
              f"falling back to channel id {cid}")
        return cid
    if cid and uid != cid:
        print(f"[dim]  {slug}: channel id {cid}, broadcaster user_id {uid} "
              f"(using user_id)")
    return uid


def _subs():
    r = requests.get(API, headers=_headers(), timeout=25)
    r.raise_for_status()
    return r.json().get("data") or []


def show():
    want = broadcaster_id()
    print(f"  channel            : {config.KICK_CHANNEL}")
    print(f"  broadcaster_user_id: {want}")
    print(f"  public url         : {live_url() or '(not known - start the tunnel first)'}")
    subs = _subs()
    if not subs:
        print("   (no subscriptions)")
        return
    for s in subs:
        bid = s.get("broadcaster_user_id")
        mark = "OK  " if bid == want else "OTHER"
        print(f"   {mark} {s.get('id')}  {s.get('event')} v{s.get('version')}"
              f"  broadcaster={bid}")


def subscribe():
    want = broadcaster_id()
    # Replace rather than pile up duplicates.
    for s in _subs():
        if s.get("broadcaster_user_id") == want and s.get("event") == "chat.message.sent":
            r = requests.delete(API, headers=_headers(), timeout=25,
                                params={"id": s.get("id")})
            print(f"  removed existing {s.get('id')} -> HTTP {r.status_code}")
    r = requests.post(API, headers=_headers(), timeout=30, json={
        "broadcaster_user_id": want,
        "events": [{"name": "chat.message.sent", "version": 1}],
        "method": "webhook",
    })
    print(f"  POST subscription -> HTTP {r.status_code}")
    try:
        for s in (r.json().get("data") or []):
            print(f"   id={s.get('subscription_id')}  {s.get('name')} v{s.get('version')}"
                  f"  error={s.get('error')}")
    except Exception:
        print("  ", r.text[:300])
    if r.status_code >= 400:
        print("\n  400 'webhook not enabled for app' means webhooks are still off")
        print("  in Kick's developer portal for this app.")


def prune():
    want = broadcaster_id()
    n = 0
    for s in _subs():
        if s.get("broadcaster_user_id") != want:
            r = requests.delete(API, headers=_headers(), timeout=25,
                                params={"id": s.get("id")})
            print(f"  removed {s.get('id')} (broadcaster={s.get('broadcaster_user_id')})"
                  f" -> HTTP {r.status_code}")
            n += 1
    print(f"  pruned {n} subscription(s)")


def delete_all():
    n = 0
    for s in _subs():
        r = requests.delete(API, headers=_headers(), timeout=25,
                            params={"id": s.get("id")})
        print(f"  removed {s.get('id')} -> HTTP {r.status_code}")
        n += 1
    print(f"  removed {n} subscription(s)")


def live_url():
    """Prefer the quick-tunnel hostname the running tunnel actually published.

    KICK_WEBHOOK_URL in .env goes stale the moment cloudflared restarts, and a
    subscribe built from it silently points Kick at a dead address. The tunnel
    host writes the live one to KICK_TUNNEL_URL_FILE.
    """
    try:
        with open(config.KICK_TUNNEL_URL_FILE, "r", encoding="utf-8") as fh:
            txt = fh.read().strip().rstrip("/")
        if txt.startswith("http"):
            return txt
    except OSError:
        pass
    return (config.KICK_WEBHOOK_URL or "").strip().rstrip("/")


def main():
    print("=" * 64)
    print("KICK CHAT WEBHOOK")
    print("=" * 64)
    if not (config.KICK_CLIENT_ID and config.KICK_CLIENT_SECRET):
        sys.exit("KICK_CLIENT_ID / KICK_CLIENT_SECRET missing from .env")
    if not config.KICK_CHANNEL:
        sys.exit("KICK_CHANNEL missing from .env")
    if not live_url():
        print("  KICK_WEBHOOK_URL is empty in .env. The URL is set in Kick's")
        print("  developer portal; this is only used for the reminder below.")
        print()
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "show").lower()
    fn = {"show": show, "subscribe": subscribe,
          "prune": prune, "delete": delete_all}.get(cmd)
    if not fn:
        print(f"  usage: setup_kick_webhook.py [show|subscribe|prune|delete]  (got {cmd!r})")
        return
    fn()
    if cmd == "subscribe":
        print("\n  Confirm Kick's portal posts to:")
        print(f"     {live_url()}/kick/events")


if __name__ == "__main__":
    main()
