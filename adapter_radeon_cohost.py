"""Kick -> Radeon AI Co-Host adapter.

Writes incoming Kick chat into the shared mailbox folder (see kick_mailbox.py):
one file per message in mailbox/inbox. The Co-Host reads them, answers, and
drops its replies in mailbox/outbox; the bridge posts those back to Kick.

Nothing needs to be pasted into the Co-Host any more: its main.py reads the
mailbox itself (core/kick_mailbox.py, the same file as kick_mailbox.py here).
Both sides use <this kit>/mailbox by default. If you move it, set
KICK_MAILBOX_DIR to the same folder in the Co-Host's .env and this kit's .env
(or "mailbox_dir" in config.json for kick_bridge.py).

The old version of this file suggested pasting a loop into the Co-Host that
read messages.json, worked through it, then wrote its old copy back. That lost
any message that arrived in the meantime, so don't use that snippet.
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
