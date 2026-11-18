"""Phone and chat notifications. Both channels are optional and free.

ntfy  (https://ntfy.sh): install the ntfy app, subscribe to a long random topic name, put NTFY_TOPIC=<that name>
      in .env. Anyone who knows the topic can read it, so make it long and unguessable.
Discord: Server Settings > Integrations > Webhooks > New Webhook > Copy URL, put DISCORD_WEBHOOK_URL=<url> in .env.
"""
from __future__ import annotations

import requests

from .config import secret

PRIORITY = {"urgent": 5, "error": 4, "warn": 3, "info": 2}
TAGS = {"urgent": "rotating_light", "error": "warning", "warn": "eyes", "info": "clapper"}


def channels() -> list[str]:
    return [name for name, var in (("ntfy", "NTFY_TOPIC"), ("discord", "DISCORD_WEBHOOK_URL")) if secret(var)]


def send(title: str, text: str, level: str = "info", link: str | None = None) -> list[str]:
    """Send to every configured channel. Returns the channels that accepted it."""
    sent = []
    topic = secret("NTFY_TOPIC")
    if topic:
        server = (secret("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        # JSON publishing keeps emoji and accents intact (HTTP headers can't carry them).
        body = {"topic": topic, "title": title[:200], "message": text[:4000], "priority": PRIORITY.get(level, 3),
                "tags": [TAGS.get(level, "bell")]}
        if link and link.startswith("http"):
            body["click"] = link
        try:
            r = requests.post(server, json=body, timeout=15)
            if r.status_code < 300:
                sent.append("ntfy")
        except requests.RequestException:
            pass
    hook = secret("DISCORD_WEBHOOK_URL")
    if hook:
        body = f"**{title}**\n{text}" + (f"\n{link}" if link and link.startswith("http") else "")
        try:
            r = requests.post(hook, json={"content": body[:1900]}, timeout=15)
            if r.status_code < 300:
                sent.append("discord")
        except requests.RequestException:
            pass
    return sent
