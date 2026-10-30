# Kick AI Bridge

Framework-agnostic Kick chat bridge. Receive Kick chat messages and hand them
to **any** bot — Radeon AI Co-Host, a custom Python bot, Node bot, or anything
else that can read JSON or accept an HTTP POST.

The kit is **transport-only**: it ships no brain, persona, TTS, or voice
support. It receives Kick chat through Kick's official `chat.message.sent`
webhook and (optionally) posts replies back into Kick chat. You supply the
brain.

## Universal integration point

The webhook listener pushes every Kick message onto a standard
`queue.Queue` as:

```python
("chat", formatted, "kick:<username>")
```

- `formatted` = e.g. `"[Kick viewer 'kick:dave1' asks]: what game is this?"`
- `"kick:dave1"` namespaces memory per platform (same shape Twitch chat uses)

Your bot drains that queue and routes it through its own command/brain
pipeline exactly like Twitch chat. That is the universal plug — it works
whether your bot is Radeon AI Co-Host or a completely different framework.

## Plugging in

### Option A: Radeon AI Co-Host

Use the **file** adapter. `adapter_radeon_cohost.py` contains a code shim to
paste into `Radeon AI Co-Host/main.py`'s main loop so Kick inbox messages are
processed through `process_message()` exactly like Twitch chat (batch, OOC,
mute, lore, etc.).

### Option B: Any custom framework

Use `demo_main.py` as the template. Replace `handle_message(user, formatted)`
with a call into your own brain:

```python
def handle_message(user, formatted):
    display = user.split(":", 1)[-1]   # strip "kick:"
    text = formatted.split("]: ", 1)[-1]
    reply_text = my_bot.answer(display, text)   # YOUR framework
    reply(reply_text)                           # posts via KickSender
```

The `reply()` helper in `demo_main.py` already handles `KICK_TEXT_REPLIES=0`
(dry-run mode) and auto-splits long messages at 500 chars.

## The two bridge modes

The GUI can launch either:

| Mode | Script | Best for |
|---|---|---|
| **production** (default) | `demo_main.py` | Kick signature verification, tunnel health watchdog, token refresh. Recommended for actual streaming. |
| **simple** | `kick_bridge.py` | Single-file, no signature verification. Good for quick testing / non-Kick-verified webhooks. |

Both import only the stdlib plus `requests` (production also needs `aiohttp`).

## Setup you'll still need

These steps are required before Kick chat will flow:

### 1. Register a Kick developer app
Go to https://developers.kick.com/ and create an app. Note the **Client ID**
and **Client Secret**. Set a redirect URI for OAuth (e.g. `http://localhost`
for testing).

### 2. Authorize the bot account (one time)
Run `setup_kick_auth.py` while logged into Kick as the account that will post
replies. This writes `kick_tokens.json` with an access token + refresh token.
Kick access tokens live only ~2 hours, so `demo_main.py` refreshes
automatically at startup and before each send.

### 3. Start a public tunnel (required)
Kick POSTs webhooks from its own servers to a public HTTPS URL — `localhost`
won't work. Run:

```bat
python kick_tunnel_host.py
```

This starts cloudflared and prints a public URL like
`https://xxxx.us.trycloudflare.com`. It also writes
`data\kick_tunnel_url.txt` so the bridge can pick up hostname changes without
a restart.

### 4. Subscribe the webhook
```bat
python setup_kick_webhook.py subscribe
```
Tell Kick to POST `chat.message.sent` events to your public tunnel URL plus
`/kick/events`, e.g.:

```
https://xxxx.us.trycloudflare.com/kick/events
```

### 5. Configure `.env`
Copy `.env.example` to `.env` and fill in at minimum:

```
KICK_ENABLED=1
KICK_CHANNEL=yourchannelslug
KICK_CLIENT_ID=...
KICK_CLIENT_SECRET=...
KICK_WEBHOOK_URL=https://xxxx.us.trycloudflare.com
KICK_TEXT_REPLIES=1
KICK_COMMAND_PREFIX=!ask
```

`KICK_CHANNEL` is the part after `kick.com/`. `KICK_WEBHOOK_URL` is the
public tunnel URL (without the `/kick/events` path — the bridge adds that).

### 6. Launch

**First run (recommended):** double-click `setup_kick_gui.bat`. It prompts for your
channel slug, public tunnel URL, adapter, bridge mode, and port, writes
`config.json`, then opens `kick_gui.py` where you can start/stop the bridge.

**Subsequent runs:** double-click `start_kick_bridge.bat` (reuses `config.json`), or
run directly:

```bat
python demo_main.py
```

## Files

| File | Purpose |
|---|---|
| `demo_main.py` | Production wiring harness (receiver → your `handle_message()` → sender) |
| `kick_bridge.py` | Simple single-file bridge (no signature verification) |
| `kick_gui.py` | tkinter launcher (stdlib only) |
| `setup_kick_gui.bat` | Interactive setup: prompts for channel, URL, adapter, then launches `kick_gui.py` |
| `start_kick_bridge.bat` | Double-click launcher (uses existing `config.json`) |
| `config.json` | Your saved configuration (create from example) |
| `config.example.json` | Config template |
| `input/kick_webhook.py` | Kick webhook receiver (signature verify, dedupe, tunnel watchdog) |
| `input/kick_sender.py` | Kick chat sender (token refresh, 500-char split, rate-limit backoff) |
| `setup_kick_auth.py` | One-time OAuth to get bot tokens |
| `setup_kick_webhook.py` | Subscribe / unsubscribe the webhook |
| `kick_tunnel_host.py` | cloudflared tunnel host |
| `start_kick_tunnel.bat` | Tunnel launcher |
| `adapter_base.py` | Abstract adapter class to subclass |
| `adapter_radeon_cohost.py` | Shim for Radeon AI Co-Host |
| `adapter_http.py` | HTTP callback adapter + example Flask endpoint |
| `messages.json` | File mailbox (created at runtime, `file` adapter) |
| `kick_tokens.json` | OAuth tokens (created at runtime, git-ignored) |
| `data/kick_tunnel_url.txt` | Current tunnel hostname (created at runtime) |

## Requirements

- Python 3
- `pip install requests aiohttp`

The GUI (`kick_gui.py`) uses tkinter only, so no extra packages are needed
to launch.

## License

This kit is released under the [MIT License](LICENSE). You are free to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the software.

The one requirement is attribution: the copyright notice and license text must
be retained in all copies or substantial portions of the software, so the
original creator (the Radeon AI Co-Host authors) stays credited no matter how
far downstream the code is shared.
