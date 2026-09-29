"""Due-item loop, per-item check logic and notification diffing (see PLAN.md "Scheduler")."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from . import images
from .checkers import CheckResult, run_check
from .config import get_settings
from .db import SessionLocal
from .models import CheckEvent, Item, Notification, UserSettings, utcnow
from .notifier import send_ntfy

log = logging.getLogger("stockwatcher.scheduler")

TICK_SECONDS = 10
CHECK_TIMEOUT_SECONDS = 120
KEEP_EVENTS_PER_ITEM = 500
KEEP_NOTIFICATIONS_PER_USER = 1000
VALID_STATUSES = {"in_stock", "out_of_stock", "unknown", "error"}
MAX_LABELS_IN_MESSAGE = 8

# Per-item mutual exclusion: item_id -> Event that is set when the running check finishes.
_inflight: dict[int, asyncio.Event] = {}
_queued: set[int] = set()  # scheduled by the loop, waiting on the semaphore
_background: set[asyncio.Task] = set()
_loop_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_semaphore: asyncio.Semaphore | None = None


def spawn(coro) -> asyncio.Task:
    """Run a coroutine in the background, keeping a strong reference until done."""
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# --------------------------------------------------------------------- helpers
@dataclass
class _Snapshot:
    kind: str
    url: str
    generic_config: dict | None
    apple_config: dict | None


@dataclass
class _NtfyTarget:
    ntfy_server: str
    ntfy_topic: str | None
    ntfy_token: str | None
    ntfy_priority: int


@dataclass
class _Outcome:
    notification_id: int | None = None
    title: str = ""
    message: str = ""
    click_url: str | None = None
    tags: tuple[str, ...] = ()
    target: _NtfyTarget | None = None
    send: bool = False
    fetch_image_url: str | None = None


def _load_snapshot(item_id: int) -> _Snapshot | None:
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if item is None:
            return None
        return _Snapshot(item.kind, item.url, item.generic_config, item.apple_config)


def _normalize(result: Any) -> CheckResult:
    if not isinstance(result, CheckResult):
        return CheckResult(status="error", status_text="Checker returned no result", error="Checker returned no result")
    if result.status not in VALID_STATUSES:
        result.status = "unknown"
    result.available = list(result.available or [])
    return result


def _summarize(labels: list[str]) -> str:
    shown = labels[:MAX_LABELS_IN_MESSAGE]
    text = "; ".join(shown)
    if len(labels) > len(shown):
        text += f"; +{len(labels) - len(shown)} more"
    return text


def _apply_result(item_id: int, result: CheckResult, duration_ms: int) -> _Outcome | None:
    """Persist a check result: item state, CheckEvent, Notification. Returns delivery info."""
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if item is None:  # deleted while the check was running
            return None
        now = utcnow()
        prev_status = item.status
        prev_keys = list(item.available_keys or [])
        outcome = _Outcome()

        item.last_checked_at = now
        new_labels: list[str] = []
        went_out_of_stock = False

        if result.status == "error":
            # keep available_keys / last_result (no flapping alerts)
            item.consecutive_errors = (item.consecutive_errors or 0) + 1
            item.last_error = (result.error or result.status_text or "Unknown error")[:2000]
            item.status = "error"
            item.status_text = (result.status_text or result.error or "Error")[:500]
        else:
            item.consecutive_errors = 0
            item.last_error = None
            item.status = result.status
            item.status_text = (result.status_text or "")[:500]
            item.last_result = result.detail or {}
            if result.price is not None:
                item.price = result.price[:64]
            keys = []
            seen = set()
            for a in result.available:
                if a.key not in seen:
                    seen.add(a.key)
                    keys.append(a.key)
            prev_set = set(prev_keys)
            new_labels = [a.label for a in result.available if a.key not in prev_set]
            # dedupe labels for repeated keys
            item.available_keys = keys
            went_out_of_stock = result.status == "out_of_stock" and (
                prev_status == "in_stock" or bool(prev_keys)
            )
            if not item.image_path and result.image_url and images.should_auto_fetch(item.id):
                outcome.fetch_image_url = result.image_url

        changed = item.status != prev_status or bool(new_labels)
        if item.status != prev_status or new_labels:
            item.last_change_at = now

        db.add(
            CheckEvent(
                item_id=item.id,
                checked_at=now,
                status=item.status,
                status_text=item.status_text or "",
                error=result.error if result.status == "error" else None,
                duration_ms=duration_ms,
                changed=changed,
            )
        )
        db.flush()
        # prune history to the newest KEEP_EVENTS_PER_ITEM events
        cutoff = db.execute(
            select(CheckEvent.id)
            .where(CheckEvent.item_id == item.id)
            .order_by(CheckEvent.id.desc())
            .offset(KEEP_EVENTS_PER_ITEM)
            .limit(1)
        ).scalar()
        if cutoff is not None:
            db.execute(delete(CheckEvent).where(CheckEvent.item_id == item.id, CheckEvent.id <= cutoff))

        settings = db.execute(select(UserSettings).where(UserSettings.user_id == item.user_id)).scalar_one_or_none()
        notify_oos = bool(settings and settings.notify_on_out_of_stock)

        notif: Notification | None = None
        tags = ("apple",) if item.kind == "apple" else ("shopping_cart",)
        if new_labels:
            notif = Notification(
                user_id=item.user_id,
                item_id=item.id,
                title=f"Restock alert: {item.name}"[:300],
                message=_summarize(new_labels),
                url=item.url,
                image_url=item.image_url,
                created_at=now,
            )
        elif went_out_of_stock and notify_oos:
            notif = Notification(
                user_id=item.user_id,
                item_id=item.id,
                title=f"Out of stock: {item.name}"[:300],
                message=item.status_text or "No longer available",
                url=item.url,
                image_url=item.image_url,
                created_at=now,
            )
            tags = ("x",)

        if notif is not None:
            db.add(notif)
            db.flush()
            outcome.notification_id = notif.id
            outcome.title = notif.title
            outcome.message = notif.message
            outcome.click_url = item.url
            outcome.tags = tags
            if item.notify_enabled and settings and (settings.ntfy_topic or "").strip():
                outcome.send = True
                outcome.target = _NtfyTarget(
                    settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token, settings.ntfy_priority
                )
            # trim old notifications for this user
            cutoff_n = db.execute(
                select(Notification.id)
                .where(Notification.user_id == item.user_id)
                .order_by(Notification.id.desc())
                .offset(KEEP_NOTIFICATIONS_PER_USER)
                .limit(1)
            ).scalar()
            if cutoff_n is not None:
                db.execute(
                    delete(Notification).where(
                        Notification.user_id == item.user_id, Notification.id <= cutoff_n
                    )
                )
        db.commit()
        return outcome


def _record_delivery(notification_id: int, ok: bool, error: str | None) -> None:
    with SessionLocal() as db:
        n = db.get(Notification, notification_id)
        if n is None:
            return
        n.delivered = bool(ok)
        n.delivery_error = None if ok else (error or "Delivery failed")[:1000]
        db.commit()


# ------------------------------------------------------------------- the check
async def check_item(item_id: int, *, wait: bool = True) -> bool:
    """Run one check for an item and persist/notify. Shared by the loop and the API.

    If a check for this item is already running, wait for it (wait=True) or return
    immediately; never runs the same item twice concurrently. Returns True if this
    call performed the check.
    """
    running = _inflight.get(item_id)
    if running is not None:
        if wait:
            await running.wait()
        return False

    done = asyncio.Event()
    _inflight[item_id] = done
    try:
        snap = await asyncio.to_thread(_load_snapshot, item_id)
        if snap is None:
            return False
        started = time.monotonic()
        try:
            result = _normalize(
                await asyncio.wait_for(
                    run_check(snap.kind, snap.url, snap.generic_config, snap.apple_config),
                    CHECK_TIMEOUT_SECONDS,
                )
            )
        except asyncio.TimeoutError:
            result = CheckResult(status="error", status_text="Check timed out", error="Check timed out")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - checkers shouldn't raise, but be safe
            log.exception("checker raised for item %s", item_id)
            msg = f"{type(e).__name__}: {e}"[:500]
            result = CheckResult(status="error", status_text=msg, error=msg)
        duration_ms = int((time.monotonic() - started) * 1000)

        outcome = await asyncio.to_thread(_apply_result, item_id, result, duration_ms)
        if outcome is None:
            return True

        if outcome.notification_id is not None and outcome.send and outcome.target is not None:
            ok, err = await send_ntfy(
                outcome.target,
                outcome.title,
                outcome.message,
                outcome.click_url,
                outcome.tags,
                outcome.target.ntfy_priority,
            )
            if not ok:
                log.warning("ntfy delivery failed for item %s: %s", item_id, err)
            await asyncio.to_thread(_record_delivery, outcome.notification_id, ok, err)

        if outcome.fetch_image_url:
            try:
                await images.attach_image_from_url(item_id, outcome.fetch_image_url)
            except Exception:  # noqa: BLE001
                log.exception("auto image fetch failed for item %s", item_id)
        return True
    finally:
        _inflight.pop(item_id, None)
        done.set()


# ------------------------------------------------------------------------ loop
def _jitter_seconds(item_id: int) -> float:
    return ((item_id * 2654435761) % 5000) / 1000.0  # deterministic 0-5 s


def select_due_items() -> list[int]:
    now = utcnow()
    floor = get_settings().min_interval_seconds
    with SessionLocal() as db:
        rows = db.execute(
            select(Item.id, Item.last_checked_at, Item.interval_minutes).where(Item.enabled.is_(True))
        ).all()
    due: list[int] = []
    for item_id, last, interval in rows:
        if last is None:
            due.append(item_id)
            continue
        seconds = max((interval or 2) * 60, floor)
        if now >= last + timedelta(seconds=seconds + _jitter_seconds(item_id)):
            due.append(item_id)
    return due


async def _run_limited(item_id: int) -> None:
    try:
        assert _semaphore is not None
        async with _semaphore:
            await check_item(item_id, wait=False)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("check for item %s failed", item_id)
    finally:
        _queued.discard(item_id)


async def tick() -> int:
    """One scheduler pass: start checks for all due items. Returns how many were started."""
    due = await asyncio.to_thread(select_due_items)
    started = 0
    for item_id in due:
        if item_id in _queued or item_id in _inflight:
            continue
        _queued.add(item_id)
        spawn(_run_limited(item_id))
        started += 1
    return started


async def _loop(stop: asyncio.Event) -> None:
    from .security import purge_expired_sessions

    last_purge = 0.0
    while not stop.is_set():
        try:
            await tick()
            if time.monotonic() - last_purge > 3600:
                last_purge = time.monotonic()
                await asyncio.to_thread(purge_expired_sessions)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except asyncio.TimeoutError:
            pass


def start() -> None:
    global _loop_task, _stop_event, _semaphore
    if _loop_task is not None and not _loop_task.done():
        return
    _semaphore = asyncio.Semaphore(max(1, get_settings().check_concurrency))
    _stop_event = asyncio.Event()
    _queued.clear()
    _loop_task = asyncio.get_running_loop().create_task(_loop(_stop_event))
    log.info("scheduler started (concurrency=%s)", get_settings().check_concurrency)


async def stop() -> None:
    global _loop_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    tasks = [t for t in [_loop_task, *list(_background)] if t is not None]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _loop_task = None
    _stop_event = None
    _queued.clear()
    _inflight.clear()
    _background.clear()


def min_interval_minutes() -> int:
    return max(1, math.ceil(get_settings().min_interval_seconds / 60))
