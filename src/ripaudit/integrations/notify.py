"""Notification delivery: ntfy and generic JSON webhook.

Messages are queued in the ``notifications`` table with a unique dedupe key, so
an unchanged issue is never announced twice. Delivery retries use bounded
exponential backoff (1, 2, 4, 8 ... minutes, capped at 60) for up to
``MAX_ATTEMPTS`` attempts.
"""

from __future__ import annotations

import json
import time

import httpx

from ..security import redact

MAX_ATTEMPTS = 6


class NotifyError(Exception):
    pass


def backoff_seconds(attempts: int) -> float:
    return float(min(60 * (2 ** max(0, attempts - 1)), 3600))


def send(cfg: dict, title: str, body: str, priority: str, transport: httpx.BaseTransport | None = None) -> None:
    kind = cfg.get("kind", "none")
    url = cfg.get("url", "")
    token = cfg.get("token", "")
    if kind == "none" or not url:
        raise NotifyError("Notifications are not configured.")
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15.0, transport=transport) as client:
            if kind == "ntfy":
                headers.update({"Title": title.encode("ascii", "replace").decode(),
                                "Priority": "high" if priority == "high" else "default",
                                "Tags": "film_frames"})
                resp = client.post(url, content=body.encode("utf-8"), headers=headers)
            elif kind == "webhook":
                payload = {"source": "ripaudit", "title": title, "body": body, "priority": priority,
                           "sent_at": int(time.time())}
                headers["Content-Type"] = "application/json"
                resp = client.post(url, content=json.dumps(payload), headers=headers)
            else:
                raise NotifyError(f"Unknown notification kind: {kind}")
    except httpx.HTTPError as exc:
        raise NotifyError(redact(f"Delivery failed: {type(exc).__name__}", [token])) from None
    if resp.status_code >= 400:
        raise NotifyError(f"Notification endpoint returned HTTP {resp.status_code}.")
