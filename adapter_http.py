"""Kick -> custom bot adapter via HTTP callback.

Your bot exposes an HTTP endpoint (Flask, FastAPI, Express, etc.) that
accepts:

    POST /kick
    {
        "id": "<msg_id>",
        "username": "<kick_username>",
        "message": "<text without prefix>",
        "platform": "kick"
    }

And replies with:

    {
        "reply": "<text to post back into Kick chat>"
    }

Or returns 204 / empty body if it will answer asynchronously (then it must
POST the reply itself to Kick's API).
"""

import sys
import os

KICK_ROOT = os.path.dirname(os.path.abspath(__file__))
if KICK_ROOT not in sys.path:
    sys.path.insert(0, KICK_ROOT)

from adapter_base import HttpAdapter


def make_adapter(config=None):
    return HttpAdapter(config or {})


# Example callback server (optional, run this in your bot project):
#
#     from flask import Flask, request, jsonify
#     app = Flask(__name__)
#
#     @app.post("/kick")
#     def kick():
#         data = request.get_json()
#         reply = my_bot.answer(data["username"], data["message"])
#         return jsonify({"reply": reply})
#
#     app.run(port=5000)
#
# Then set http_callback=http://localhost:5000/kick in config.json.
