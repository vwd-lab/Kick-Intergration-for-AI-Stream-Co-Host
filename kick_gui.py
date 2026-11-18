"""Kick bridge GUI — tkinter (stdlib only, no extra pip install).

Starts kick_bridge.py as a subprocess and streams its log into a live
window. Lets you configure channel, credentials, adapter, and public URL
without touching config.json by hand.

Works whether you are plugging into Radeon AI Co-Host (file adapter) or
your own custom bot (HTTP adapter).
"""

import json
import os
import subprocess
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# ---------------------------------------------------------------------------
# Running the bridge without freezing the window
# (The same code is used in the OBS, Discord and Kick GUIs.)
# ---------------------------------------------------------------------------

class BridgeProcess:
    """Runs a bridge script and collects its output without ever blocking the
    window.

    Before, the GUI called stdout.readline() on the window's own thread, which
    waits until the bridge prints something - so the window froze ("Not
    Responding") whenever the bridge was quiet, and Stop couldn't be clicked.
    Now a background thread reads the output into a queue and the window only
    takes what's already there.

    Also:
    - the bridge's output is unbuffered UTF-8, so lines show up right away and
      emoji/accents in chat can't crash its printing;
    - Stop asks the bridge to shut down cleanly first (by closing its stdin;
      the bridge treats that like Ctrl+C and runs its own cleanup, e.g. hiding
      the OBS image) and only forces it if it hasn't exited after a grace
      period. Before, Stop killed it outright and the cleanup never ran.
    """

    def __init__(self, script, cwd):
        env = {**os.environ,
               "PYTHONUNBUFFERED": "1",        # print lines immediately
               "PYTHONIOENCODING": "utf-8",    # emoji/accents can't crash printing
               "BRIDGE_GUI_CHILD": "1"}        # "stdin closed" means "please stop"
        self.proc = subprocess.Popen(
            [sys.executable or "python", "-u", script],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self.pid = self.proc.pid
        self.finished = False           # True once all output has been read
        self._lines = queue.Queue()
        self._stopping = False
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self):
        try:
            for line in self.proc.stdout:
                self._lines.put(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)       # end of output

    def poll(self):
        return self.proc.poll()

    @property
    def returncode(self):
        return self.proc.returncode

    def drain(self, limit=500):
        """Lines printed since the last call. Never waits."""
        out = []
        for _ in range(limit):
            try:
                item = self._lines.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.finished = True
                break
            out.append(item)
        return out

    def request_stop(self, grace_s=8.0):
        """Ask for a clean stop, then force it after grace_s. Returns at once."""
        if self._stopping:
            return
        self._stopping = True
        threading.Thread(target=self._stop, args=(grace_s,), daemon=True).start()

    def _stop(self, grace_s):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        self._lines.put(f"[bridge didn't stop within {grace_s:.0f}s - forcing it]")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def stop_and_wait(self, timeout=12.0):
        """For closing the window: stop and wait (bounded)."""
        self.request_stop()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()


ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config.example.json")

