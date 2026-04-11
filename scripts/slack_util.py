"""Slack helpers — channel string cleanup and API headers."""
from __future__ import annotations

import os
from typing import Any


def slack_channel() -> str:
    """Channel name (#foo), encoded ID (C…), or IM id. Normalized from .env."""
    raw = (os.environ.get("SLACK_CHANNEL") or "#launches").strip().strip("'\"")
    return raw or "#launches"


def slack_json_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def slack_error_hint(data: dict[str, Any]) -> str:
    err = data.get("error")
    if err == "channel_not_found":
        return (
            " Create #launches (or your target channel) in the **same workspace** as this bot token, "
            "invite the app to the channel, or set SLACK_CHANNEL to the channel ID (e.g. C012AB34CD) "
            "from Slack → channel name → View channel details."
        )
    if err == "not_in_channel":
        return " Invite the Slack app to the channel: `/invite @YourAppName`."
    return ""
