================================================================================
 BOT NAME KICK INTEGRATION - STANDALONE SHARE KIT
================================================================================

This folder is a bare-bones, plug-and-play version of the Kick integration used
by the Bot Name streamer bot. It gives you the REAL, battle-tested classes:

  input/kick_webhook.py   The official Kick chat.message.sent receiver
  input/kick_sender.py    Posts replies into Kick chat (optional)
  demo_main.py            Minimal harness that wires them to YOUR bot

Two directions, independent:

  IN  (always): Kick POSTs chat to a public HTTPS URL -> the receiver -> a
                queue your bot drains.
  OUT (optional): your bot posts text back into Kick chat as a bot account.

For the full contract (constructor args, queue shape, threading, security),
see INTEGRATION.md.

--------------------------------------------------------------------------------
 WHAT YOU GET
--------------------------------------------------------------------------------
  input/kick_webhook.py    The real KickWebhookListener (RSA verify, dedupe,
                           health watchdog, status endpoint)
  input/kick_sender.py     The real KickSender (OAuth refresh, throttling,
                           500-char splitting)
  demo_main.py             Minimal harness that wires it to YOUR bot
  setup_kick_auth.py       One-time OAuth for the bot account (text replies)
  setup_kick_webhook.py    List/create/prune the webhook subscription
  kick_tunnel_host.py      Optional cloudflared quick-tunnel helper
  start_kick_tunnel.bat    Windows launcher for the tunnel helper
  config.py                Trimmed config (only Kick settings)
  requirements.txt         Packages needed
  .env.example             Copy to .env and fill in your values
  COMMANDS.txt             Chat behaviour + admin scripts + endpoints
  INTEGRATION.md           Full integration contract

--------------------------------------------------------------------------------
 1) CREATE A KICK APP (5-10 minutes)
--------------------------------------------------------------------------------
  1. Log in to the Kick developer dashboard with your MAIN/streamer account.
  2. Create an app/application.
  3. Copy its CLIENT ID and CLIENT SECRET.
  4. Add this redirect URI (needed only for text replies):
         http://localhost:4519/callback
  5. Enable webhooks for the app (the subscription call below fails without it).
  6. Create a SEPARATE Kick account for the bot if you want it to post replies
     (recommended). You will log in as this account during OAuth.

  Never post from your main account: the bot would look like you, and a bad
  reply is much harder to distance yourself from.

--------------------------------------------------------------------------------
 2) GET A PUBLIC HTTPS URL
--------------------------------------------------------------------------------
Kick POSTs from its own servers, so localhost is not reachable. Use any HTTPS
tunnel that forwards to http://127.0.0.1:8770.

  Option A - included helper (cloudflared quick tunnel):
      winget install --id Cloudflare.cloudflared
      python kick_tunnel_host.py
    It prints a URL like https://random-words.trycloudflare.com and writes it
    to data/kick_tunnel_url.txt.

  Option B - bring your own:
      cloudflared tunnel --url http://127.0.0.1:8770
      (ngrok, a reverse proxy, or a real domain work too)

The receiver itself is your bot (demo_main.py or your own process). The tunnel
only forwards traffic to it, so start the bot too.

Quick-tunnel hostnames change on every restart. When that happens:
  1. update the URL in Kick's developer portal, and
  2. run  python setup_kick_webhook.py subscribe
The receiver re-reads data/kick_tunnel_url.txt automatically.

--------------------------------------------------------------------------------
 3) CONFIGURE .env
--------------------------------------------------------------------------------
  copy .env.example .env

Minimum:
  KICK_CHANNEL=yourchannelslug
  KICK_WEBHOOK_URL=https://your-public-url

Optional:
  KICK_COMMAND_PREFIX=!ask          (questions vs statements)
  KICK_WEBHOOK_PORT=8770
  KICK_IGNORED_USERS=                (comma-separated names to drop)
  KICK_TEXT_REPLIES=0                (1 = allow posting back)

Relative paths (tokens, tunnel URL) resolve against THIS folder, not the
current working directory.

--------------------------------------------------------------------------------
 4) REGISTER THE WEBHOOK SUBSCRIPTION
--------------------------------------------------------------------------------
In Kick's developer portal, set the webhook URL to:

      https://your-public-url/kick/events

Then run, from this folder:

      python setup_kick_webhook.py show
      python setup_kick_webhook.py subscribe

`show` prints the broadcaster user id and current subscriptions. `subscribe`
replaces any existing chat.message.sent subscription for this channel.

