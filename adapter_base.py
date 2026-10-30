"""Base adapter for plugging the Kick bridge into any bot framework.

Subclass this and implement handle_message(). Your adapter receives
(username, message, msg_id) from Kick and returns a reply string (or None
if your bot answers asynchronously via outbox / callback).

See adapter_radeon_cohost.py and adapter_http.py for examples.
"""

from kick_bridge import AdapterBase, MessageQueue


class AdapterBase:
    name = "base"

    def __init__(self, config=None):
        self.config = config or {}

    def handle_message(self, username, message, msg_id):
        """Called synchronously inside the webhook thread.

        username: Kick username
        message: message text (prefix already stripped)
        msg_id: unique Kick message id

        Return a reply string to post back to Kick, or None to defer.
        """
        raise NotImplementedError


class FileAdapter(AdapterBase):
    """Writes incoming Kick messages into messages.json inbox. An external
    bot (any language) polls inbox, computes a reply, and writes it to
    outbox. The bridge polls outbox and posts to Kick."""

    name = "file"

    def __init__(self, config=None):
        super().__init__(config)
        self.queue = MessageQueue()

    def handle_message(self, username, message, msg_id):
        self.queue.push_inbox(username, message, msg_id)
        return None  # reply arrives later via outbox

    def poll_outbox(self):
        return self.queue.pop_outbox()


class HttpAdapter(AdapterBase):
    """POSTs each Kick message to a callback URL your bot exposes."""

    name = "http"

    def __init__(self, config=None):
        super().__init__(config)
        import requests
        self._requests = requests
        self.callback_url = config.get("http_callback", "")
        self.timeout = float(config.get("http_timeout", 10))

    def handle_message(self, username, message, msg_id):
        payload = {
            "id": msg_id,
            "username": username,
            "message": message,
            "platform": "kick",
        }
        try:
            r = self._requests.post(
                self.callback_url, json=payload, timeout=self.timeout
            )
            if r.status_code == 200:
                try:
                    data = r.json()
                    return data.get("reply")
                except Exception:
                    return (r.text or "")[:500] or None
        except Exception as e:
            print(f"[kick-http] adapter error: {e}")
        return None
