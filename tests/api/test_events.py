"""Tests for the TrueNorth Range internal event bus."""

import asyncio
import pytest
from app.events import (
    Event,
    EventBus,
    EventType,
    audit_handler,
    metrics_handler,
    setup_event_bus,
    websocket_handler,
    xapi_handler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_type: EventType = EventType.RANGE_CREATED, **kw) -> Event:
    defaults = {"type": event_type, "data": {"id": "test-1"}}
    defaults.update(kw)
    return Event(**defaults)


# ---------------------------------------------------------------------------
# EventType enum tests
# ---------------------------------------------------------------------------

class TestEventTypeEnum:
    def test_all_values_are_strings(self):
        for member in EventType:
            assert isinstance(member.value, str)

    def test_range_events_present(self):
        range_events = [e for e in EventType if e.value.startswith("range.")]
        assert len(range_events) >= 7

    def test_exercise_events_present(self):
        ex_events = [e for e in EventType if e.value.startswith("exercise.")]
        assert len(ex_events) >= 5

    def test_scenario_events_present(self):
        sc_events = [e for e in EventType if e.value.startswith("scenario.")]
        assert len(sc_events) >= 4

    def test_objective_events_present(self):
        obj_events = [e for e in EventType if e.value.startswith("objective.")]
        assert len(obj_events) >= 2

    def test_user_events_present(self):
        user_events = [e for e in EventType if e.value.startswith("user.")]
        assert len(user_events) >= 2

    def test_system_events_present(self):
        sys_events = [e for e in EventType if e.value.startswith("system.")]
        assert len(sys_events) >= 2

    def test_ai_events_present(self):
        ai_events = [e for e in EventType if e.value.startswith("ai.")]
        assert len(ai_events) >= 2

    def test_total_event_count_minimum(self):
        assert len(EventType) >= 24


# ---------------------------------------------------------------------------
# Event dataclass tests
# ---------------------------------------------------------------------------

class TestEvent:
    def test_defaults(self):
        e = _make_event()
        assert e.id  # UUID generated
        assert e.timestamp  # ISO timestamp generated
        assert e.source == "api"
        assert e.tenant_id is None

    def test_custom_fields(self):
        e = _make_event(
            tenant_id="t-1",
            user_id="u-1",
            correlation_id="corr-abc",
            source="worker",
        )
        assert e.tenant_id == "t-1"
        assert e.user_id == "u-1"
        assert e.correlation_id == "corr-abc"
        assert e.source == "worker"

    def test_to_dict_serialises_type(self):
        e = _make_event(EventType.RANGE_READY)
        d = e.to_dict()
        assert d["type"] == "range.ready"
        assert isinstance(d["data"], dict)

    def test_from_dict_roundtrip(self):
        e = _make_event(EventType.EXERCISE_STARTED, data={"score": 99})
        d = e.to_dict()
        restored = Event.from_dict(d)
        assert restored.type == EventType.EXERCISE_STARTED
        assert restored.data["score"] == 99
        assert restored.id == e.id

    def test_from_dict_preserves_optional_fields(self):
        e = _make_event(tenant_id="t-2", user_id="u-5")
        restored = Event.from_dict(e.to_dict())
        assert restored.tenant_id == "t-2"
        assert restored.user_id == "u-5"


# ---------------------------------------------------------------------------
# EventBus core functionality
# ---------------------------------------------------------------------------

class TestEventBus:
    @pytest.mark.asyncio
    async def test_register_and_emit(self):
        """Handler registered with .on() is called when event is emitted."""
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.RANGE_CREATED, handler)
        evt = _make_event()
        await bus.emit(evt)

        assert len(received) == 1
        assert received[0].id == evt.id

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_event(self):
        """Multiple handlers for the same event type all fire."""
        bus = EventBus()
        calls = {"a": 0, "b": 0}

        async def handler_a(event: Event):
            calls["a"] += 1

        async def handler_b(event: Event):
            calls["b"] += 1

        bus.on(EventType.RANGE_READY, handler_a)
        bus.on(EventType.RANGE_READY, handler_b)
        await bus.emit(_make_event(EventType.RANGE_READY))

        assert calls["a"] == 1
        assert calls["b"] == 1

    @pytest.mark.asyncio
    async def test_global_handler_receives_all(self):
        """Handler registered with .on_all() fires for any event type."""
        bus = EventBus()
        received = []

        async def global_handler(event: Event):
            received.append(event.type)

        bus.on_all(global_handler)

        await bus.emit(_make_event(EventType.RANGE_CREATED))
        await bus.emit(_make_event(EventType.EXERCISE_STARTED))
        await bus.emit(_make_event(EventType.USER_LOGIN))

        assert EventType.RANGE_CREATED in received
        assert EventType.EXERCISE_STARTED in received
        assert EventType.USER_LOGIN in received
        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_handler_not_called_for_other_events(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.RANGE_CREATED, handler)
        await bus.emit(_make_event(EventType.RANGE_DESTROYED))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_off_removes_handler(self):
        bus = EventBus()
        calls = 0

        async def handler(event: Event):
            nonlocal calls
            calls += 1

        bus.on(EventType.RANGE_CREATED, handler)
        await bus.emit(_make_event())
        assert calls == 1

        bus.off(EventType.RANGE_CREATED, handler)
        await bus.emit(_make_event())
        assert calls == 1  # not called again

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_break_bus(self):
        bus = EventBus()
        ok_calls = 0

        async def bad_handler(event: Event):
            raise RuntimeError("boom")

        async def good_handler(event: Event):
            nonlocal ok_calls
            ok_calls += 1

        bus.on(EventType.RANGE_CREATED, bad_handler)
        bus.on(EventType.RANGE_CREATED, good_handler)
        await bus.emit(_make_event())

        assert ok_calls == 1  # good handler still ran

    @pytest.mark.asyncio
    async def test_start_and_shutdown_no_redis(self):
        bus = EventBus()
        await bus.start()
        assert bus._running is True
        await bus.shutdown()
        assert bus._running is False

    @pytest.mark.asyncio
    async def test_emit_no_handlers(self):
        """Emitting with no registered handlers should not raise."""
        bus = EventBus()
        await bus.emit(_make_event(EventType.AI_FLEET_NODE_DOWN))


# ---------------------------------------------------------------------------
# Pre-built handlers
# ---------------------------------------------------------------------------

class TestPrebuiltHandlers:
    @pytest.mark.asyncio
    async def test_audit_handler_runs(self):
        evt = _make_event()
        await audit_handler(evt)  # should not raise

    @pytest.mark.asyncio
    async def test_websocket_handler_runs(self):
        evt = _make_event(EventType.RANGE_READY)
        await websocket_handler(evt)

    @pytest.mark.asyncio
    async def test_xapi_handler_runs(self):
        evt = _make_event(EventType.EXERCISE_COMPLETED)
        await xapi_handler(evt)

    @pytest.mark.asyncio
    async def test_metrics_handler_runs(self):
        evt = _make_event()
        await metrics_handler(evt)


# ---------------------------------------------------------------------------
# setup_event_bus
# ---------------------------------------------------------------------------

class TestSetupEventBus:
    def test_wires_handlers(self):

        class FakeApp:
            class state:
                event_bus = None

        bus = setup_event_bus(FakeApp)
        assert FakeApp.state.event_bus is bus
        assert len(bus._global_handlers) >= 2  # audit + metrics
        assert EventType.RANGE_READY in bus._handlers
        assert EventType.EXERCISE_STARTED in bus._handlers