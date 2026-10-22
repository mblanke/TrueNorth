"""Fixtures for worker task tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa


class FakeRedis:
    """The part of Redis a lock uses (SET NX PX, GET, the compare-and-PEXPIRE / -DEL scripts of
    worker/redis_lock.py), with a clock the test moves (``now`` ms) so expiry can be shown."""

    def __init__(self):
        self.now = 0
        self.data: dict[str, tuple[str, int]] = {}  # key -> (value, expires at ms)
        self.down = False

    def _live(self, key):
        if self.down:
            raise ConnectionError("redis down")
        got = self.data.get(key)
        if got is not None and got[1] <= self.now:
            del self.data[key]
            got = None
        return got

    def set(self, key, value, nx=False, px=None):
        if nx and self._live(key) is not None:
            return None
        self._live(key)
        self.data[key] = (value, self.now + int(px))
        return True

    def get(self, key):
        got = self._live(key)
        return got[0].encode() if got else None

    def eval(self, script, numkeys, key, token, *args):
        from worker import redis_lock

        got = self._live(key)
        if got is None or got[0] != token:
            return 0
        if script == redis_lock.RENEW:
            self.data[key] = (got[0], self.now + int(args[0]))
            return 1
        if script == redis_lock.RELEASE:
            del self.data[key]
            return 1
        raise AssertionError("unknown script")


@pytest.fixture
def fake_redis(monkeypatch):
    """A FakeRedis the drift sweep's lock uses instead of the broker's Redis."""
    from worker import range_drift

    client = FakeRedis()
    monkeypatch.setattr(range_drift, "redis_client", lambda: client)
    return client


@pytest.fixture
def lease_always_free(monkeypatch):
    """For tests that stub the worker's database: the range's lease (worker/fencing.py)
    is always free and taking or giving it back touches nothing. Tests of the lease
    itself use a real database (test_range_task_fencing.py)."""
    from worker import fencing

    monkeypatch.setattr(fencing, "_claim_lease", lambda db, range_id, holder: True)
    monkeypatch.setattr(fencing, "_release_lease", lambda db, range_id, holder: None)
    monkeypatch.setattr(fencing, "_extend_lease", lambda db, range_id, holder, seconds=None: True)
    monkeypatch.setattr(fencing, "_lease_holder", lambda db, range_id, lock=False: None)
    monkeypatch.setattr(fencing._Lease, "observe", lambda self, holder_now: "held")


@pytest.fixture(autouse=True)
def _no_maintenance_windows(request, monkeypatch):
    """Range tasks re-check maintenance windows at their entry (worker/maintenance_windows.py),
    reading two more tables first. Tests of the tasks' own behaviour stub their database
    reads in sequence, or build only the tables they need, so here no window is ever in
    effect. A test module that tests the re-check itself sets ``REAL_MAINTENANCE_WINDOWS =
    True`` (tests/worker/test_maintenance_windows_worker.py)."""
    if getattr(request.module, "REAL_MAINTENANCE_WINDOWS", False):
        return
    from worker import maintenance_windows

    monkeypatch.setattr(maintenance_windows, "at_entry", lambda *a, **k: None)


# ── A real (SQLite) worker database, for the range-task tests ────────────


@pytest.fixture
def requeued(monkeypatch):
    """Every re-queue (fencing.defer) is recorded here; none reaches a broker."""
    from celery.app.task import Task

    calls: list[dict] = []
    monkeypatch.setattr(Task, "apply_async", lambda self, *a, **k: calls.append({"task": self.name, **k}))
    return calls


def _sqlite_now(dbapi_conn, _record):
    """The worker's SQL is written for PostgreSQL (NOW()); give SQLite one."""
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A file SQLite database with the worker's range tables, and the tasks pointed at it."""
    from worker import fencing, tasks

    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = sa.create_engine(url, connect_args={"timeout": 30})
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE templates (id TEXT PRIMARY KEY, yaml TEXT)"))
        conn.execute(
            sa.text(
                "CREATE TABLE ranges (id TEXT PRIMARY KEY, name TEXT, template_id TEXT, state TEXT, provisioner_backend TEXT, "
                "provisioner_output TEXT, error_message TEXT, updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE range_snapshots (id TEXT PRIMARY KEY, range_id TEXT, snapshot_state TEXT, "
                "snapshot_data TEXT, range_state_at_snapshot TEXT, size_bytes INTEGER, updated_at TIMESTAMP)"
            )
        )
    fencing.range_leases.create(engine)
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield engine
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)
