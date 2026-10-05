"""Range state changes, from the worker to the browser (S0 defect "notifications unheard").

The worker publishes ``{"id", "state", "error"}`` on Redis channel ``truenorth:range``
(``worker.tasks._notify_api``). The API's WebSocket manager subscribes to it
(``WebSocketManager.worker_handlers``, wired in main.py's lifespan), and ``relay`` hands
each event to the sockets of the range's own tenant, on channel ``ranges`` and
``range.<id>``. Only those three fields go out.

The socket is authenticated (``ws_user``): a browser cannot set an Authorization header
on a WebSocket, so the access token travels as the second subprotocol
(``new WebSocket(url, ["bearer", token])``), not in the URL, where proxies log it.
``authorize`` decides which channels a user may open (closed by default), and
``room_allowed`` which collaboration rooms. A socket is closed when its token expires
(``WebSocketManager.close_expired``); the client reconnects with a fresh one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from sqlalchemy.orm import Session

from . import auth
from .auth import CurrentUser
from .models import Exercise, Range, UserRole
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


@dataclass(frozen=True)
class WsIdentity:
    user: CurrentUser
    expires_at: float | None  # the token's exp; None when AUTH_DISABLED (no token)


async def ws_user(websocket: WebSocket, db: Session) -> WsIdentity | None:
    """The signed-in user behind a WebSocket handshake, or None (refuse it)."""
    try:
        if auth.AUTH_DISABLED:
            return WsIdentity(await auth.get_current_user(None, auth._dev_identity(), db), None)
        token = bearer_token(websocket)
        if not token:
            return None
        claims = await auth.get_auth_backend().validate_token(token)
        user = await auth.get_current_user(None, auth.TokenPayload(**claims), db)
        exp = claims.get("exp")
        return WsIdentity(user, float(exp) if isinstance(exp, int | float) else None)
    except Exception:  # an invalid token, an unknown or disabled user: no socket
        return None


def _owned(db: Session, model, raw_id: str, user: CurrentUser) -> bool:
    try:
        oid = uuid.UUID(raw_id)
    except ValueError:
        return False
    tenant = db.query(model.tenant_id).filter(model.id == oid).scalar()
    return tenant is not None and str(tenant) == user.tenant_id


def authorize(channel: str, user: CurrentUser, db: Session) -> bool:
    """May ``user`` open ``channel``? Closed unless listed here.

    * ``ranges``: range events, delivered only for the user's tenant (``relay``);
    * ``range.<id>`` / ``exercise.<id>``: a range / exercise of the user's tenant (the
      ops center broadcasts injects and commands on ``exercise.<id>``);
    * ``tenant.<id>``: the user's own tenant; ``system.*``: admins.
    """
    if channel == "ranges":
        return True
    if channel.startswith("range."):
        return _owned(db, Range, channel[len("range.") :], user)
    if channel.startswith("exercise."):
        return _owned(db, Exercise, channel[len("exercise.") :], user)
    if channel.startswith("tenant."):
        return channel[len("tenant.") :] == user.tenant_id
    if channel.startswith("system."):
        return user.role == UserRole.admin
    return False


def room_allowed(room_id: str, user: CurrentUser, db: Session) -> bool:
    """Collaboration rooms are exercises: only one of the user's tenant."""
    return _owned(db, Exercise, room_id, user)


def room_message_type(requested: object) -> str:
    """A client may only send room_* frames into a room, never e.g. an inject."""
    return requested if isinstance(requested, str) and requested.startswith("room_") else "room_chat"


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
