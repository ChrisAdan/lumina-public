"""
services/ntfy.py — send push notifications via a self-hosted ntfy server.
"""
from __future__ import annotations

import logging

import httpx

from configs.app import NTFY_TOPIC, NTFY_URL

log = logging.getLogger("lumina.ntfy")


async def send(
    message: str,
    title: str | None = None,
    topic: str | None = None,
    priority: str = "default",
    tags: list[str] | None = None,
) -> None:
    """POST a notification to ntfy. Fire-and-forget — logs on error, never raises."""
    t = topic or NTFY_TOPIC
    url = f"{NTFY_URL.rstrip('/')}/{t}"
    headers: dict[str, str] = {"Content-Type": "text/plain"}
    if title:
        headers["Title"] = title
    if priority and priority != "default":
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=message.encode(), headers=headers)
            resp.raise_for_status()
            log.info("[ntfy] sent → %s: %s", t, message[:80])
    except Exception as e:
        log.error("[ntfy] failed to send notification: %s", e)
