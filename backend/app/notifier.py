"""ntfy client. Uses JSON publishing (POST to server root) so non-latin-1 titles are safe."""
from __future__ import annotations

import logging
from typing import Any, Sequence

import httpx

log = logging.getLogger("stockwatcher.notifier")

NTFY_TIMEOUT = 10.0


async def send_ntfy(
    settings: Any,
    title: str,
    message: str,
    click_url: str | None = None,
    tags: Sequence[str] | None = None,
    priority: int | None = None,
    actions: Sequence[dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    """Publish to ntfy. `settings` needs ntfy_server, ntfy_topic, ntfy_token (and ntfy_priority).

    `actions` are ntfy action buttons, e.g. ``[{"action": "view", "label": "Add to cart", "url": ...}]``
    (ntfy allows at most 3).

    Returns (ok, error). Never raises.
    """
    topic = (getattr(settings, "ntfy_topic", None) or "").strip()
    if not topic:
        return False, "ntfy topic is not configured"
    server = (getattr(settings, "ntfy_server", None) or "https://ntfy.sh").strip().rstrip("/")
    token = getattr(settings, "ntfy_token", None)
    if priority is None:
        priority = getattr(settings, "ntfy_priority", None) or 3
    payload: dict[str, Any] = {
        "topic": topic,
        "title": title[:250],
        "message": message[:4000] or title[:250],
        "priority": max(1, min(5, int(priority))),
    }
    if tags:
        payload["tags"] = list(tags)
    if click_url:
        payload["click"] = click_url
    if actions:
        payload["actions"] = [dict(a) for a in actions][:3]
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=NTFY_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(f"{server}/", json=payload, headers=headers)
    except httpx.TimeoutException:
        return False, "ntfy request timed out"
    except httpx.HTTPError as e:
        return False, f"ntfy request failed: {type(e).__name__}: {e}"[:300]
    except Exception as e:  # noqa: BLE001
        return False, f"ntfy request failed: {e}"[:300]
    if 200 <= resp.status_code < 300:
        return True, None
    body = (resp.text or "").strip().replace("\n", " ")[:200]
    return False, f"ntfy returned HTTP {resp.status_code}" + (f": {body}" if body else "")
