# Kick Bridge — Integration Guide

The Kick kit is transport-only: it receives Kick chat through the official
`chat.message.sent` webhook and (optionally) posts replies back into Kick chat.
It ships no brain, persona, TTS or voice support.

Two modules, usable independently:

- `input/kick_webhook.py` — `KickWebhookListener` (chat in)
- `input/kick_sender.py` — `KickSender` (chat out)

Both import only the stdlib plus `requests`, `aiohttp` and `rich`. They read no
config and no environment variables; everything is passed to the constructor.
`config.py` and `demo_main.py` are example scaffolding only.

Contents:

- [Architecture](#architecture)
- [The public URL requirement](#the-public-url-requirement)
- [Minimal wiring](#minimal-wiring)
- [KickWebhookListener](#kickwebhooklistener)
- [Queue contract](#queue-contract)
- [KickSender](#kicksender)
- [Threading](#threading)
- [Security](#security)
- [HTTP endpoints](#http-endpoints)
- [Setup sequence](#setup-sequence)
- [Layout and imports](#layout-and-imports)
- [How this differs from the Discord kit](#how-this-differs-from-the-discord-kit)
- [Troubleshooting](#troubleshooting)

## Architecture

```
Kick chat
   |
   v  (Kick POSTs from its own servers)
public HTTPS URL  --tunnel-->  127.0.0.1:8770/kick/events
                                   |
                                   v
                        KickWebhookListener
                        (verify -> dedupe -> queue)
                                   |
                                   v
                          your queue ("chat", formatted, "kick:name")
                                   |
                                   v
                              your brain
                                   |
                                   v
                            KickSender.send()
                                   |
                                   v
                        api.kick.com/public/v1/chat -> Kick chat
```

Outbound replies are always optional. Inbound works without any Kick app
credentials; only the webhook subscription needs the app's client id/secret.

## The public URL requirement

Kick POSTs from its own servers, so `127.0.0.1` is not reachable. You need an
HTTPS URL that forwards to the receiver. The included `kick_tunnel_host.py`
uses a cloudflared quick tunnel and writes the random hostname to
`data/kick_tunnel_url.txt`; any tunnel or reverse proxy works.

Quick-tunnel hostnames change on restart. When that happens, update the URL in
Kick's developer portal and re-run `setup_kick_webhook.py subscribe`. The
receiver re-reads `url_file` at runtime, so it does not need a restart.

## Minimal wiring

```python
import queue
from input.kick_webhook import KickWebhookListener

inbox = queue.Queue()

listener = KickWebhookListener(
    channel_slug="yourchannelslug",
    message_queue=inbox,
    command_prefix="!ask",
    port=8770,
    public_url="https://your-tunnel.trycloudflare.com",
    ignored_users={"yourbotname"},
    url_file="data/kick_tunnel_url.txt",
)
listener.start()

while True:
    kind, formatted, user = inbox.get()
    # formatted: "[Kick viewer 'kick:name' says]: hello"
    # user:      "kick:name"
    ...
```

Optional outbound:

```python
from input.kick_sender import KickSender

sender = KickSender(
    client_id="...",
    client_secret="...",
    tokens_file="kick_tokens.json",   # written by setup_kick_auth.py
    target_slug="yourchannelslug",
    min_interval_s=2.0,
)
if sender.refresh_now():
    sender.send("Hello from the bot.")
```

`KickSender` self-disables (`enabled == False`) if credentials or the tokens
file are missing; construction never raises.

## KickWebhookListener

```python
KickWebhookListener(
    channel_slug,                # required; lowercase channel slug
    message_queue=None,          # queue.Queue(); created if omitted
    command_prefix="!ask",       # formats "<prefix> ..." as "asks"
    on_ambient=None,             # optional on_ambient(user, content)
    port=8770,                   # local bind port
    public_url="",               # https URL used for health probes
    health_s=30,                 # seconds between tunnel probes
    url_file=None,               # default: <project>/data/kick_tunnel_url.txt
    ignored_users=(),            # usernames to drop (case-insensitive)
)
```

| Argument | Purpose |
|---|---|
| `channel_slug` | The part after `kick.com/`. A payload for another channel is ignored. |
| `message_queue` | Standard `queue.Queue`. Items are described below. |
| `command_prefix` | Formats messages starting with it as `asks`; it does **not** filter. Set `""` to disable. |
| `on_ambient` | Called for every accepted message, before queueing. Exceptions are swallowed. |
| `port` | Local HTTP port. Kick reaches it through the tunnel. |
| `public_url` | Used only by the health watchdog. May stay empty if `url_file` supplies it. |
| `health_s` | Probe interval. Also drives the re-nag cadence. |
| `url_file` | File the tunnel host writes; re-read on every probe. |
| `ignored_users` | Drop messages from these usernames, including the bot's own if Kick echoes them. |

Public methods:

| Method | Returns | Notes |
|---|---|---|
| `start()` | — | Starts the HTTP server and watchdog on daemon threads. A second call would fight for the port. |
| `start_watchdog()` | — | Starts just the health thread (called by `start()`). |
| `status()` | `dict` | Same data as the `/kick/status` endpoint. |

There is no `stop()`: the threads are daemons and the process exit is the stop.
If you need a clean shutdown, run the receiver in its own process.

## Queue contract

Each item is a 3-tuple:

```python
("chat", "[Kick viewer 'kick:name' says]: hello", "kick:name")
("chat", "[Kick viewer 'kick:name' asks]: hello", "kick:name")
("chat", "[Kick viewer 'kick:name (mod)' says]: hello", "kick:name")
```

- `kind` is always `"chat"` today (kept for compatibility with shared
  Twitch/Kick queues).
- `formatted` is ready to log or pass to a prompt.
- `user` is namespaced `kick:<username>` so memories stay separate from
  same-named viewers elsewhere. Strip `kick:` for display.

Every message in the channel is forwarded. If you want mention/keyword gating,
do it in your own queue consumer.

## KickSender

```python
KickSender(
    client_id=None,
    client_secret=None,
    tokens_file=None,            # default: <project>/kick_tokens.json
    min_interval_s=2.0,
    target_slug="",
    broadcaster_user_id=None,
    transport=None,              # injectable POST for tests
)
```

| Member | Notes |
|---|---|
| `enabled` | True when credentials and a token pair are present. |
| `refresh_now()` | Refreshes before the first send. Returns bool. Never raises. |
| `send(text)` | Splits at 500 chars, throttles, returns the number of chunks sent. Stops at the first failure. |
| `split_message(text, limit=500)` | Static helper; splits on sentence/word boundaries and never drops characters. |

`target_slug` is resolved to `broadcaster_user_id` through Kick's public v2
API (it uses `user_id`, not the channel `id`). Pass `broadcaster_user_id`
directly to skip the lookup.

Sending uses `type: "user"`; Kick's API rejects `type: "bot"` unless a bot
account is approved on the app.

## Threading

- The HTTP server and the health watchdog each run on daemon threads.
- `message_queue` is a standard `queue.Queue`; safe across threads.
- `KickSender.send()` is thread-safe (internal lock) but **blocks** while it
  throttles and performs HTTP. Call it from a worker if your queue consumer
  must stay responsive.
- `on_ambient` runs on the HTTP thread. Keep it cheap and thread-safe.

## Security

- Every request is signature-checked with Kick's RSA public key (PKCS#1 v1.5 /
  SHA-256, pure stdlib). Unverifiable requests get HTTP 401 and are counted.
- The key is fetched live and cached for one hour, so a rotation does not
  require a restart.
- Message ids are deduped in a bounded (2000-entry) cache; replays get HTTP
  200 so Kick stops retrying.
- A payload whose `broadcaster.channel_slug` does not match `channel_slug` is
  ignored (when the slug is present).
- `setup_kick_auth.py` writes the token file with mode `0600` where the OS
  supports it. Never commit it.

## HTTP endpoints

| Path | Method | Purpose |
|---|---|---|
| `/kick/events` | POST | Kick's webhook target. |
| `/kick/status` | GET | JSON counters and tunnel health. |
| `/health` | GET | Alias for `/kick/status`. |

`status()` fields: `channel`, `port`, `received`, `verified`, `rejected`,
`tunnel_ok`, `last_event_seconds_ago`, `public_url`, `note`.

## Setup sequence

1. Create a Kick app; copy client id/secret; enable webhooks; add redirect
   `http://localhost:4519/callback`.
2. Start a tunnel to `http://127.0.0.1:8770`.
3. Put the public URL in the portal as `https://.../kick/events`.
4. Set `KICK_CHANNEL` and the credentials in `.env`.
5. Run `python setup_kick_webhook.py subscribe`.
6. Start your bot (`python demo_main.py`).
7. Optional text replies: run `python setup_kick_auth.py` **as the bot
   account**, then set `KICK_TEXT_REPLIES=1`.

## Layout and imports

```
your_project/
  input/
    kick_webhook.py
    kick_sender.py
  demo_main.py         # example only
  config.py            # example only
  setup_kick_auth.py   # optional tooling
  setup_kick_webhook.py
  kick_tunnel_host.py
```

`demo_main.py` imports `input.kick_webhook` / `input.kick_sender`. The modules
themselves have no layout assumption; adjust the two imports for your package
name.

## How this differs from the Discord kit

| Discord kit | Kick kit |
|---|---|
| Callback (`on_text`) | Queue (`message_queue`) |
| Gated (mention/keyword/prefix) | Every message forwarded |
| Voice in/out | Text only |
| Token only | Client id/secret + optional OAuth |
| No public URL | Requires a public HTTPS URL |
| One `DiscordBridge` object | Two independent modules |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `verified=0`, `tunnel_ok=false` | Tunnel down or the portal URL is stale. Restart the tunnel; update the portal; re-subscribe. |
| `verified=0`, `tunnel_ok=true` | Channel is offline/quiet, or the subscription targets the wrong broadcaster. Run `setup_kick_webhook.py show`. |
| `rejected` climbing | Requests are not validly signed (wrong URL or intercepted traffic). Check the portal URL and system clock. |
| 400 "webhook not enabled for app" | Enable webhooks for the app in Kick's portal. |
| `send()` returns 0 | Sender disabled (no credentials/tokens), expired refresh token, or no `broadcaster_user_id`. Check the log. |
| Bot replies to itself | Set `KICK_BOT_ACCOUNT` or add the bot's username to `ignored_users`. |
| Channel not resolved | `KICK_CHANNEL` must be the lowercase slug, not the display name. |
