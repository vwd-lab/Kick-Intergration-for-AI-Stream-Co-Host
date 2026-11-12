# Changelog — sharing\Kick (MIT)

Canonical working copy at `C:\XXXXX\AI Projects\Project AI Logan\sharing\Kick`.

Framework-agnostic Kick chat bridge. Transport-only: ships no brain, persona,
TTS, or voice. Receives Kick chat through Kick's official `chat.message.sent`
webhook, pushes messages onto a `queue.Queue`, and optionally posts replies back
into Kick chat. Requires a public HTTPS tunnel (cloudflared); Kick retired the
Pusher websocket path.

This is not a git repository, so versions are tracked here by date instead of commit hash.

## 2026-10-28 — Initial release

- `kick_gui.py` — tkinter setup GUI (stdlib only) with bridge-mode combobox (`demo_main.py` production mode, `simple` optional via `kick_bridge.py`); configures channel, credentials, adapter, and public URL without touching `config.json` by hand.
- `kick_bridge.py` — file-adapter (Radeon AI Co-Host `messages.json` mailbox) and HTTP-adapter bridge implementations.
- `adapter_base.py`, `adapter_http.py`, `adapter_radeon_cohost.py` — adapter implementations for custom and Radeon AI Co-Host integration.
- `setup_kick_gui.bat` / `start_kick_bridge.bat` — interactive setup and launcher.
- `config.example.json` — example configuration.
- `LICENSE` — MIT (copyright-notice retention guarantees attribution downstream).
- `README.md` — setup sequence, webhook/tunnel requirements, Radeon AI Co-Host vs custom-framework plug-in instructions.

## Planned

-Fixes based on feedback:
- Fixed Kick "simple" mode security (kick_bridge.py): every POST now requires a valid Kick RSA signature (same check as production listener in input/kick_webhook.py). Server binds to 127.0.0.1 by default instead of 0.0.0.0. Rejects bodies over 64 KB (413) and requests without Content-Length (411). Only chat.message.sent events are handled; other events return 200 "ignored". Message dedupe by Kick-Event-Message-Id prevents replay. Public key cached for 1 hour, kept on refresh failure. Message ID comes from signed header (not data["id"]). Rejected requests logged sparingly. kick_bridge.py now needs aiohttp and rich (already in requirements.txt).
- kick_gui.py: hint text no longer suggests port-forwarding; simple mode description no longer says "no verification".
- README.md: mode table and dependency note updated; kick_bridge.py file description updated.
- setup_kick_gui.bat / start_kick_bridge.bat: mode description and install instruction updated.
