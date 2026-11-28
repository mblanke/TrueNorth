# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scenario_engine.scoring.leaderboard: the in-memory leaderboard against a recording fake
Redis client — score updates with history, top-N ranking, team ranks including the missing
team, chronological history (both stores), clear(), and the constructor fallback to memory
when Redis is unavailable (no module, and a client whose ping fails). Redis is a fake: no
server, no network, no redis-py import in the fallback paths."""

from __future__ import annotations

import sys

import pytest
from scenario_engine.scoring.leaderboard import Leaderboard


class FakeRedis:
    """Sorted-set client that records calls and answers from one store."""

    def __init__(self, *, ping_fails=False):
        self.zsets: dict[str, dict[str, int]] = {}
        self.lists: dict[str, list[str]] = {}
        self.deleted: list[str] = []
        self.calls: list[tuple] = []
        self._ping_fails = ping_fails

    def ping(self):
        if self._ping_fails:
            raise ConnectionError("redis down")
        return True

    def zadd(self, key, mapping):
        self.calls.append(("zadd", key, dict(mapping)))
        self.zsets.setdefault(key, {}).update(mapping)

    def rpush(self, key, value):
        self.calls.append(("rpush", key, value))
        self.lists.setdefault(key, []).append(value)

    def zrevrange(self, key, start, stop, withscores=False):
        self.calls.append(("zrevrange", key, start, stop, withscores))
        board = self.zsets.get(key, {})
        ranked = sorted(board.items(), key=lambda kv: kv[1], reverse=True)[start : stop + 1]
        return [(tid, float(sc)) for tid, sc in ranked]

    def zrevrank(self, key, member):
        self.calls.append(("zrevrank", key, member))
        board = self.zsets.get(key, {})
        ranked = sorted(board.items(), key=lambda kv: kv[1], reverse=True)
        for i, (tid, _) in enumerate(ranked):
            if tid == member:
                return i
        return None

    def lrange(self, key, start, stop):
        self.calls.append(("lrange", key, start, stop))
        return list(self.lists.get(key, []))

    def delete(self, key):
        self.calls.append(("delete", key))
        self.deleted.append(key)
        self.zsets.pop(key, None)


def test_inmemory_update_rank_history_and_clear():
    lb = Leaderboard()  # no URL: pure in-memory, no redis import attempted
    assert lb._redis is None
    import asyncio

    asyncio.run(lb.update_score("ex-1", "red", 50))
    asyncio.run(lb.update_score("ex-1", "blue", 80))
    asyncio.run(lb.update_score("ex-1", "gold", 80))
    asyncio.run(lb.update_score("ex-1", "red", 70))  # an update overwrites the score

    board = asyncio.run(lb.get_leaderboard("ex-1"))
    assert board == [
        {"rank": 1, "team_id": "blue", "score": 80},
        {"rank": 2, "team_id": "gold", "score": 80},
        {"rank": 3, "team_id": "red", "score": 70},
    ]
    # top_n truncates after sorting.
    assert [row["team_id"] for row in asyncio.run(lb.get_leaderboard("ex-1", top_n=2))] == ["blue", "gold"]

    assert asyncio.run(lb.get_team_rank("ex-1", "blue")) == 1
    assert asyncio.run(lb.get_team_rank("ex-1", "red")) == 3
    assert asyncio.run(lb.get_team_rank("ex-1", "stranger")) == 0
    assert asyncio.run(lb.get_team_rank("other-ex", "blue")) == 0  # unknown exercise

    history = asyncio.run(lb.get_historical("red"))
    assert [h["exercise_id"] for h in history] == ["ex-1", "ex-1"]
    assert [h["score"] for h in history] == [50, 70]
    assert all(isinstance(h["timestamp"], int) for h in history)
    assert asyncio.run(lb.get_historical("stranger")) == []

    asyncio.run(lb.clear("ex-1"))
    assert asyncio.run(lb.get_leaderboard("ex-1")) == []
    assert asyncio.run(lb.get_team_rank("ex-1", "blue")) == 0
    assert asyncio.run(lb.get_historical("red")) != []  # history survives the clear


def test_redis_store_records_the_sorted_set_calls():
    import asyncio

    client = FakeRedis()
    lb = Leaderboard(key_prefix="lb:test")
    lb._redis = client  # inject the fake directly: no server, no import
    asyncio.run(lb.update_score("ex-9", "red", 50))
    asyncio.run(lb.update_score("ex-9", "blue", 80))

    assert client.zsets == {"lb:test:ex-9": {"red": 50, "blue": 80}}
    assert ("zadd", "lb:test:ex-9", {"red": 50}) in client.calls
    history_pushes = [c for c in client.calls if c[0] == "rpush"]
    assert [c[1] for c in history_pushes] == ["lb:test:history:red", "lb:test:history:blue"]
    exercise_id, score, timestamp = history_pushes[0][2].split(":")
    assert (exercise_id, score) == ("ex-9", "50") and timestamp.isdigit()

    assert asyncio.run(lb.get_leaderboard("ex-9")) == [
        {"rank": 1, "team_id": "blue", "score": 80},
        {"rank": 2, "team_id": "red", "score": 50},
    ]
    assert asyncio.run(lb.get_team_rank("ex-9", "red")) == 2
    assert asyncio.run(lb.get_team_rank("ex-9", "stranger")) == 0  # zrevrank None -> 0
    assert ("zrevrank", "lb:test:ex-9", "stranger") in client.calls

    asyncio.run(lb.clear("ex-9"))
    assert client.deleted == ["lb:test:ex-9"]
    assert asyncio.run(lb.get_leaderboard("ex-9")) == []


def test_redis_history_parses_the_pushed_entries():
    import asyncio

    client = FakeRedis()
    client.lists["lb:test:history:red"] = ["ex-1:50:1700000000", "ex-2:70:1700000100"]
    lb = Leaderboard(key_prefix="lb:test")
    lb._redis = client
    assert asyncio.run(lb.get_historical("red")) == [
        {"exercise_id": "ex-1", "score": 50, "timestamp": 1700000000},
        {"exercise_id": "ex-2", "score": 70, "timestamp": 1700000100},
    ]
    # Malformed entries (wrong part count) are dropped, never crash the read.
    client.lists["lb:test:history:blue"] = ["not-a-triple", "ex-1:30:1700000002"]
    assert asyncio.run(lb.get_historical("blue")) == [
        {"exercise_id": "ex-1", "score": 30, "timestamp": 1700000002},
    ]


def test_constructor_falls_back_to_memory_when_ping_fails(caplog, monkeypatch):
    client = FakeRedis(ping_fails=True)
    fake_module = type(sys)("redis_fake_for_cov_f")
    fake_module.from_url = lambda url, decode_responses=False: client
    monkeypatch.setitem(sys.modules, "redis", fake_module)

    import logging

    with caplog.at_level(logging.WARNING, logger="scenario_engine.scoring.leaderboard"):
        lb = Leaderboard("redis://nope:6379/0")
    assert lb._redis is None  # the failed ping downgrades to the in-memory store
    assert any("using in-memory fallback" in r.message for r in caplog.records)

    # The fallback really is usable: scores land in memory, not in the fake client.
    import asyncio

    asyncio.run(lb.update_score("ex-1", "red", 10))
    assert client.zsets == {}
    assert asyncio.run(lb.get_leaderboard("ex-1")) == [{"rank": 1, "team_id": "red", "score": 10}]


def test_constructor_without_redis_imports_nothing(monkeypatch):
    # With no URL the redis package is never even imported — prove it by making the
    # import fail loudly if it happens.
    monkeypatch.setitem(sys.modules, "redis", None)
    lb = Leaderboard()
    assert lb._redis is None and lb.redis_url is None and lb.key_prefix == "truenorth:leaderboard"
    assert lb._key("ex-1") == "truenorth:leaderboard:ex-1"
    assert lb._history_key("red") == "truenorth:leaderboard:history:red"


def test_redis_unavailable_at_construction_is_caught(monkeypatch):
    def boom(url, decode_responses=False):
        raise RuntimeError("no route to host")

    fake_module = type(sys)("redis_fake_for_cov_f")
    fake_module.from_url = boom
    monkeypatch.setitem(sys.modules, "redis", fake_module)
    lb = Leaderboard("redis://nope:6379/0")
    assert lb._redis is None  # any connection failure leaves the memory store active


@pytest.mark.parametrize(
    "prefix,exercise,expected",
    [
        ("truenorth:leaderboard", "ex-1", "truenorth:leaderboard:ex-1"),
        ("custom", "x", "custom:x"),
    ],
)
def test_key_prefixes_are_honoured(prefix, exercise, expected):
    assert Leaderboard(key_prefix=prefix)._key(exercise) == expected
