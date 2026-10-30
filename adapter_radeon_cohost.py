"""Kick -> Radeon AI Co-Host adapter.

Writes incoming Kick chat into the shared file mailbox (messages.json).
Replies are written to outbox and the bridge posts them back to Kick.

To wire this into Radeon AI Co-Host, add the small shim below into
main.py's main loop so Kick inbox messages are fed through
process_message() exactly like Twitch chat.
"""

import sys
import os

# Allow running from the Kick folder or from Radeon AI Co-Host
KICK_ROOT = os.path.dirname(os.path.abspath(__file__))
if KICK_ROOT not in sys.path:
    sys.path.insert(0, KICK_ROOT)

from adapter_base import FileAdapter


class RadeonCohostAdapter(FileAdapter):
    name = "radeon_cohost"

    def __init__(self, config=None):
        super().__init__(config)
        # Point the mailbox at the Radeon AI Co-Host data folder if adjacent
        cohost_data = os.path.abspath(
            os.path.join(KICK_ROOT, "..", "Radeon AI Co-Host", "data")
        )
        if os.path.isdir(cohost_data):
            # messages.json lives in Kick root; we keep it here so both
            # projects share the same file via relative path.
            pass


# ---------------------------------------------------------------------------
# Shim to add inside Radeon AI Co-Host main.py
# ---------------------------------------------------------------------------
#
# Paste this into main.py's `while True:` loop (after the Twitch chat drain),
# adjusting the path to this Kick folder:
#
#     # ---- Kick chat (file mailbox) ----
#     kick_inbox = os.path.join("..", "Kick", "messages.json")
#     if os.path.exists(kick_inbox):
#         try:
#             with open(kick_inbox, "r", encoding="utf-8") as f:
#                 kick_data = json.load(f)
#         except Exception:
#             kick_data = {"inbox": [], "outbox": []}
#         for item in kick_data.get("inbox", []):
#             msg_text = item.get("message", "")
#             username = item.get("username", "KickViewer")
#             msg_id = item.get("id")
#             if dispatch(f"!ask {msg_text}", username, "kick", msg_id):
#                 continue
#             spoken = special(f"!ask {msg_text}", username)
#             if spoken is False:
#                 process_message(
#                     msg_text, source="kick", username=f"kick:{username}",
#                     chatter_memory=chatter_memory,
#                     knowledge_base=knowledge_base,
#                     conversation=conversation, ai_engine=ai_engine,
#                     tts=tts, audio_manager=audio_manager,
#                     stream_tool=stream_tool, memory_db=memory_db,
#                     lore_manager=lore_manager, ooc_mode=ooc_mode,
#                 )
#         # clear processed inbox
#         kick_data["inbox"] = []
#         with open(kick_inbox, "w", encoding="utf-8") as f:
#             json.dump(kick_data, f, indent=2)
