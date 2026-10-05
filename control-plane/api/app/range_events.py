"""Range state changes, from the worker to the browser (S0 defect "notifications unheard").

The worker publishes ``{"id", "state", "error"}`` on Redis channel ``truenorth:range``
(``worker.tasks._notify_api``). The API's WebSocket manager subscribes to it
(``WebSocketManager.worker_handlers``, wired in main.py's lifespan), and ``relay`` hands
each event to the sockets of the range's own tenant, on channel ``ranges`` and
``range.<id>``. Only those three fields go out.

The socket is authenticated (``ws_user``): a browser cannot set an Authorization header
on a WebSocket, so the access token travels as the second subprotocol
(``new WebSocket(url, ["bearer", token])``), not in the URL, where proxies log it.
``authorize`` decides which channels a user may open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from . import auth
from .auth import CurrentUser
from .models import Range, UserRole
from .websocket_manager import MessageType, WebSocketManager

logger = logging.getLogger("truenorth.range_events")

WORKER_CHANNEL = "truenorth:range"
FIELDS = ("id", "state", "error")
SUBPROTOCOL = "bearer"


def bearer_token(websocket: WebSocket) -> str | None:
    """The token from ``Sec-WebSocket-Protocol: bearer, <token>``, else None."""
    parts = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    if len(parts) == 2 and parts[0] == SUBPROTOCOL and parts[1]:
        return parts[1]
    return None


async def ws_user(websocket: WebSocket, db: Session) -> CurrentUser | None:
    """The signed-in user behind a WebSocket handshake, or None (refuse it)."""
    try:
        if auth.AUTH_DISABLED:
            return await auth.get_current_user(None, auth._dev_identity(), db)
        token = bearer_token(websocket)
        if not token:
            return None
        identity = await auth.get_token_identity(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        return await auth.get_current_user(None, identity, db)
    except Exception:  # an invalid token, an unknown or disabled user: no socket
        return None


def authorize(channel: str, user: CurrentUser, db: Session) -> bool:
    """May ``user`` open ``channel``? ``range.<id>`` only for a range of their tenant."""
    if channel.startswith("range."):
        try:
            rid = uuid.UUID(channel[len("range.") :])
        except ValueError:
            return False
        tenant = db.query(Range.tenant_id).filter(Range.id == rid).scalar()
        return tenant is not None and str(tenant) == user.tenant_id
    role = user.role.value if isinstance(user.role, UserRole) else str(user.role)
    return WebSocketManager.authorize_channel(channel, user.tenant_id, role)


def _tenant_of(session_factory: Callable[[], Any], rid: uuid.UUID) -> str | None:
    with session_factory() as db:
        tenant = db.query(Range.tenant_id).filter(Range.id == rid).scalar()
    return str(tenant) if tenant else None


async def relay(manager: WebSocketManager, raw: str, session_factory: Callable[[], Any]) -> int:
    """Deliver one worker event to the range's tenant. Returns how many sockets got it."""
    try:
        msg = json.loads(raw)
        rid = uuid.UUID(str(msg["id"]))
    except (TypeError, ValueError, KeyError):
        logger.debug("dropped a malformed worker range event")
        return 0
    tenant = await asyncio.to_thread(_tenant_of, session_factory, rid)
    if tenant is None:
        return 0
    data = {k: msg.get(k) for k in FIELDS} | {"id": str(rid)}
    return await manager.deliver_to_tenant(tenant, ("ranges", f"range.{rid}"), MessageType.RANGE_STATE.value, data)
