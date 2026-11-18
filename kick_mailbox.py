"""Kick <-> Radeon AI Co-Host mailbox: one small JSON file per message.

The same file ships in both projects (Kick kit: kick_mailbox.py, Co-Host:
core/kick_mailbox.py). The folder layout below is the contract between them.

    <mailbox>/inbox/    Kick chat waiting for the Co-Host  (written by the Kick bridge)
    <mailbox>/outbox/   Co-Host replies waiting to go to Kick  (written by the Co-Host)
    <mailbox>/bad/      files that couldn't be read, kept so you can look at them

Default <mailbox> is the "mailbox" folder inside the Kick kit. Set
KICK_MAILBOX_DIR (both projects' .env) to put it somewhere else.

Why one file per message instead of one shared messages.json: the old design
read the whole file, worked for a while, then wrote its old copy back - so
anything the other program added in between was lost, and a read error wiped
the file. Here nothing is ever read, changed and written back:

- Writing: write "<name>.tmp", then rename it to "<name>.json". A rename is
  all-or-nothing, so a reader never sees half a file.
- Reading: rename "<name>.json" to "<name>.work" first. Only one reader can
  win that rename, so a message is never handled twice. Then read and delete.
- A file that can't be read is moved to bad/ instead of being retried forever.
- A file that's briefly locked (antivirus, OneDrive) is simply picked up on the
  next pass.

Messages are handled oldest first (file names start with a nanosecond time).
Standard library only.

Inbox item:  {"id", "username", "message", "received_at"}
Outbox item: {"reply", "username", "reply_to", "created_at"}
"""
import json
import os
import threading
import time
import uuid

LEFTOVER_MAX_AGE_S = 600     # .tmp/.work files older than this are from a crash

# Windows' clock can return the same time_ns for two quick messages; without
# this, their order would depend on the random part of the name.
_NAME_LOCK = threading.Lock()
_LAST_NS = [0]


class Mailbox:
    def __init__(self, folder, max_outbox=200):
        self.folder = os.path.abspath(folder)
        self.inbox = os.path.join(self.folder, "inbox")
        self.outbox = os.path.join(self.folder, "outbox")
        self.bad = os.path.join(self.folder, "bad")
        self.max_outbox = max_outbox

    # ---- setup -------------------------------------------------------------
    def ensure(self):
        """Create the folders if they don't exist yet."""
        for path in (self.inbox, self.outbox, self.bad):
            os.makedirs(path, exist_ok=True)

    def exists(self):
        return os.path.isdir(self.inbox) and os.path.isdir(self.outbox)

    # ---- inbox: Kick bridge writes, Co-Host reads ----------------------------
    def put_inbox(self, username, message, msg_id=None):
        return self._put(self.inbox, {
            "id": msg_id or uuid.uuid4().hex,
            "username": username,
            "message": message,
            "received_at": time.time(),
        })

    def take_inbox(self, limit=50):
        """Claim and return waiting chat messages, oldest first."""
        return self._take(self.inbox, limit)

    # ---- outbox: Co-Host writes, Kick bridge reads ---------------------------
    def put_outbox(self, reply, username="", reply_to=None):
        name = self._put(self.outbox, {
            "reply": reply,
            "username": username,
            "reply_to": reply_to,
            "created_at": time.time(),
        })
        self._trim(self.outbox, self.max_outbox)
        return name

    def take_outbox(self, limit=50, max_age_s=300):
        """Claim and return replies to post. Replies older than max_age_s are
        dropped (e.g. queued while the Kick bridge wasn't running) so chat
        doesn't get a burst of stale answers."""
        fresh, now = [], time.time()
        for item in self._take(self.outbox, limit):
            created = item.get("created_at")
            if max_age_s and isinstance(created, (int, float)) and now - created > max_age_s:
                continue
            fresh.append(item)
        return fresh

    # ---- internals -----------------------------------------------------------
    @staticmethod
    def _new_name():
        """Sortable name: time in ns, kept strictly increasing within this
        program, then the process id and a random part to stay unique across
        programs."""
        with _NAME_LOCK:
            ns = max(time.time_ns(), _LAST_NS[0] + 1)
            _LAST_NS[0] = ns
        return f"{ns:020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def _put(self, box, item):
        os.makedirs(box, exist_ok=True)
        name = self._new_name()
        tmp = os.path.join(box, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(box, name + ".json"))
        return name

    def _take(self, box, limit):
        try:
            names = sorted(os.listdir(box))
        except FileNotFoundError:
            return []
        self._sweep_leftovers(box, names)
        items = []
        for name in [n for n in names if n.endswith(".json")][:limit]:
            path = os.path.join(box, name)
            work = path[:-5] + ".work"
            try:
                os.replace(path, work)          # claim it - only one reader wins
            except OSError:
                continue                        # taken by someone else, or locked: next pass
            try:
                with open(work, encoding="utf-8") as f:
                    item = json.load(f)
                if not isinstance(item, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError):
                self._quarantine(work, name)
                continue
            try:
                os.remove(work)
            except OSError:
                pass
            items.append(item)
        return items

    def _quarantine(self, path, name):
        try:
            os.makedirs(self.bad, exist_ok=True)
            os.replace(path, os.path.join(self.bad, name))
        except OSError:
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _sweep_leftovers(box, names):
        now = time.time()
        for name in names:
            if name.endswith((".tmp", ".work")):
                path = os.path.join(box, name)
                try:
                    if now - os.path.getmtime(path) > LEFTOVER_MAX_AGE_S:
                        os.remove(path)
                except OSError:
                    pass

    @staticmethod
    def _trim(box, keep):
        try:
            names = sorted(n for n in os.listdir(box) if n.endswith(".json"))
        except FileNotFoundError:
            return
        for name in names[:max(0, len(names) - keep)]:
            try:
                os.remove(os.path.join(box, name))
            except OSError:
                pass
