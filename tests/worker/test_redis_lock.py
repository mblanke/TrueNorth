"""The drift sweep's Redis lock (worker/redis_lock.py; range certification S5 review).

Replaces a session-level PostgreSQL advisory lock that pgbouncer's transaction pooling would
split across server connections. Shown against a fake Redis with a test-driven clock:
overlap refused, a killed holder's lock lapses after its TTL, only the owner releases, a
holder that lost its lock knows it, and no Redis means no sweep.
"""

from __future__ import annotations

from worker import range_drift
from worker.redis_lock import RedisLock

KEY, TTL = "truenorth:test:lock", 300_000


def test_a_second_holder_is_refused_while_the_first_holds_it(fake_redis):
    r = fake_redis
    first, second = RedisLock(r, KEY, TTL), RedisLock(r, KEY, TTL)
    assert first.acquire() and not second.acquire()
    assert first.release() and second.acquire()


def test_a_killed_holders_lock_lapses_after_its_ttl(fake_redis):
    r = fake_redis
    assert RedisLock(r, KEY, TTL).acquire()  # the holder dies here: no renew, no release
    r.now += TTL - 1
    assert not RedisLock(r, KEY, TTL).acquire()
    r.now += 1
    assert RedisLock(r, KEY, TTL).acquire()


def test_only_the_owner_releases_and_a_holder_that_lost_it_knows(fake_redis):
    r = fake_redis
    old = RedisLock(r, KEY, TTL)
    assert old.acquire()
    r.now += TTL  # expired (a stalled worker), and taken by the next sweep
    new = RedisLock(r, KEY, TTL)
    assert new.acquire()
    assert not old.renew() and old.lost.is_set()
    assert not old.release()  # its token is not the key's any more: the new holder keeps it
    assert r.get(KEY) == new.token.encode()


def test_renewal_keeps_a_live_holder_past_its_ttl(fake_redis):
    r = fake_redis
    lock = RedisLock(r, KEY, TTL)
    assert lock.acquire()
    for _ in range(5):
        r.now += TTL // 2
        assert lock.renew()
    assert not RedisLock(r, KEY, TTL).acquire()


def test_no_redis_means_no_lock(fake_redis):
    r = fake_redis
    r.down = True
    assert not RedisLock(r, KEY, TTL).acquire()


def test_the_drift_sweep_does_nothing_while_another_holds_the_lock(fake_redis, monkeypatch):
    other = range_drift.sweep_lock(fake_redis)
    assert other.acquire()
    monkeypatch.setattr(range_drift, "sweep", lambda lost=None: (_ for _ in ()).throw(AssertionError("ran")))
    assert range_drift.check_range_drift()["status"] == "skipped"
    other.release()


def test_the_drift_sweep_releases_its_lock_when_done(fake_redis, monkeypatch):
    monkeypatch.setattr(range_drift, "sweep", lambda lost=None: {"status": "ok"})
    assert range_drift.check_range_drift() == {"status": "ok"}
    assert fake_redis.get(range_drift.SWEEP_LOCK_KEY) is None
