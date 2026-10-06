"""Who may open the ``/ws/{channel}`` WebSocket, and which channels and rooms (CR1-07).

Until this, the endpoint accepted anyone, with no token, on any channel name, and let any
socket join, list or post into any collaboration room.

A browser cannot set an Authorization header on a WebSocket, so the access token travels
as the second subprotocol, ``new WebSocket(url, ["bearer", token])``, not in the URL,
where proxies log it. The user is resolved as for any request (``auth.get_current_user``:
unknown or disabled users are refused), and the socket is closed when the token expires
(``WebSocketManager.close_expired``); the client reconnects with a fresh one.

Channels are closed unless ``authorize`` lists them, and every one it lists is scoped to
the user's tenant. Broadcast channels with no tenant (``ranges``, ``exercises``, ``all``)
stay closed until something delivers on them per tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import WebSocket
from sqlalchemy.orm import Session

from . import auth
from .auth import CurrentUser
from .models import Exercise, Range, UserRole

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
    except Exception:  # noqa: BLE001 — an invalid token, an unknown or disabled user: no socket
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

    * ``range.<id>`` / ``exercise.<id>``: a range / exercise of the user's tenant;
    * ``tenant.<id>``: the user's own tenant;
    * ``system.*``: admins.
    """
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