DEFAULT_CONFIG = {
    "kick_channel": "",
    "kick_client_id": "",
    "kick_client_secret": "",
    "kick_access_token": "",
    "kick_refresh_token": "",
    "public_url": "",
    "webhook_port": 8765,
    "adapter": "file",
    "http_callback": "",
    "text_replies": True,
    "command_prefix": "!ask",
    "reply_prefix": "Bot:",
    "max_reply_chars": 500,
    "log_to_console": True,
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    # config.json holds the app's client secret: when the kit's packages are
    # installed, write it so only this Windows account can read it.
    try:
        from input.kick_sender import write_private_json
    except Exception:
        write_private_json = None
    if write_private_json is not None:
        write_private_json(CONFIG_PATH, cfg)
        return
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class KickBridgeGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Kick AI Bridge — Launcher")
        self.geometry("900x620")
        self.minsize(700, 480)

        self.process = None
        self.log_queue = []
        self.cfg = load_config()

        self._build_ui()
        self._load_config_into_fields()

        # Poll the subprocess output so the GUI stays responsive
        self.after(100, self._poll_process)
        self.after(500, self._poll_log_queue)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        # Top: status bar
        self.status_var = tk.StringVar(value="Stopped")
        status = ttk.Label(
            self, textvariable=self.status_var,
            relief=tk.SUNKEN, anchor=tk.W, padding=(6, 3),
        )
        status.pack(side=tk.BOTTOM, fill=tk.X)

        # Notebook: Config | Log | About
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True)

        self.tab_config = ttk.Frame(nb, padding=10)
        self.tab_log = ttk.Frame(nb, padding=10)
        self.tab_about = ttk.Frame(nb, padding=10)
        nb.add(self.tab_config, text="Configure")
        nb.add(self.tab_log, text="Live Log")
        nb.add(self.tab_about, text="About")

        self._build_config_tab()
        self._build_log_tab()
        self._build_about_tab()

    def _build_config_tab(self):
        f = self.tab_config

        # Left column: Kick credentials
        left = ttk.LabelFrame(f, text="Kick credentials", padding=10)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        self.entries = {}

        def entry_row(parent, label, key, width=40, show=False):
            row = ttk.Frame(parent)
            row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=label, width=22, anchor=tk.W).pack(side=tk.LEFT)
            var = tk.StringVar()
            ent = ttk.Entry(row, textvariable=var, width=width, show="*" if show else "")
            ent.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.entries[key] = var
            return row

        entry_row(left, "Channel slug", "kick_channel")
        entry_row(left, "Client ID", "kick_client_id")
        entry_row(left, "Client secret", "kick_client_secret", show=True)
        entry_row(left, "Access token", "kick_access_token", show=True)
        entry_row(left, "Refresh token", "kick_refresh_token", show=True)

        # Public URL / webhook
        row = ttk.Frame(left)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Public webhook URL", width=22, anchor=tk.W).pack(side=tk.LEFT)
        self.entries["public_url"] = tk.StringVar()
        ttk.Entry(row, textvariable=self.entries["public_url"]).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            left,
            text="Give this URL to Kick in your app's webhook settings.\n"
                 "Use cloudflared or ngrok to reach this PC (no port-forward:\n"
                 "the bridge only listens on 127.0.0.1).",
            wraplength=300, foreground="#666666",
        ).pack(anchor=tk.W, pady=(4, 0))

        # Right column: adapter + launch controls
        right = ttk.LabelFrame(f, text="Bridge settings", padding=10)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        row = ttk.Frame(right)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Adapter", width=22, anchor=tk.W).pack(side=tk.LEFT)
        self.adapter_var = tk.StringVar(value=self.cfg.get("adapter", "file"))
        ttk.Combobox(
            row, textvariable=self.adapter_var,
            values=["file", "http"], state="readonly", width=16,
        ).pack(side=tk.LEFT)
        ttk.Label(
            right,
            text="file = shared mailbox folder (Radeon AI Co-Host, any language)\n"
                 "http  = POST each message to your bot's callback URL",
            wraplength=320, foreground="#666666",
        ).pack(anchor=tk.W, pady=(4, 0))

        # Bridge mode: production demo_main.py vs simple kick_bridge.py
        row = ttk.Frame(right)
        row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(row, text="Bridge mode", width=22, anchor=tk.W).pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value=self.cfg.get("bridge_mode", "production"))
        ttk.Combobox(
            row, textvariable=self.mode_var,
            values=["production", "simple"], state="readonly", width=16,
        ).pack(side=tk.LEFT)
        ttk.Label(
            right,
            text="production = demo_main.py with Kick signature verification,\n"
                 "tunnel health watchdog, and token refresh (recommended)\n"
                 "simple    = kick_bridge.py, single file, Kick signature\n"
                 "            verification, no watchdog or token refresh",
            wraplength=320, foreground="#666666",
        ).pack(anchor=tk.W, pady=(4, 0))

        entry_row(right, "HTTP callback URL", "http_callback")
        entry_row(right, "Webhook port", "webhook_port", width=10)

        # Toggles
        self.text_replies_var = tk.BooleanVar(value=bool(self.cfg.get("text_replies", True)))
        ttk.Checkbutton(
            right, text="Post bot replies back into Kick chat",
            variable=self.text_replies_var,
        ).pack(anchor=tk.W, pady=(8, 2))

        row = ttk.Frame(right)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Command prefix", width=22, anchor=tk.W).pack(side=tk.LEFT)
        self.entries["command_prefix"] = tk.StringVar(value=self.cfg.get("command_prefix", "!ask"))
        ttk.Entry(row, textvariable=self.entries["command_prefix"], width=10).pack(side=tk.LEFT)

        row = ttk.Frame(right)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Reply prefix", width=22, anchor=tk.W).pack(side=tk.LEFT)
        self.entries["reply_prefix"] = tk.StringVar(value=self.cfg.get("reply_prefix", "Bot:"))
        ttk.Entry(row, textvariable=self.entries["reply_prefix"], width=12).pack(side=tk.LEFT)

        # Launch buttons
        btns = ttk.Frame(right)
        btns.pack(fill=tk.X, pady=(16, 0))
        self.btn_start = ttk.Button(btns, text="Start bridge", command=self._start_bridge)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_stop = ttk.Button(btns, text="Stop bridge", command=self._stop_bridge, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)
        ttk.Button(btns, text="Save config", command=self._save_config).pack(side=tk.LEFT, padx=(10, 0))

    def _build_log_tab(self):
        f = self.tab_log
        self.log_text = scrolledtext.ScrolledText(f, wrap=tk.WORD, font=("Consolas", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        btns = ttk.Frame(f)
        btns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="Clear log", command=self._clear_log).pack(side=tk.LEFT)

    def _build_about_tab(self):
        f = self.tab_about
        text = (
            "Kick AI Bridge\n\n"
            "Framework-agnostic Kick chat bridge. Receives Kick webhook\n"
            "messages and hands them to your bot adapter:\n\n"
            "  • file  — shared mailbox folder (one file per message)\n"
            "        (plugs into Radeon AI Co-Host or any custom bot)\n"
            "  • http  — POSTs each message to your bot's callback URL\n\n"
            "Requires: Python 3, requests\n"
            "Optional: cloudflared / ngrok for the public webhook URL\n\n"
            "No credentials are hard-coded; everything lives in config.json.\n"
        )
        ttk.Label(f, text=text, justify=tk.LEFT, wraplength=640).pack(anchor=tk.W, pady=10)

    # ------------------------------------------------------------------ config fields

    def _load_config_into_fields(self):
        for key, var in self.entries.items():
            var.set(str(self.cfg.get(key, "")))

    def _collect_config(self):
        cfg = {}
        for key, var in self.entries.items():
            val = var.get().strip()
            cfg[key] = val
        cfg["adapter"] = self.adapter_var.get()
        cfg["bridge_mode"] = self.mode_var.get()
        cfg["text_replies"] = bool(self.text_replies_var.get())
        # coerce port to int
        try:
            cfg["webhook_port"] = int(cfg.get("webhook_port", 8765))
        except ValueError:
            cfg["webhook_port"] = 8765
        return cfg

    def _save_config(self):
        cfg = self._collect_config()
        save_config(cfg)
        self.cfg = cfg
        messagebox.showinfo("Saved", "config.json written.")

    # ------------------------------------------------------------------ bridge process

    def _start_bridge(self):
        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning("Already running", "The bridge is already running.")
            return

        cfg = self._collect_config()
        if not cfg.get("kick_channel"):
            messagebox.showwarning("Missing channel", "Please set a Kick channel slug.")
            return

        save_config(cfg)
        self.cfg = cfg

        # Launch the chosen bridge script as subprocess
        if self.mode_var.get() == "production":
            script = os.path.join(ROOT, "demo_main.py")
            mode_label = "production"
        else:
            script = os.path.join(ROOT, "kick_bridge.py")
            mode_label = "simple"
        try:
            self.process = BridgeProcess(script, ROOT)
        except Exception as e:
            messagebox.showerror("Start failed", str(e))
            return

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.status_var.set("Running")
        self._log_ui(f"Bridge started ({mode_label}, PID {self.process.pid})")
        self._log_ui(f"Adapter: {cfg.get('adapter')}  Port: {cfg.get('webhook_port')}")
        if not cfg.get("public_url"):
            self._log_ui("[warn] No public webhook URL set — Kick cannot reach this bridge.")

    def _stop_bridge(self):
        """Ask the bridge to stop cleanly. Returns at once; _poll_process
        notices when it has exited and resets the buttons."""
        if self.process is None:
            return
        self.process.request_stop()
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_var.set("Stopping...")
        self._log_ui("Stopping the bridge (letting it clean up)...")

    def _set_stopped(self):
        self.process = None
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_var.set("Stopped")

    # ------------------------------------------------------------------ log plumbing

    def _poll_process(self):
        """Show whatever the bridge has printed so far. Never waits for
        output - the old readline() here froze the window."""
        proc = self.process
        if proc is not None:
            self.log_queue.extend(proc.drain())
            if proc.finished and proc.poll() is not None:
                self.log_queue.append(f"[process exited code {proc.returncode}]")
                self.log_queue.append("Bridge stopped.")
                self._set_stopped()
        self.after(100, self._poll_process)

    def _poll_log_queue(self):
        while self.log_queue:
            line = self.log_queue.pop(0)
            self._log_ui(line)
        self.after(500, self._poll_log_queue)

    def _log_ui(self, text):
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)

    def _clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def on_closing(self):
        if self.process is not None:
            self.status_var.set("Stopping...")
            self.update_idletasks()
            self.process.stop_and_wait()
        self.destroy()


if __name__ == "__main__":
    app = KickBridgeGUI()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
