"""Interactive setup for the Kick kit: asks a few questions, writes config.json.

Run by setup_kick_gui.bat, or directly:  python setup_wizard.py
- Everything else already in config.json (OAuth tokens, http_callback, your
  own keys) is kept.
- The command prefix keeps its "!" (the old .bat turned "!ask" into "ask").
- Uses only the Python standard library, so it works before `pip install`.
- Press Enter to keep the value shown in [brackets]. Type - to clear a value.
- Secrets (passwords, tokens) are not shown while you type them.
- Exits with code 1 and writes nothing if setup is cancelled or fails, so the
  .bat file knows not to launch the GUI.

Replaces the multi-line `python -c "..."` the .bat used before: cmd.exe can't
pass a multi-line string, so the settings were never actually written.
"""
import getpass
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# (key, question, default, kind[, options])
QUESTIONS = [
    ("kick_channel", "Kick channel slug (the part after kick.com/)", "", "text"),
    ("public_url", "Public webhook URL (https://...)", "", "text"),
    ("kick_client_id", "Kick Client ID", "", "text"),
    ("kick_client_secret", "Kick Client Secret", "", "secret"),
    ("adapter", "Adapter: file = shared mailbox folder, http = POST to your bot", "file", "choice",
     ["file", "http"]),
    ("bridge_mode", "Bridge mode: production = demo_main.py (recommended), simple = kick_bridge.py",
     "production", "choice", ["production", "simple"]),
    ("webhook_port", "Webhook port", "8765", "port"),
    ("command_prefix", "Command prefix", "!ask", "text"),
    ("text_replies", "Post bot replies back into Kick chat?", "1", "yesno"),
]

# Written only when config.json doesn't have them yet.
NEW_FILE_DEFAULTS = {
    "kick_access_token": "",
    "kick_refresh_token": "",
    "http_callback": "",
    "reply_prefix": "Bot:",
    "max_reply_chars": 500,
    "log_to_console": True,
}

# ---------------------------------------------------------------- shared helpers
# (The same helper code is used in the OBS, Discord and Kick kits.)
class Cancelled(Exception):
    pass


def read_line(prompt, secret=False):
    try:
        if secret and sys.stdin.isatty():
            return getpass.getpass(prompt)
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Cancelled()


def ask(question, current, kind, options=None):
    """Ask until the answer is valid. Returns the value as a string.

    kind: text | secret | yesno | port | count | seconds | id | idlist | choice
    """
    while True:
        if kind == "yesno":
            shown = "yes" if current == "1" else "no"
            raw = read_line(f"{question} [yes/no] [{shown}]: ").strip().lower()
            if not raw:
                return current
            if raw in ("y", "yes", "1", "true", "on"):
                return "1"
            if raw in ("n", "no", "0", "false", "off"):
                return "0"
            print("  Please answer yes or no.")
            continue
        if kind == "secret":
            shown = "keep current" if current else "none"
            raw = read_line(f"{question} [{shown}]: ", secret=True).strip()
        elif kind == "choice":
            raw = read_line(f"{question} [{'/'.join(options)}] [{current}]: ").strip().lower()
        else:
            raw = read_line(f"{question} [{current}]: ").strip()
        if not raw:
            return current
        if raw == "-" and kind not in ("port", "count", "seconds", "choice"):
            return ""
        if "\n" in raw or "\r" in raw:
            print("  That can't contain a line break.")
            continue
        if kind == "port":
            if raw.isdigit() and 0 < int(raw) < 65536:
                return raw
            print("  Please enter a port number between 1 and 65535.")
        elif kind == "count":
            if raw.isdigit() and int(raw) > 0:
                return raw
            print("  Please enter a whole number of 1 or more.")
        elif kind == "seconds":
            try:
                if float(raw) > 0:
                    return raw
            except ValueError:
                pass
            print("  Please enter a number of seconds, e.g. 3.0.")
        elif kind == "id":
            if raw.isdigit():
                return raw
            print("  Please enter the ID as digits only (right-click > Copy ID in Discord).")
        elif kind == "idlist":
            ids = [p.strip() for p in raw.split(",") if p.strip()]
            if ids and all(p.isdigit() for p in ids):
                return ",".join(ids)
            print("  Please enter digits only, separated by commas, e.g. 123456789012345678.")
        elif kind == "choice":
            if raw in options:
                return raw
            print(f"  Please choose one of: {', '.join(options)}.")
        else:
            return raw


def ask_all(questions, current):
    answers = {}
    for key, question, default, kind, *rest in questions:
        options = rest[0] if rest else None
        answers[key] = ask(question, current.get(key, default), kind, options)
    return answers


def save_atomically(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    _only_this_user_can_read(path)


def _only_this_user_can_read(path):
    """config.json / .env hold the app's client secret. When the kit's
    packages are installed, restrict the file to this Windows account."""
    try:
        from input.kick_sender import restrict_to_owner
    except Exception:
        return
    restrict_to_owner(path)


def read_env(path):
    """Return (lines, values) from an existing .env, or ([], {})."""
    if not os.path.exists(path):
        return [], {}
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    values = {}
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            values[k.strip()] = v.strip()
    return lines, values


def write_env(path, lines, answers, header):
    """Update answered keys in place, append new ones, keep everything else.

    Values are written unquoted (KEY=value), the same format the kit's GUI
    reads and writes.
    """
    out, done = [], set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in answers:
                if k not in done:
                    out.append(f"{k}={answers[k]}")
                    done.add(k)
                continue
        out.append(line)
    if not lines:
        out = list(header)
    for k, v in answers.items():
        if k not in done:
            out.append(f"{k}={v}")
    save_atomically(path, "\n".join(out) + "\n")


def intro():
    print()
    print("Press Enter to keep the value in [brackets]. Type - to clear a value.")
    print()


# ---------------------------------------------------------------- main
def as_text(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    return "" if value is None else str(value)


def main():
    intro()
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            print(f"config.json exists but can't be read ({e}).")
            print("Fix or rename it first, so the tokens in it aren't lost. Nothing was written.")
            return 1
    current = {k: as_text(v) for k, v in cfg.items()}
    try:
        answers = ask_all(QUESTIONS, current)
    except Cancelled:
        print("\nSetup cancelled - nothing was written.")
        return 1
    for k, v in NEW_FILE_DEFAULTS.items():
        cfg.setdefault(k, v)
    cfg.update(answers)
    cfg["webhook_port"] = int(answers["webhook_port"])
    cfg["text_replies"] = answers["text_replies"] == "1"
    cfg["command_prefix"] = answers["command_prefix"] or "!ask"
    try:
        save_atomically(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n")
    except OSError as e:
        print(f"\nCould not write {CONFIG_PATH}: {e}")
        return 1
    print(f"\nSaved {CONFIG_PATH}")
    if not answers["kick_channel"]:
        print("Note: kick_channel is still empty - the bridge needs it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
