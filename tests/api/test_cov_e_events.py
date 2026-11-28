# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/events.py: the Redis fan-out paths and the xAPI handler dispatch.

The Redis client is a fake object with the three methods the bus uses (publish, pubsub,
close) — the bus's own pub/sub logic is the unit under test, and conftest already forbids
a real broker. The LRS transport is stubbed the way tests/api/test_cmi5_lms.py stubs it:
``xapi.send_statement`` is the outbound call, and ``get_lms_backend`` is patched to a
recorder so no statement leaves the process.
"""

import asyncio
import json
import uuid

import pytest
from app import xapi
from app.events import REDIS_CHANNEL, Event, EventBus, EventType, xapi_handler

CHANNEL = REDIS_CHANNEL


class FakePubSub:
    def __init__(self, messages=()):
        self._messages = list(messages)
        self.calls: list[tuple[str, str]] = []

    async def subscribe(self, channel):
        self.calls.append(("subscribe", channel))

    async def unsubscribe(self, channel):
        self.calls.append(("unsubscribe", channel))

    async def listen(self):
        for m in self._messages:
            yield m
        while True:  # a live subscription never ends; shutdown() cancels the listener
            await asyncio.sleep(3600)


class FakeRedis:
    def __init__(self, messages=(), *, fail_publish=False, fail_pubsub=False):
        self.published: list[tuple[str, str]] = []
        self.closed = False
        self._fail_publish = fail_publish
        self._fail_pubsub = fail_pubsub
        self.pubsub_obj = FakePubSub(messages)

    def pubsub(self):
        if self._fail_pubsub:
            raise RuntimeError("no pubsub for you")
        return self.pubsub_obj

    async def publish(self, channel, payload):
        if self._fail_publish:
            raise RuntimeError("redis down")
        self.published.append((channel, payload))

    async def close(self):
        self.closed = True


def _event(event_type=EventType.RANGE_CREATED, **kw):
    data = {"id": "e-1", **kw.pop("data", {})}
    return Event(type=event_type, data=data, **kw)


async def _wait_until(predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# -- Redis fan-out -----------------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_redis_subscribes_and_shutdown_closes(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: fake)
    bus = EventBus("redis://example.invalid/0")
    await bus.start()
    assert bus._running is True
    # The listener subscribes on its own task; give it a beat.
    assert await _wait_until(lambda: ("subscribe", CHANNEL) in fake.pubsub_obj.calls)
    await bus.shutdown()
    assert bus._running is False
    assert fake.closed is True
    assert ("unsubscribe", CHANNEL) in fake.pubsub_obj.calls


@pytest.mark.asyncio
async def test_emit_publishes_the_serialised_event(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: fake)
    bus = EventBus("redis://example.invalid/0")
    await bus.start()
    await bus.shutdown()
    evt = _event(tenant_id="t-1")
    await bus.emit(evt)
    assert len(fake.published) == 1
    channel, payload = fake.published[0]
    assert channel == CHANNEL
    assert json.loads(payload)["id"] == evt.id


@pytest.mark.asyncio
async def test_a_publish_failure_does_not_break_emit():
    bus = EventBus()
    bus._redis = FakeRedis(fail_publish=True)
    await bus.emit(_event())  # must not raise


@pytest.mark.asyncio
async def test_redis_messages_are_replayed_to_local_handlers(monkeypatch):
    good = _event(EventType.EXERCISE_STARTED, user_id="u-1").to_dict()
    fake = FakeRedis(
        [
            {"type": "subscribe", "data": 1, "pattern": None},  # the ack, not an event
            {"type": "message", "data": json.dumps(good), "pattern": None},
            {"type": "message", "data": "{not json", "pattern": None},  # bad payload
            {"type": "message", "data": json.dumps({"type": "no.such.event"}), "pattern": None},
        ]
    )
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: fake)
    bus = EventBus("redis://example.invalid/0")
    seen: list[Event] = []
    bus.on_all(lambda e: _record(seen, e))
    await bus.start()
    ok = await _wait_until(lambda: len(seen) == 1)
    await bus.shutdown()
    assert ok, "the one well-formed message was never replayed"
    assert seen[0].type == EventType.EXERCISE_STARTED


async def _record(seen, event):
    seen.append(event)


@pytest.mark.asyncio
async def test_a_stopped_bus_ignores_its_messages():
    fake = FakeRedis([{"type": "message", "data": json.dumps(_event().to_dict()), "pattern": None}])
    bus = EventBus()
    bus._redis = fake
    bus._running = False
    seen: list[Event] = []

    async def handler(event):
        seen.append(event)

    bus.on_all(handler)
    await bus._subscribe_redis()  # returns at once instead of consuming the queue
    assert seen == []


@pytest.mark.asyncio
async def test_a_broken_pubsub_does_not_crash_the_subscriber():
    bus = EventBus()
    bus._redis = FakeRedis(fail_pubsub=True)
    await bus._subscribe_redis()  # logs "Redis subscriber crashed" and returns


# -- xapi_handler ------------------------------------------------------------


class RecordingLRS:
    def __init__(self, *, ok=True, explode=False):
        self.statements: list[dict] = []
        self._ok = ok
        self._explode = explode

    async def emit_statement(self, statement):
        if self._explode:
            raise RuntimeError("LRS unreachable")
        if self._ok:
            self.statements.append(statement)
        return self._ok  # a refused send is not a delivered one


@pytest.fixture
def lrs(monkeypatch):
    mem = RecordingLRS()
    monkeypatch.setattr(xapi, "get_lms_backend", lambda: mem)
    return mem


@pytest.mark.asyncio
async def test_an_event_without_a_user_sends_nothing(lrs):
    await xapi_handler(_event(EventType.EXERCISE_STARTED, data={"exercise_id": "e1"}))
    assert lrs.statements == []


@pytest.mark.asyncio
async def test_an_uninteresting_event_type_sends_nothing(lrs):
    await xapi_handler(_event(EventType.RANGE_READY, user_id="u-1"))
    assert lrs.statements == []


@pytest.mark.asyncio
async def test_exercise_started_becomes_a_launched_statement(lrs):
    run = str(uuid.uuid4())  # the exercise run is the registration, and only a UUID is one
    await xapi_handler(
        _event(
            EventType.EXERCISE_STARTED,
            user_id="u-9",
            data={"exercise_id": run, "exercise_name": "Phish hunt", "range_id": "r-1", "scenario_id": "s-1"},
        )
    )
    assert len(lrs.statements) == 1
    stmt = lrs.statements[0]
    assert stmt["verb"]["display"]["en"] == "launched"
    assert stmt["actor"]["account"]["name"] == "u-9"
    assert stmt["context"]["registration"] == run
    assert stmt["object"]["id"].endswith(f"/exercise/{run}")
    assert stmt["context"]["extensions"] == {
        "http://localhost:4200/xapi/extensions/range_id": "r-1",
        "http://localhost:4200/xapi/extensions/scenario_id": "s-1",
    }


@pytest.mark.asyncio
async def test_exercise_completed_carries_the_scaled_score(lrs):
    await xapi_handler(
        _event(
            EventType.EXERCISE_COMPLETED,
            user_id="u-9",
            data={"exercise_id": "ex-1", "exercise_name": "Phish hunt", "total_score": 80, "max_score": 100},
        )
    )
    result = lrs.statements[0]["result"]
    assert result["score"]["raw"] == 80 and result["score"]["scaled"] == 0.8
    assert lrs.statements[0]["verb"]["display"]["en"] == "completed"


@pytest.mark.asyncio
async def test_objective_achieved_passes_with_points(lrs):
    run = str(uuid.uuid4())
    await xapi_handler(
        _event(
            EventType.OBJECTIVE_ACHIEVED,
            user_id="u-9",
            data={"objective_id": "O1", "objective_name": "Detect the phish", "points": 5, "exercise_id": run},
        )
    )
    stmt = lrs.statements[0]
    assert stmt["verb"]["display"]["en"] == "passed"
    assert stmt["result"]["score"]["raw"] == 5
    assert stmt["context"]["registration"] == run
    assert stmt["context"]["extensions"]["http://localhost:4200/xapi/extensions/exercise_id"] == run


@pytest.mark.asyncio
async def test_login_and_logout_are_session_statements(lrs):
    await xapi_handler(_event(EventType.USER_LOGIN, user_id="u-9", data={"session_id": "sess-1"}))
    await xapi_handler(_event(EventType.USER_LOGOUT, user_id="u-9", data={"session_id": "sess-1"}))
    assert [s["verb"]["display"]["en"] for s in lrs.statements] == ["experienced", "terminated"]
    assert all(s["object"]["id"].endswith("/session/sess-1") for s in lrs.statements)


@pytest.mark.asyncio
async def test_the_user_id_in_the_data_is_used_when_the_event_has_none(lrs):
    await xapi_handler(_event(EventType.USER_LOGIN, data={"user_id": "u-from-data"}))
    assert lrs.statements[0]["actor"]["account"]["name"] == "u-from-data"


@pytest.mark.asyncio
async def test_a_failed_send_is_reported_not_raised(monkeypatch):
    mem = RecordingLRS(ok=False)
    monkeypatch.setattr(xapi, "get_lms_backend", lambda: mem)
    await xapi_handler(_event(EventType.EXERCISE_STARTED, user_id="u-9"))  # no raise
    assert mem.statements == []


@pytest.mark.asyncio
async def test_an_lrs_exception_does_not_escape_the_handler(monkeypatch):
    mem = RecordingLRS(explode=True)
    monkeypatch.setattr(xapi, "get_lms_backend", lambda: mem)
    await xapi_handler(_event(EventType.EXERCISE_STARTED, user_id="u-9"))  # no raise
    assert mem.statements == []
