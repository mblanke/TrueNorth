"""Push a freshly committed notification to its recipient's open sockets (issue #377).

``inbox.notify`` queues a ``(notification, user_id, payload)`` on the session; when that
session **commits**, the payloads go out as ``notification`` messages on the recipient's own
channel, ``user.<id>`` (``ws_auth.authorize`` lets only that user open it). Each one is checked
at commit: a notification whose row was rolled back (the whole transaction, or a savepoint
inside it) is no longer persistent and is skipped, so a rollback pushes nothing. Nothing is
cleared on rollback itself: a savepoint rollback fires the same event and would also throw
away notifications from before the savepoint, which are real.

``WebSocketManager.broadcast`` delivers to this worker's sockets and, when the Redis relay
is on, publishes for the sibling API workers (production runs several; a user's socket is
on only one of them). Without the relay only this worker's sockets are reached, so the bell
keeps polling as the fallback and nothing here is required for a notification to arrive.

Pushing never fails the request and never blocks it: it is scheduled on the event loop and
errors are logged. From code that is not on an AnyIO worker thread (a script, a Celery task)
there is nothing to schedule on, and the push is skipped.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

logger = logging.getLogger("truenorth.api.notifications.push")

PENDING_KEY = "tn_notification_push"
MESSAGE_TYPE = "notification"

_manager: Any = None
_tasks: set[asyncio.Task] = set()  # strong references: a bare create_task can be collected mid-flight


def set_manager(manager: Any) -> None:
    """The API's ``WebSocketManager`` (main.py calls this once); None turns pushing off."""
    global _manager
    _manager = manager


def user_channel(user_id: Any) -> str:
    return f"user.{user_id}"


def queue(db: Session, notification: Any, user_id: Any, payload: dict) -> None:
    """Hold a push until `db` commits. Dropped if it rolls back."""
    db.info.setdefault(PENDING_KEY, []).append((notification, user_id, payload))


async def _send(manager: Any, channel: str, payload: dict) -> None:
    try:
        await manager.broadcast(channel, MESSAGE_TYPE, payload)
    except Exception:  # noqa: BLE001 — a push is best effort; the bell polls
        logger.warning("Notification push to %s failed", channel, exc_info=True)


def _spawn(manager: Any, channel: str, payload: dict) -> None:
    def start() -> None:
        task = asyncio.get_running_loop().create_task(_send(manager, channel, payload))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:  # already on the event loop (an async route using the session directly)
        start()
        return
    try:
        from anyio import from_thread

        from_thread.run_sync(start)
    except Exception:  # noqa: BLE001 — not an AnyIO worker thread (script, Celery): no live push
        logger.debug("No event loop to push notifications on; the bell will poll")


def deliver(items: list[tuple[Any, Any, dict]]) -> None:
    """Send each queued push whose row is really in the database."""
    manager = _manager
    if manager is None:
        return
    for notification, user_id, payload in items:
        try:
            if not inspect(notification).persistent:
                continue  # its savepoint rolled back: there is nothing to tell anyone about
        except Exception:  # noqa: BLE001
            continue
        _spawn(manager, user_channel(user_id), payload)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    items = session.info.pop(PENDING_KEY, None)
    if items:
        try:
            deliver(items)
        except Exception:  # noqa: BLE001 — never turn a successful commit into an error
            logger.warning("Notification push failed", exc_info=True)
