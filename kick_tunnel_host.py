"""cloudflared quick tunnel for the Kick webhook, in its own process.

Run alongside your bot. It:
  1. launches cloudflared and waits for it to hand out a public hostname
  2. writes that hostname to KICK_TUNNEL_URL_FILE

It deliberately does NOT start the webhook receiver. demo_main.py (or your own
bot) owns that, because the receiver has to hand chat messages to your brain.
Starting two receivers on the same port fails with "address already in use".

Quick-tunnel hostnames change on every restart, so the URL is discovered at
runtime and the receiver re-reads the file, rather than being baked into .env.

Run:  python kick_tunnel_host.py
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import config
from rich import print

URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


def find_cloudflared():
    env = os.getenv("CLOUDFLARED", "").strip()
    if env:
        return env
    found = shutil.which("cloudflared")
    if found:
        return found
    for path in (r"C:\Program Files\cloudflared\cloudflared.exe",
                 r"C:\Program Files (x86)\cloudflared\cloudflared.exe"):
        if os.path.exists(path):
            return path
    return None


def write_url(url):
    try:
        parent = os.path.dirname(config.KICK_TUNNEL_URL_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(config.KICK_TUNNEL_URL_FILE, "w", encoding="utf-8") as fh:
            fh.write(url + "\n")
    except OSError as e:
        print(f"[yellow]Could not write {config.KICK_TUNNEL_URL_FILE}: {e}")


def start_tunnel(port, exe):
    """Spawn cloudflared and return (process, url_or_None)."""
    cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        print(f"[red]Could not start cloudflared: {e}")
        return None, None

    url = None

    def _read():
        nonlocal url
        for line in proc.stdout:
            m = URL_RE.search(line or "")
            if m and not url:
                url = m.group(0)
                write_url(url)
                print("=" * 66)
                print(f"  KICK PUBLIC URL: {url}")
                print("  Set this in Kick's developer portal:")
                print(f"     {url}/kick/events")
                print("  And re-register:  python setup_kick_webhook.py subscribe")
                print("=" * 66)
            elif "ERR" in (line or ""):
                print(f"[red]cloudflared: {line.strip()[:160]}")
            elif "Registered tunnel connection" in (line or ""):
                print("[green]cloudflared: tunnel connected")

    threading.Thread(target=_read, daemon=True).start()
    return proc, url


def stop_tunnel(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            print("[dim]cloudflared stopped")
        except Exception:
            pass


def main():
    print(f"[cyan]Channel: {config.KICK_CHANNEL}   "
          f"(receiver owned by your bot on port {config.KICK_WEBHOOK_PORT})")
    exe = find_cloudflared()
    if not exe:
        print("[red]cloudflared not found.")
        print("[red]  Install it:  winget install --id Cloudflare.cloudflared")
        print("[red]  Or set CLOUDFLARED to the full .exe path.")
        return 1

    proc, url = start_tunnel(config.KICK_WEBHOOK_PORT, exe)
    if url is None and proc:
        # Give cloudflared a few seconds to hand out the hostname.
        for _ in range(30):
            time.sleep(1)
            if url or (proc.poll() is not None):
                break

    if not proc:
        print("[yellow]No tunnel: Kick chat will not receive anything.")
        return 1

    print(f"[cyan]Status: http://127.0.0.1:{config.KICK_WEBHOOK_PORT}/kick/status")

    try:
        while True:
            time.sleep(5)
            if proc.poll() is not None:
                print("[red]cloudflared exited - Kick chat is DOWN. Restart this script.")
                return 1
    except KeyboardInterrupt:
        print("\n[kick-tunnel] shutting down")
        stop_tunnel(proc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
