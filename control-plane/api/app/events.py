"""TrueNorth Range - Internal event bus for decoupled service communication.

Events flow: API handlers -> Event Bus -> Handlers (WebSocket, telemetry, audit, xAPI, notifications)
Backed by Redis pub/sub for multi-instance support.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

logger = logging.getLogger("truenorth.events")

REDIS_CHANNEL = "truenorth:events"


class EventType(str, Enum):
    """All domain events recognised by TrueNorth Range."""

    # Range events
    RANGE_CREATED = "range.created"
    RANGE_PROVISIONING = "range.provisioning"
    RANGE_READY = "range.ready"
    RANGE_FAILED = "range.failed"
    RANGE_DESTROYING = "range.destroying"
    RANGE_DESTROYED = "range.destroyed"
    RANGE_STOPPED = "range.stopped"

    # Exercise events
    EXERCISE_CREATED = "exercise.created"
    EXERCISE_STARTED = "exercise.started"
    EXERCISE_PAUSED = "exercise.paused"
    EXERCISE_COMPLETED = "exercise.completed"
    EXERCISE_SCORED = "exercise.scored"

    # Scenario events
    SCENARIO_PHASE_START = "scenario.phase.start"
    SCENARIO_PHASE_END = "scenario.phase.end"
    SCENARIO_INJECT_FIRED = "scenario.inject.fired"
    SCENARIO_COMPLETED = "scenario.completed"

    # Objective events
    OBJECTIVE_ACHIEVED = "objective.achieved"
    OBJECTIVE_FAILED = "objective.failed"

    # User events
    USER_LOGIN = "user.login"
    USER_LOGOUT = "user.logout"

    # System events
    SYSTEM_ALERT = "system.alert"
    SYSTEM_HEALTH_DEGRADED = "system.health.degraded"

    # AI events
    AI_GENERATION_COMPLETE = "ai.generation.complete"
    AI_FLEET_NODE_DOWN = "ai.fleet.node_down"


@dataclass
class Event:
    """Immutable domain event transported by the bus."""

    type: EventType
    data: dict
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = "api"
    tenant_id: str | None = None
    user_id: str | None = None
    correlation_id: str | None = None

    def to_dict(self) -> dict:
        """Serialise event to a plain dictionary (JSON-safe)."""
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> Event:
        """Deserialise an event from a dictionary."""
        raw = dict(raw)  # shallow copy
        raw["type"] = EventType(raw["type"])
        return cls(**raw)


EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """In-process event bus with optional Redis pub/sub fan-out.

    Usage::

        bus = EventBus()
        bus.on(EventType.RANGE_READY, my_handler)
        await bus.emit(Event(type=EventType.RANGE_READY, data={"id": "r-1"}))
    """

    def __init__(self, redis_url: str | None = None):
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []
        self._redis_url = redis_url
        self._redis = None
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on(self, event_type: EventType, handler: EventHandler) -> None:
        """Register *handler* to be called when *event_type* is emitted."""
        self._handlers.setdefault(event_type, []).append(handler)
        logger.debug("Registered handler %s for %s", handler.__name__, event_type.value)

    def on_all(self, handler: EventHandler) -> None:
        """Register *handler* to be called for **every** event."""
        self._global_handlers.append(handler)
        logger.debug("Registered global handler %s", handler.__name__)

    def off(self, event_type: EventType, handler: EventHandler) -> None:
        """Remove a previously-registered handler."""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    async def emit(self, event: Event) -> None:
        """Emit *event* to local handlers and optionally publish to Redis."""
        logger.info(
            "Event emitted: %s  id=%s source=%s",
            event.type.value,
            event.id,
            event.source,
        )

        # Dispatch to local handlers
        await self._dispatch_local(event)

        # Fan-out via Redis when available
        if self._redis is not None:
            await self._publish_redis(event)

    async def _dispatch_local(self, event: Event) -> None:
        """Call all matching local handlers, catching per-handler errors."""
        handlers = list(self._global_handlers) + list(self._handlers.get(event.type, []))
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "Handler %s failed for event %s",
                    handler.__name__,
                    event.type.value,
                )

    # ------------------------------------------------------------------
    # Redis pub/sub
    # ------------------------------------------------------------------

    async def _publish_redis(self, event: Event) -> None:
        """Publish serialised event to the Redis channel."""
        try:
            payload = json.dumps(event.to_dict())
            await self._redis.publish(REDIS_CHANNEL, payload)
        except Exception:
            logger.exception("Failed to publish event %s to Redis", event.id)

    async def _subscribe_redis(self) -> None:
        """Background loop: subscribe to Redis and replay to local handlers."""
        try:
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(REDIS_CHANNEL)
            logger.info("Redis subscription active on channel %s", REDIS_CHANNEL)
            async for message in self._pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                try:
                    raw = json.loads(message["data"])
                    event = Event.from_dict(raw)
                    await self._dispatch_local(event)
                except Exception:
                    logger.exception("Error processing Redis message")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Redis subscriber crashed")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the event bus.  If *redis_url* was provided, connect and
        begin the subscription listener."""
        self._running = True

        if self._redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                self._listener_task = asyncio.create_task(self._subscribe_redis())
                logger.info("Event bus started with Redis at %s", self._redis_url)
            except ImportError:
                logger.warning("redis package not installed; running in local-only mode")
            except Exception:
                logger.exception("Could not connect to Redis; local-only mode")
        else:
            logger.info("Event bus started (local-only, no Redis)")

    async def shutdown(self) -> None:
        """Gracefully shut down the event bus."""
        self._running = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        if self._pubsub:
            await self._pubsub.unsubscribe(REDIS_CHANNEL)
        if self._redis:
            await self._redis.close()
        logger.info("Event bus shut down")


