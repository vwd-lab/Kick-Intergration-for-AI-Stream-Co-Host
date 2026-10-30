# Changelog — sharing\Kick (MIT)

Canonical working copy at `C:\Users\bbsti\AI Projects\Project AI Logan\sharing\Kick`.

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

- (none yet — log changes here as they land)