Important: the events API wants the broadcaster's USER id, not the channel id.
setup_kick_webhook.py resolves both and uses user_id. Using the channel id
subscribes successfully and then silently never delivers - the script exists
to avoid that trap.

Check delivery any time:

      http://127.0.0.1:8770/kick/status

  received / verified / rejected  request counters
  tunnel_ok                       can Kick reach your public URL?
  last_event_seconds_ago          None until the first chat arrives

Kick only POSTs while the channel is live and someone is talking, so
verified=0 is normal on a quiet/offline channel. tunnel_ok=false is the real
problem: Kick cannot reach the receiver.

--------------------------------------------------------------------------------
 5) OPTIONAL: TEXT REPLIES (bot posts into chat)
--------------------------------------------------------------------------------
  1. Put KICK_CLIENT_ID / KICK_CLIENT_SECRET in .env.
  2. Run once:

         python setup_kick_auth.py

     A browser opens. Log in as the BOT ACCOUNT (not your main account) and
     approve. Tokens are saved to kick_tokens.json and refreshed automatically.
  3. Set KICK_TEXT_REPLIES=1 in .env.
  4. Optional: set KICK_BOT_ACCOUNT=yourbotname. This sanity-checks the OAuth
     result and ignores the bot's own messages if Kick echoes them.

Tokens last ~2 hours; KickSender refreshes them before every send. If the
refresh token is revoked, sending turns itself off and logs the fix.

Without this step the receiver still works; replies just stay local.

--------------------------------------------------------------------------------
 6) RUN THE DEMO
--------------------------------------------------------------------------------
      python demo_main.py

Then in Kick chat:

      !ask hello there        -> forwarded as an "asks" message
      hello there             -> forwarded as a "says" message
      !ping                   -> demo replies "pong"

The demo answers only "asks" messages and !ping/!help. Replace
handle_message() in demo_main.py with a call into your own bot's brain.

--------------------------------------------------------------------------------
 HOW MESSAGES LOOK
--------------------------------------------------------------------------------
Queue items are 3-tuples:

      ("chat", "[Kick viewer 'kick:name' says]: hello", "kick:name")

With the prefix:

      ("chat", "[Kick viewer 'kick:name' asks]: hello", "kick:name")

Moderators/broadcasters get " (mod)" after the name; subscribers get " (sub)".
Emotes are flattened from [emote:ID:NAME] to NAME.

The "kick:" prefix keeps memories separate from same-named viewers on other
platforms. Your brain can strip it for display (demo_main does).

The receiver forwards EVERY message in the channel; it does not gate on a
mention or keyword. Decide in your handler what deserves a reply. (The old
Pusher websocket path, which gated on a mention, is retired and not included.)

--------------------------------------------------------------------------------
 FILES YOU CAN DELETE
--------------------------------------------------------------------------------
If you already have a tunnel and an OAuth flow:
  kick_tunnel_host.py, start_kick_tunnel.bat, setup_kick_auth.py
The receiver and sender have no dependency on them.

--------------------------------------------------------------------------------
 TROUBLESHOOTING
--------------------------------------------------------------------------------
  No messages, status page reachable
      Kick's portal webhook URL is wrong, or the subscription points at a
      different broadcaster. Run setup_kick_webhook.py show/subscribe.
      Remember: https://.../kick/events

  tunnel_ok=false
      The public URL no longer reaches the receiver. Restart the tunnel; if
      the hostname changed, update the portal and re-subscribe.

  rejected climbing
      Requests are failing signature verification. Check that the request
      really comes from Kick (correct URL) and that the system clock is sane.
      The public key is fetched live and cached for an hour.

  400 "webhook not enabled for app"
      Enable webhooks for your app in the Kick developer portal.

  No text replies
      missing kick_tokens.json, KICK_TEXT_REPLIES=0, or expired/re-run needed
      OAuth. Run setup_kick_auth.py again (as the bot account).

  Bot cannot resolve the channel
      KICK_CHANNEL must be the slug (after kick.com/), lowercase.

--------------------------------------------------------------------------------
 INTEGRATION NOTE
--------------------------------------------------------------------------------
The modules import only the stdlib plus requests/aiohttp/rich. They read no
config and no environment variables - everything is passed to the constructor.
config.py and demo_main.py exist to make the standalone demo run.

demo_main.py imports them as "input.kick_webhook" / "input.kick_sender", so the
kit expects an input/ folder at your project root. If your project uses another
package name, adjust the two imports; the modules themselves do not care where
they live.

See INTEGRATION.md for the queue contract, constructor tables, threading notes
and the HTTP endpoints.
================================================================================