# ======================================================================
# Singleton convenience instance
# ======================================================================

event_bus = EventBus()


# ======================================================================
# Pre-built handlers
# ======================================================================


async def audit_handler(event: Event) -> None:
    """Log every event to the audit trail (structured JSON)."""
    logger.info(
        "AUDIT %s | tenant=%s user=%s | %s",
        event.type.value,
        event.tenant_id,
        event.user_id,
        json.dumps(event.data, default=str),
    )


_WS_EVENT_TYPES = {
    EventType.RANGE_READY,
    EventType.RANGE_FAILED,
    EventType.RANGE_PROVISIONING,
    EventType.RANGE_DESTROYING,
    EventType.RANGE_DESTROYED,
    EventType.EXERCISE_STARTED,
    EventType.EXERCISE_COMPLETED,
    EventType.OBJECTIVE_ACHIEVED,
    EventType.SYSTEM_ALERT,
}


async def websocket_handler(event: Event) -> None:
    """Forward relevant events to WebSocket subscribers."""
    if event.type not in _WS_EVENT_TYPES:
        return
    logger.debug("WS broadcast: %s", event.type.value)
    # In production: call websocket_manager.broadcast(event.to_dict())


_XAPI_EVENT_TYPES = {
    EventType.EXERCISE_STARTED,
    EventType.EXERCISE_COMPLETED,
    EventType.OBJECTIVE_ACHIEVED,
    EventType.USER_LOGIN,
    EventType.USER_LOGOUT,
}


async def xapi_handler(event: Event) -> None:
    """Generate xAPI statements from training-relevant events and POST to LRS."""
    if event.type not in _XAPI_EVENT_TYPES:
        return

    from . import xapi

    data = event.data
    user_email = data.get("user_email", "unknown@truenorth.local")
    user_name = data.get("user_name", "Unknown")

    stmt = None
    try:
        if event.type == EventType.EXERCISE_STARTED:
            stmt = xapi.exercise_launched(
                user_email=user_email,
                user_name=user_name,
                exercise_id=data.get("exercise_id", ""),
                exercise_name=data.get("exercise_name", ""),
                range_id=data.get("range_id", ""),
                scenario_id=data.get("scenario_id", ""),
            )
        elif event.type == EventType.EXERCISE_COMPLETED:
            stmt = xapi.exercise_completed(
                user_email=user_email,
                user_name=user_name,
                exercise_id=data.get("exercise_id", ""),
                exercise_name=data.get("exercise_name", ""),
                score=data.get("total_score", 0),
                max_score=data.get("max_score", 100),
            )
        elif event.type == EventType.OBJECTIVE_ACHIEVED:
            stmt = xapi.objective_achieved(
                user_email=user_email,
                user_name=user_name,
                objective_id=data.get("objective_id", ""),
                objective_name=data.get("objective_name", ""),
                points=data.get("points", 0),
            )
        elif event.type == EventType.USER_LOGIN:
            stmt = xapi.build_statement(
                "experienced",
                user_email,
                user_name,
                "session",
                data.get("session_id", ""),
                "User Login",
            )
        elif event.type == EventType.USER_LOGOUT:
            stmt = xapi.build_statement(
                "terminated",
                user_email,
                user_name,
                "session",
                data.get("session_id", ""),
                "User Logout",
            )

        if stmt:
            success = await xapi.send_statement(stmt)
            if success:
                logger.info("xAPI statement sent for %s", event.type.value)
            else:
                logger.warning("xAPI statement failed to send for %s (LRS may be unavailable)", event.type.value)
    except Exception:
        logger.exception("xAPI handler error for %s", event.type.value)


async def metrics_handler(event: Event) -> None:
    """Update Prometheus metrics counters from events."""
    logger.debug("Metric incremented: truenorth_event_%s", event.type.value)
    # In production: EVENTS_COUNTER.labels(event_type=event.type.value).inc()


# ======================================================================
# Application wiring helper
# ======================================================================


def setup_event_bus(app: Any, redis_url: str | None = None) -> EventBus:
    """Create, wire, and attach an :class:`EventBus` to the FastAPI *app*.

    Standard handlers are registered automatically:

    * **audit_handler** â€” logs every event
    * **websocket_handler** â€” pushes UI-relevant events to WS clients
    * **xapi_handler** â€” emits xAPI statements for LRS
    * **metrics_handler** â€” increments Prometheus counters
    """
    bus = EventBus(redis_url)

    # Global (all events)
    bus.on_all(audit_handler)
    bus.on_all(metrics_handler)

    # Targeted
    bus.on(EventType.RANGE_READY, websocket_handler)
    bus.on(EventType.RANGE_FAILED, websocket_handler)
    bus.on(EventType.RANGE_PROVISIONING, websocket_handler)
    bus.on(EventType.EXERCISE_STARTED, websocket_handler)
    bus.on(EventType.EXERCISE_COMPLETED, websocket_handler)
    bus.on(EventType.OBJECTIVE_ACHIEVED, websocket_handler)

    bus.on(EventType.EXERCISE_STARTED, xapi_handler)
    bus.on(EventType.EXERCISE_COMPLETED, xapi_handler)
    bus.on(EventType.OBJECTIVE_ACHIEVED, xapi_handler)

    app.state.event_bus = bus
    return bus
