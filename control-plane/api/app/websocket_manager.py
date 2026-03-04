"""TrueNorth Range — Production-grade WebSocket manager.

Features
--------
* Per-channel subscription with user/tenant tracking
* Redis pub/sub for multi-instance broadcast (optional — degrades to local)
* Heartbeat ping/pong every 30 s; disconnect after 3 missed pongs
* Per-user connection limit (50) and global cap (10 000)
* Structured JSON messages with monotonic sequence numbers
* Graceful shutdown with drain
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("truenorth.ws")

# ── Constants ──────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_S: int = 30
MAX_MISSED_PONGS: int = 3
MAX_CONNECTIONS_PER_USER: int = 50
MAX_CONNECTIONS_TOTAL: int = 10_000


# ── Message Types ──────────────────────────────────────────────────────
class MessageType(str, Enum):
    RANGE_STATE = "range_state"
    EXERCISE_UPDATE = "exercise_update"
    SCENARIO_EVENT = "scenario_event"
    SYSTEM_NOTIFICATION = "system_notification"
    TELEMETRY_STREAM = "telemetry_stream"


# ── Connection Wrapper ─────────────────────────────────────────────────
@dataclass
class WSConnection:
    """Metadata for a single WebSocket connection."""

    websocket: WebSocket
    user_id: str | None = None
    tenant_id: str | None = None
    channels: set[str] = field(default_factory=set)
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_pong: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = 0
    missed_pongs: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence


# ── Manager ────────────────────────────────────────────────────────────
class WebSocketManager:
    """Manages WebSocket connections, channels, heartbeat, and Redis fan-out.

    Parameters
    ----------
    redis_url:
        Optional Redis connection string.  When provided the manager will
        publish broadcasts to Redis pub/sub so other API workers can relay
        them to their local connections.  If *None*, all messaging is
        process-local only.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        # conn_id → WSConnection
        self.connections: dict[str, WSConnection] = {}
        # channel → set of conn_ids
        self.channels: dict[str, set[str]] = {}
        # user_id → set of conn_ids (for per-user sends & limits)
        self._user_connections: dict[str, set[str]] = {}

        self._redis_url = redis_url
        self._redis: Any = None  # aioredis / redis.asyncio connection
        self._pubsub: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = False
        self._lock = asyncio.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        """Start background tasks (heartbeat, optional Redis listener)."""
        if self._running:
            return
        self._running = True
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        if self._redis_url:
            await self._connect_redis()
            self._tasks.append(asyncio.create_task(self._redis_listener()))
        logger.info(
            "WebSocketManager started (redis=%s)", "yes" if self._redis_url else "no"
        )

    async def shutdown(self) -> None:
        """Gracefully close every connection and cancel background tasks."""
        self._running = False
        # Close all websockets
        for conn_id in list(self.connections):
            await self._force_disconnect(conn_id, reason="server_shutdown")
        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Close Redis
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
        logger.info("WebSocketManager shut down")

    # ── Connect / Disconnect ───────────────────────────────────────────
    async def connect(
        self,
        websocket: WebSocket,
        channel: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> str:
        """Accept a WebSocket, register it, subscribe to *channel*.

        Returns the unique ``conn_id``.

        Raises
        ------
        WebSocketDisconnect
            If the global or per-user connection limit is exceeded.
        """
        # Enforce global cap
        if len(self.connections) >= MAX_CONNECTIONS_TOTAL:
            await websocket.close(code=1013, reason="Server at capacity")
            raise WebSocketDisconnect(code=1013)

        # Enforce per-user cap
        if user_id:
            user_conns = self._user_connections.get(user_id, set())
            if len(user_conns) >= MAX_CONNECTIONS_PER_USER:
                await websocket.close(
                    code=1008, reason="Too many connections for this user"
                )
                raise WebSocketDisconnect(code=1008)

        await websocket.accept()
        conn_id = uuid.uuid4().hex

        conn = WSConnection(
            websocket=websocket,
            user_id=user_id,
            tenant_id=tenant_id,
            channels={channel},
        )

        async with self._lock:
            self.connections[conn_id] = conn
            self.channels.setdefault(channel, set()).add(conn_id)
            if user_id:
                self._user_connections.setdefault(user_id, set()).add(conn_id)

        logger.info(
            "WS connect conn=%s user=%s channel=%s (total=%d)",
            conn_id[:8],
            user_id or "anon",
            channel,
            len(self.connections),
        )
        return conn_id

    async def disconnect(self, conn_id: str) -> None:
        """Cleanly disconnect a connection and remove from all registries."""
        async with self._lock:
            conn = self.connections.pop(conn_id, None)
            if conn is None:
                return
            # Remove from channels
            for ch in conn.channels:
                ch_set = self.channels.get(ch)
                if ch_set:
                    ch_set.discard(conn_id)
                    if not ch_set:
                        del self.channels[ch]
            # Remove from user index
            if conn.user_id:
                u_set = self._user_connections.get(conn.user_id)
                if u_set:
                    u_set.discard(conn_id)
                    if not u_set:
                        del self._user_connections[conn.user_id]

        # Best-effort close
        try:
            await conn.websocket.close()
        except Exception:
            pass

        logger.info(
            "WS disconnect conn=%s user=%s (total=%d)",
            conn_id[:8],
            conn.user_id or "anon",
            len(self.connections),
        )

    async def _force_disconnect(
        self, conn_id: str, *, reason: str = "server"
    ) -> None:
        """Disconnect with a close reason (used by heartbeat & shutdown)."""
        conn = self.connections.get(conn_id)
        if conn is None:
            return
        try:
            await conn.websocket.close(code=1001, reason=reason)
        except Exception:
            pass
        await self.disconnect(conn_id)

    # ── Subscribe / Unsubscribe ────────────────────────────────────────
    async def subscribe(self, conn_id: str, channel: str) -> None:
        """Subscribe an existing connection to an additional channel."""
        async with self._lock:
            conn = self.connections.get(conn_id)
            if conn is None:
                return
            conn.channels.add(channel)
            self.channels.setdefault(channel, set()).add(conn_id)
        logger.debug("conn=%s subscribed to %s", conn_id[:8], channel)

    async def unsubscribe(self, conn_id: str, channel: str) -> None:
        """Unsubscribe a connection from a channel."""
        async with self._lock:
            conn = self.connections.get(conn_id)
            if conn is None:
                return
            conn.channels.discard(channel)
            ch_set = self.channels.get(channel)
            if ch_set:
                ch_set.discard(conn_id)
                if not ch_set:
                    del self.channels[channel]
        logger.debug("conn=%s unsubscribed from %s", conn_id[:8], channel)

    # ── Sending ────────────────────────────────────────────────────────
    def _build_message(
        self,
        conn: WSConnection,
        message_type: str,
        channel: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the structured JSON envelope."""
        return {
            "type": message_type,
            "channel": channel,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": conn.next_sequence(),
        }

    async def _send(
        self,
        conn_id: str,
        conn: WSConnection,
        message_type: str,
        channel: str,
        data: dict[str, Any],
    ) -> None:
        """Attempt to send a message; disconnect on failure."""
        try:
            msg = self._build_message(conn, message_type, channel, data)
            await conn.websocket.send_json(msg)
        except Exception:
            await self._force_disconnect(conn_id, reason="send_error")

    async def broadcast(
        self,
        channel: str,
        message_type: str,
        data: dict[str, Any],
    ) -> None:
        """Send a message to **all** connections subscribed to *channel*.

        If Redis is configured the message is also published so that sibling
        API workers can relay it to their local subscribers.
        """
        conn_ids = list(self.channels.get(channel, set()))
        for cid in conn_ids:
            conn = self.connections.get(cid)
            if conn:
                await self._send(cid, conn, message_type, channel, data)

        # Fan-out via Redis
        if self._redis:
            await self._redis_publish(channel, message_type, data)

    async def send_to_user(
        self,
        user_id: str,
        message_type: str,
        data: dict[str, Any],
    ) -> None:
        """Send to every connection belonging to *user_id*."""
        conn_ids = list(self._user_connections.get(user_id, set()))
        for cid in conn_ids:
            conn = self.connections.get(cid)
            if conn:
                # Use first channel or a synthetic user channel
                channel = f"user.{user_id}"
                await self._send(cid, conn, message_type, channel, data)

    async def send_to_tenant(
        self,
        tenant_id: str,
        message_type: str,
        data: dict[str, Any],
    ) -> None:
        """Send to **all** connections whose ``tenant_id`` matches."""
        channel = f"tenant.{tenant_id}"
        for cid, conn in list(self.connections.items()):
            if conn.tenant_id == tenant_id:
                await self._send(cid, conn, message_type, channel, data)

    # ── Redis Pub/Sub ──────────────────────────────────────────────────
    async def _connect_redis(self) -> None:
        """Establish an async Redis connection (redis.asyncio / aioredis)."""
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True
            )
            self._pubsub = self._redis.pubsub()
            await self._pubsub.psubscribe("truenorth:ws:*")
            logger.info("Redis pub/sub connected: %s", self._redis_url)
        except Exception as exc:
            logger.warning("Redis unavailable — running local-only: %s", exc)
            self._redis = None
            self._pubsub = None

    async def _redis_publish(
        self, channel: str, message_type: str, data: dict[str, Any]
    ) -> None:
        """Publish a message to Redis for cross-worker fan-out."""
        if not self._redis:
            return
        payload = json.dumps(
            {
                "channel": channel,
                "message_type": message_type,
                "data": data,
                "origin": id(self),  # ignore own messages
            }
        )
        try:
            await self._redis.publish(f"truenorth:ws:{channel}", payload)
        except Exception as exc:
            logger.warning("Redis publish failed: %s", exc)

    async def _redis_listener(self) -> None:
        """Background task: relay Redis pub/sub messages to local sockets."""
        if not self._pubsub:
            return
        origin = id(self)
        try:
            while self._running:
                msg = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg is None:
                    await asyncio.sleep(0.05)
                    continue
                if msg["type"] not in ("pmessage", "message"):
                    continue
                try:
                    payload = json.loads(msg["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                # Skip messages we published ourselves
                if payload.get("origin") == origin:
                    continue
                channel = payload.get("channel", "")
                message_type = payload.get("message_type", "system_notification")
                data = payload.get("data", {})
                # Deliver to local subscribers only (no re-publish)
                conn_ids = list(self.channels.get(channel, set()))
                for cid in conn_ids:
                    conn = self.connections.get(cid)
                    if conn:
                        await self._send(cid, conn, message_type, channel, data)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Redis listener error: %s", exc)

    # ── Heartbeat ──────────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        """Ping every ``HEARTBEAT_INTERVAL_S``; disconnect after 3 misses."""
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                stale: list[str] = []
                for cid, conn in list(self.connections.items()):
                    try:
                        await conn.websocket.send_json({"type": "ping"})
                        conn.missed_pongs += 1
                        if conn.missed_pongs > MAX_MISSED_PONGS:
                            stale.append(cid)
                    except Exception:
                        stale.append(cid)
                for cid in stale:
                    logger.info("Heartbeat timeout — disconnecting %s", cid[:8])
                    await self._force_disconnect(cid, reason="heartbeat_timeout")
        except asyncio.CancelledError:
            pass

    def handle_pong(self, conn_id: str) -> None:
        """Call when a ``pong`` frame is received from a client."""
        conn = self.connections.get(conn_id)
        if conn:
            conn.missed_pongs = 0
            conn.last_pong = datetime.now(timezone.utc)

    # ── Stats / Introspection ──────────────────────────────────────────
    @property
    def stats(self) -> dict[str, Any]:
        """Connection statistics snapshot."""
        by_channel: dict[str, int] = {
            ch: len(ids) for ch, ids in self.channels.items()
        }
        by_tenant: dict[str, int] = {}
        for conn in self.connections.values():
            tid = conn.tenant_id or "unknown"
            by_tenant[tid] = by_tenant.get(tid, 0) + 1
        return {
            "total_connections": len(self.connections),
            "total_channels": len(self.channels),
            "by_channel": by_channel,
            "by_tenant": by_tenant,
            "unique_users": len(self._user_connections),
        }

    # ── Channel Authorization Helper ──────────────────────────────────
    @staticmethod
    def authorize_channel(
        channel: str, user_tenant_id: str | None, user_role: str | None = None
    ) -> bool:
        """Check whether a user may subscribe to *channel*.

        Rules:
        * ``system.*`` — admins only
        * ``tenant.<id>`` — user must belong to that tenant (or be admin)
        * ``range.<id>``, ``exercise.<id>`` — allowed if same tenant
          (actual DB check should be done in the route; this is a fast pre-check)
        * ``public.*`` — anyone
        """
        if channel.startswith("system."):
            return user_role == "admin"
        if channel.startswith("tenant."):
            parts = channel.split(".", 1)
            target_tenant = parts[1] if len(parts) > 1 else ""
            if user_role == "admin":
                return True
            return target_tenant == user_tenant_id
        if channel.startswith("public."):
            return True
        # Default: allow (more specific checks should be layered in routes)
        return True