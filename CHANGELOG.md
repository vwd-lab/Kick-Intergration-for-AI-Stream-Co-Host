# Changelog — sharing\Kick (MIT)

Canonical working copy at `C:\XXXXX\AI Projects\Project AI Logan\sharing\Kick`.

Framework-agnostic Kick chat bridge. Transport-only: ships no brain, persona,
TTS, or voice. Receives Kick chat through Kick's official `chat.message.sent`
webhook, pushes messages onto a `queue.Queue`, and optionally posts replies back
into Kick chat. Requires a public HTTPS tunnel (cloudflared); Kick retired the
Pusher websocket path.

This is not a git repository, so versions are tracked here by date instead of commit hash.

## 2026-11-18 — Kick fixes 01, 08, 22, 23, 27 (based on direct feedback)

- Fixed Kick "simple" mode security (Fix 01): every POST now requires a valid
  Kick RSA signature (same check as production listener in `input/kick_webhook.py`).
  Server binds to `127.0.0.1` by default instead of `0.0.0.0`. Rejects bodies
  over 64 KB (413) and requests without `Content-Length` (411). Only
  `chat.message.sent` events are handled; other events return 200 "ignored".
  Message dedupe by `Kick-Event-Message-Id` prevents replay. Public key cached
  for 1 hour, kept on refresh failure. Message ID comes from signed header
  (not `data["id"]`). Rejected requests logged sparingly. `kick_bridge.py` now
  needs `aiohttp` and `rich` (already in `requirements.txt`).
- `kick_gui.py`: hint text no longer suggests port-forwarding; simple mode
  description no longer says "no verification".
- `README.md`: mode table and dependency note updated; `kick_bridge.py` file
  description updated.
- `setup_kick_gui.bat` / `start_kick_bridge.bat`: mode description and install
  instruction updated.

- Fixed Kick mailbox (Fix 08): the single `messages.json` mailbox that both
  the bridge and the Co-Host read-and-wrote back lost messages (58% lost in
  testing) and never delivered replies to Kick chat. Replaced with a mailbox
  **folder** (`mailbox/inbox`, `mailbox/outbox`, `mailbox/bad`) where each
  message is its own small JSON file — nothing is ever read, changed and
  written back, so nothing can be lost or handled twice (300 sent, 300
  received in testing). Writes use temp-file-then-rename; reading claims a
  file by renaming it first. Bad files go to `bad/`; stale replies older than
  5 minutes are dropped; outbox capped at 200.
- `kick_mailbox.py` (new): the mailbox module, identical in both Kick kit and
  Co-Host (`core/kick_mailbox.py`).
- Production mode (`demo_main.py`) now feeds the Co-Host: `!ask` messages go
  into the mailbox and the Co-Host's replies are posted back to Kick chat
  (when `KICK_TEXT_REPLIES=1`). New settings: `KICK_MAILBOX` (default on) and
  `KICK_MAILBOX_DIR`.
- Co-Host (`sharing\Radeon AI Co-Host`): `main.py` reads Kick chat from the
  mailbox one file at a time (no more read-change-write-back), puts the AI's
  answer in the outbox so Kick viewers see replies in chat, and reports the
  mailbox folder at startup. `config.py`: `KICK_MAILBOX_DIR` replaces
  `KICK_MAILBOX_PATH`. `README.md` integration table updated.
- `adapter_base.py`, `adapter_radeon_cohost.py`, `kick_gui.py`,
  `setup_wizard.py`, `README.md`: text updated from `messages.json` to the
  mailbox folder; dangerous paste-in snippet removed from
  `adapter_radeon_cohost.py`.
- `.gitignore`: ignores `mailbox/` and the old `messages.json`.

- Fixed Kick replay protection (Fix 22): a captured Kick message with a valid
  signature can no longer be accepted again. A message's signed time must be
  within 5 minutes of now. Handled message IDs are remembered for a day on
  disk (`data/kick_seen_ids.json`, simple mode uses
  `data/kick_seen_ids_simple.json`), so a restart doesn't make an old message
  new again. With a body-only signature, the id and time come from inside the
  signed message, not from unsigned headers. Ignored resends get HTTP 200 so
  Kick stops retrying. Same protection in production mode and simple mode.
  `ReplayGuard` class shared by `input/kick_webhook.py` and `kick_bridge.py`.

- Fixed Kick key/token/send robustness (Fix 23): one short internet hiccup no
  longer switches Kick off for the rest of the stream. Receiving: the key
  already held keeps working when a refresh fails; the refresh is retried every
  minute and never stalls the receiver; a new key from Kick is picked up
  immediately. Sending: only Kick rejecting the login turns replies off; a
  network blip just pauses them and they retry by themselves. Programs sharing
  `kick_tokens.json` now cooperate via a lock file instead of knocking each
  other's token out. Replies are sent one at a time (no mixing); "too many
  requests" waits and sends the rest; "token no longer valid" refreshes once
  and retries.
- `input/kick_sender.py`: token retry delays, rate-limit retries, token file
  lock, token adoption from file, error text never contains token values.
- `input/kick_webhook.py`: public key refresh retry every 60 seconds instead of
  rejecting every message; key refresh on worker thread so it doesn't stall the
  event loop; fetches a fresh key if signature verification fails.
- `kick_bridge.py`: same key-refresh and key-rotation retry logic as production
  listener.
- `INTEGRATION.md`, `README.md`, `README.txt`: updated docs. `.gitignore`:
  ignores `kick_tokens.json.lock`.

- Fixed Kick small issues (Fix 27): long replies keep the full stop at every
  split (was lost). Simple mode now posts replies using the same sender as
  production mode (documented Kick API with automatic token refresh) instead of
  a fixed access token that expired after ~2 hours; running
  `setup_kick_auth.py` once is required for simple mode text replies. Secrets
  (`kick_tokens.json`, `config.json`, `.env`) are now readable only by the
  current Windows account (`icacls` on Windows, since `chmod 0600` does
  nothing). Token values never end up in error messages; the sign-in page
  escapes its error text; the status page only shows details on this PC.
  `.gitignore` covers `config.json` and `bridge.log`. The kit warns when
  `.env` is ignored (python-dotenv missing); the tunnel launcher uses the kit's
  venv; the README has the right redirect URI
  (`http://localhost:4519/callback`) and steps in working order.

## 2026-10-28 — Initial release

- `kick_gui.py` — tkinter setup GUI (stdlib only) with bridge-mode combobox
  (`demo_main.py` production mode, `simple` optional via `kick_bridge.py`);
  configures channel, credentials, adapter, and public URL without touching
  `config.json` by hand.
- `kick_bridge.py` — file-adapter (Radeon AI Co-Host `messages.json` mailbox)
  and HTTP-adapter bridge implementations.
- `adapter_base.py`, `adapter_http.py`, `adapter_radeon_cohost.py` — adapter
  implementations for custom and Radeon AI Co-Host integration.
- `setup_kick_gui.bat` / `start_kick_bridge.bat` — interactive setup and
  launcher.
- `config.example.json` — example configuration.
- `LICENSE` — MIT (copyright-notice retention guarantees attribution
  downstream).
- `README.md` — setup sequence, webhook/tunnel requirements, Radeon AI Co-Host
  vs custom-framework plug-in instructions.
  
