"""
Standalone demo for the Kick integration.

This is a MINIMAL harness that wires the real receiver and sender
(input/kick_webhook.py + input/kick_sender.py) to your own bot:

    1. load .env (config.py)
    2. create KickWebhookListener with a queue
    3. start it (Kick POSTs to your tunnel -> this process)
    4. drain the queue in your own loop
    5. optionally reply with KickSender.send()

Replace handle_message() with a call into YOUR bot's brain and you are done.

Radeon AI Co-Host: with KICK_MAILBOX=1 (the default), every "!ask" message
is dropped into the shared mailbox folder (see kick_mailbox.py) and the
Co-Host's replies are posted back to Kick chat. Nothing else to wire up.

Run:  python demo_main.py
"""
import queue
import sys
import threading
import time
import uuid

import config
from input.kick_sender import KickSender
from input.kick_webhook import KickWebhookListener
from kick_mailbox import Mailbox

mailbox = None      # set in __main__ when KICK_MAILBOX=1


def make_sender():
    if not (config.KICK_CLIENT_ID and config.KICK_CLIENT_SECRET):
        print("[yellow]Kick text replies disabled: KICK_CLIENT_ID / "
              "KICK_CLIENT_SECRET not set.")
        return None
    sender = KickSender(
        client_id=config.KICK_CLIENT_ID,
        client_secret=config.KICK_CLIENT_SECRET,
        tokens_file=config.KICK_TOKENS_FILE,
        min_interval_s=config.KICK_SEND_INTERVAL_S,
        target_slug=config.KICK_CHANNEL,
    )
    if sender.enabled:
        sender.refresh_now()
    return sender


# --------------------------------------------------------------------------
# Your bot's brain. Replace this with whatever YOU want to happen when a
# Kick message arrives.
#   user      : "kick:<name>" (keeps memory namespaced per platform)
#   formatted : a ready-to-log line like
#               "[Kick viewer 'kick:<name>' says]: your text"
#               "[Kick viewer 'kick:<name>' asks]: your text"   (prefix used)
# --------------------------------------------------------------------------
def handle_message(user, formatted):
    print(f"[magenta]GOT -> {formatted}")
    display = user.split(":", 1)[-1]
    line = formatted.split("]: ", 1)[-1] if "]: " in formatted else formatted
    low = line.strip().lower()

    # ---- built-in demo commands (handle these, don't forward to your bot) ----
    if low == "!ping":
        reply("pong")
        return
    if low == "!help":
        reply("Chat: !ask <question>. Demo replies to '!ask' and '!ping' only.")
        return
    # ---------------------------------------------------------------------

    # ---- Radeon AI Co-Host: hand "!ask" messages to it via the mailbox ----
    if mailbox is not None:
        if " asks]" in formatted:
            mailbox.put_inbox(display, line, uuid.uuid4().hex)
            print("[cyan]  -> sent to the Co-Host")
        else:
            print("[dim]  (not an ask - not sent to the Co-Host)")
        return

    # ---- REPLACE FROM HERE WITH YOUR OWN LOGIC ----
    # The receiver forwards EVERY message, so decide here what deserves a
    # reply. The demo answers only messages sent with the !ask prefix.
    if " asks]" in formatted:
        reply(f"Hi {display}! You asked: {line}")
    else:
        print("[dim]  (not an ask - no reply)")
    # ---- REPLACE UP TO HERE ----


def reply(text):
    if sender is None:
        print(f"[green]WOULD REPLY -> {text}")
        print("[dim]  (set KICK_CLIENT_ID/SECRET and run setup_kick_auth.py "
              "to post text)")
        return
    if not config.KICK_TEXT_REPLIES:
        print(f"[green]WOULD REPLY -> {text}")
        print("[dim]  (KICK_TEXT_REPLIES=0; set it to 1 in .env to post)")
        return
    n = sender.send(text)
    if n:
        print(f"[green]REPLIED via Kick ({n} chunk(s)) -> {text}")
    else:
        print("[red]Kick reply failed (see logs above)")


def pump_inbox():
    while True:
        _kind, formatted, user = inbox.get()
        try:
            handle_message(user, formatted)
        except Exception as e:
            print(f"[red]handle_message error: {type(e).__name__}: {e}")


def post_cohost_replies_once():
    """Post every reply the Co-Host has left in the mailbox outbox."""
    for item in mailbox.take_outbox():
        text = (item.get("reply") or "").strip()
        if text:
            reply(text)


def pump_outbox(interval_s=1.0):
    while True:
        time.sleep(interval_s)
        try:
            post_cohost_replies_once()
        except Exception as e:
            print(f"[red]Co-Host reply error (will retry): {type(e).__name__}: {e}")


if __name__ == "__main__":
    if not config.KICK_ENABLED:
        print("[yellow]KICK_ENABLED=0 in .env - not starting.")
        sys.exit(0)
    if not config.KICK_CHANNEL:
        print("[red]No KICK_CHANNEL set. Copy .env.example to .env and add your")
        print("[red]channel slug (the part after kick.com/).")
        sys.exit(1)

    inbox = queue.Queue()
    ignored = set(config.KICK_IGNORED_USERS)
    if config.KICK_BOT_ACCOUNT:
        ignored.add(config.KICK_BOT_ACCOUNT)

    listener = KickWebhookListener(
        channel_slug=config.KICK_CHANNEL,
        message_queue=inbox,
        command_prefix=config.KICK_COMMAND_PREFIX,
        # on_ambient=lambda user, msg: ...,  # optional; see INTEGRATION.md
        port=config.KICK_WEBHOOK_PORT,
        public_url=config.KICK_WEBHOOK_URL,
        health_s=config.KICK_WEBHOOK_HEALTH_S,
        url_file=config.KICK_TUNNEL_URL_FILE,
        ignored_users=ignored,
    )
    sender = make_sender()

    if config.KICK_MAILBOX:
        mailbox = Mailbox(config.KICK_MAILBOX_DIR)
        mailbox.ensure()
        print(f"[cyan]Co-Host mailbox: {mailbox.folder}")
        threading.Thread(target=pump_outbox, daemon=True).start()

    listener.start()
    threading.Thread(target=pump_inbox, daemon=True).start()

    print("[cyan]Kick bridge running. Press Ctrl+C to stop.")
    print(f"[cyan]Local status: http://127.0.0.1:{config.KICK_WEBHOOK_PORT}/kick/status")
    print("[cyan]Kick must POST to your public https URL + /kick/events.")
    if not config.KICK_WEBHOOK_URL:
        print("[yellow]KICK_WEBHOOK_URL is empty. Start a tunnel:")
        print(f"[yellow]  python kick_tunnel_host.py   (port "
              f"{config.KICK_WEBHOOK_PORT})")
        print("[yellow]Then put the printed URL in .env and run:")
        print("[yellow]  python setup_kick_webhook.py subscribe")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[red]Stopped.")
