"""A Redis lock for a periodic sweep that must never overlap itself (range_drift.py).

Why not a PostgreSQL advisory lock: production reaches PostgreSQL through pgbouncer in
transaction pooling (compose.prod.yml, ``POOL_MODE: transaction``), so a session-level
``pg_advisory_lock`` and its unlock can land on different server connections: the lock
leaks, and sweeps stop or overlap. A Redis key with a TTL has no such tie to a connection:

* ``acquire``: ``SET key token NX PX ttl``; a token per holder (never shared).
* ``renew``: a thread pushes the expiry forward every ``ttl / 4`` while the holder works,
  only while the key still holds its own token (compare-and-``PEXPIRE``, one Lua script).
  A holder that loses it (expired during a Redis outage, taken by another) sets ``lost``
  and the sweep stops before its next unit of work.
* ``release``: compare-and-``DEL`` (one Lua script): only the owner's token is deleted,
  never a later holder's.
* A killed holder renews nothing: its key expires ``ttl`` later and the next sweep runs.

When Redis cannot be reached the lock is **not** taken (``acquire`` returns False): a missed
drift sweep is harmless, an overlapping one is not.
"""

from __future__ import annotations

import logging
import threading
import uuid

logger = logging.getLogger("truenorth.worker.redis_lock")

RENEW = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
         "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end")
RELEASE = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
           "return redis.call('del', KEYS[1]) else return 0 end")


class RedisLock:
    def __init__(self, client, key: str, ttl_ms: int, *, renew_every: float | None = None):
        self.client, self.key, self.ttl_ms = client, key, int(ttl_ms)
        self.token = uuid.uuid4().hex
        self.renew_every = renew_every if renew_every is not None else self.ttl_ms / 4000
        self.held = False
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        try:
            self.held = bool(self.client.set(self.key, self.token, nx=True, px=self.ttl_ms))
        except Exception as exc:  # noqa: BLE001 -- no Redis: no lock, so no sweep
            logger.warning("lock %s not taken: Redis unavailable (%s)", self.key, type(exc).__name__)
            self.held = False
        return self.held

    def renew(self) -> bool:
        """Push the expiry forward if this holder still owns the key; else mark it lost."""
        try:
            ok = bool(self.client.eval(RENEW, 1, self.key, self.token, self.ttl_ms))
        except Exception as exc:  # noqa: BLE001 -- a blip: the TTL still covers the next try
            logger.warning("lock %s not renewed (%s)", self.key, type(exc).__name__)
            return True
        if not ok:
            logger.error("lock %s was lost (expired or taken); this holder stops", self.key)
            self.lost.set()
        return ok

    def release(self) -> bool:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if not self.held:
            return False
        self.held = False
        try:
            return bool(self.client.eval(RELEASE, 1, self.key, self.token))
        except Exception:  # noqa: BLE001 -- it expires on its own
            return False

    def _beat(self) -> None:
        while not self._stop.wait(self.renew_every):
            if not self.renew():
                return

    def __enter__(self) -> RedisLock:
        if self.acquire():
            self._thread = threading.Thread(target=self._beat, name=f"lock-{self.key}", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
