"""Low findings of the 2026-10-08 security sweep: probe errors, rate-limit keying."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest
from app import health
from app.middleware import RateLimitMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient

DSN_ERROR = 'connection to server at "pg-primary.internal" (10.20.0.5), port 5432 failed: password for user "tn"'


# ── /health/ready no longer reflects exception text ─────────────────────
def test_readiness_does_not_echo_dependency_errors(client):
    with patch("app.db.check_db_health", side_effect=RuntimeError(DSN_ERROR)):
        r = client.get("/health/ready")
    assert r.status_code == 503
    body = json.dumps(r.json())
    assert "pg-primary" not in body and "10.20.0.5" not in body and "password" not in body
    db = next(c for c in r.json()["components"] if c["name"] == "database")
    assert db["message"] == health.CHECK_FAILED


# ── the rate limiter keys per user, not per address only ────────────────
class _FakeRedis:
    """Enough of redis.asyncio for the sliding window: a sorted set per key."""

    def __init__(self):
        self.sets: dict[str, dict[str, float]] = {}

    def pipeline(self):
        redis, ops = self, []

        class Pipe:
            def zremrangebyscore(self, key, lo, hi):
                ops.append(lambda: [redis.sets.setdefault(key, {}).pop(m) for m, s in
                                    list(redis.sets.get(key, {}).items()) if lo <= s <= hi])

            def zadd(self, key, mapping):
                ops.append(lambda: redis.sets.setdefault(key, {}).update(mapping))

            def zcard(self, key):
                ops.append(lambda: len(redis.sets.get(key, {})))

            def expire(self, key, seconds):
                ops.append(lambda: True)

            async def execute(self):
                return [op() for op in ops]

        return Pipe()

    async def zrem(self, key, member):
        self.sets.get(key, {}).pop(member, None)


def _bearer(sub: str) -> dict:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return {"Authorization": f"Bearer {part({'alg': 'none'})}.{part({'sub': sub})}.x"}


@pytest.fixture
def limited():
    app = FastAPI()

    @app.post("/exercises/{eid}/objectives/{ref}/detections")
    def detect(eid: str, ref: str):
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, redis_client=_FakeRedis(), enabled=True, default_limit=100)
    return TestClient(app)


def test_one_student_hitting_the_detection_limit_does_not_block_a_classmate(limited):
    path = "/exercises/e1/objectives/o1/detections"
    codes = [limited.post(path, headers=_bearer("alice")).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429  # detections: 30/min per user
    assert limited.post(path, headers=_bearer("bob")).status_code == 200  # same address, own budget


def test_rotating_the_subject_runs_into_the_address_aggregate(limited):
    path = "/exercises/e1/objectives/o1/detections"
    codes = [limited.post(path, headers=_bearer(f"fake-{i}")).status_code for i in range(301)]
    assert codes[-1] == 429 and codes.count(200) == 300  # 10x the per-user limit per address
