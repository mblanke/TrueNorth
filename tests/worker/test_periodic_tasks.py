"""The scheduled health check and metrics collection.

Both used the worker's PROVISIONER_BACKEND for every range, so a vSphere range on a
worker configured for Proxmox was asked about by the Proxmox provisioner, and the
metrics were random numbers. These pin: each range is read by its own backend, one
provisioner (one hypervisor login) per backend per run, no invented numbers, and the
run's bounds (lock, budget, expiry). Run against SQLite through the tasks' own SQL.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from worker import celery_app, periodic, tasks
from worker.provisioners.hyperv import HypervProvisioner
from worker.provisioners.mock import MockProvisioner
from worker.provisioners.proxmox_api import ProxmoxAPIProvisioner
from worker.provisioners.results import HealthResult, MetricsResult
from worker.provisioners.terraform import TerraformProvisioner


def _run(coro):
    return asyncio.run(coro)


def _sqlite_now(dbapi_conn, _record):
    if hasattr(dbapi_conn, "create_function"):
        dbapi_conn.create_function("NOW", 0, lambda: "2026-10-03 00:00:00")


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE ranges (id TEXT PRIMARY KEY, name TEXT, state TEXT, provisioner_output TEXT,"
            " provisioner_backend TEXT, updated_at TEXT)"
        ))
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)

    def active():  # these test the runs, with readable ids; db_ops.active_ranges is tested on the
        with engine.connect() as conn:  # API's schema in test_worker_sql_real_db.py
            return conn.execute(sa.text("SELECT id, name, provisioner_output, provisioner_backend FROM ranges "
                                        "WHERE state IN ('ready', 'running')")).fetchall()

    monkeypatch.setattr(periodic, "_active_ranges", active)

    def run(sql, **params):
        with engine.begin() as conn:
            res = conn.execute(sa.text(sql), params)
            return res.fetchall() if sql.lstrip().upper().startswith("SELECT") else res

    yield run
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)
    engine.dispose()


def _range(db, rid, backend, state="ready", vms=("a", "b")):
    out = json.dumps({"vms": [{"name": f"{rid}-{v}", "vm_id": f"vm-{rid}-{v}"} for v in vms]})
    db("INSERT INTO ranges (id, name, state, provisioner_output, provisioner_backend, updated_at) "
       "VALUES (:id, :n, :s, :o, :b, 'before')", id=rid, n=f"Range {rid}", s=state, o=out, b=backend)


class FakeBackend:
    """A provisioner recording what it was asked, with scripted answers per range."""

    def __init__(self, name, health=None, metrics=None, delay=0.0):
        self.name = name
        self.health = health or {}
        self.metrics = metrics or {}
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.sessions = 0
        self.open = False

    def session(self):
        backend = self

        class Scope:
            async def __aenter__(self):
                backend.sessions += 1
                backend.open = True
                return backend

            async def __aexit__(self, *exc):
                backend.open = False

        return Scope()

    async def health_check(self, range_id, provision_output):
        assert self.open, "called outside the run's session"
        self.calls.append(("health", range_id))
        await asyncio.sleep(self.delay)
        answer = self.health.get(range_id, True)
        if isinstance(answer, Exception):
            raise answer
        return HealthResult(healthy=answer, status="ok" if answer else "degraded")

    async def collect_metrics(self, range_id, provision_output):
        assert self.open, "called outside the run's session"
        self.calls.append(("metrics", range_id))
        await asyncio.sleep(self.delay)
        answer = self.metrics.get(range_id)
        if isinstance(answer, Exception):
            raise answer
        return answer or MetricsResult(status="unsupported")


@pytest.fixture
def backends(monkeypatch):
    """tasks._get_backend hands out FakeBackends, one fresh instance per call, recorded."""
    made: list[FakeBackend] = []
    script: dict[str, dict] = {}

    def get(name=None):
        if name == "no_such_backend":
            raise ValueError(f"Unknown provisioner backend: {name!r}")
        backend = FakeBackend(name, **script.get(name, {}))
        made.append(backend)
        return backend

    monkeypatch.setattr(tasks, "_get_backend", get)
    monkeypatch.setenv("PROVISIONER_BACKEND", "proxmox_api")
    return SimpleNamespace(made=made, script=script, by=lambda n: [b for b in made if b.name == n])


@pytest.fixture
def notes(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(tasks, "_notify_api", lambda ch, msg: sent.append((ch, msg)))
    return sent


@pytest.fixture
def ingested(monkeypatch):
    events: list[tuple[str, list]] = []
    monkeypatch.setattr(tasks, "ingest_telemetry_batch",
                        SimpleNamespace(delay=lambda rid, evs: events.append((rid, evs))))
    return events


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """No Redis in unit tests unless a test installs a fake one."""
    monkeypatch.setitem(sys.modules, "redis", None)


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


class TestGroupByBackend:
    def test_each_range_goes_to_its_own_backend_and_the_env_only_fills_gaps(self, monkeypatch):
        monkeypatch.setenv("PROVISIONER_BACKEND", "proxmox_api")
        rows = [("r1", "A", None, "vsphere_api"), ("r2", "B", None, "mock"), ("r3", "C", None, None),
                ("r4", "D", None, "vsphere_api"), ("r5", "E", None, "")]
        groups = periodic.group_by_backend(rows)
        assert {k: [r[0] for r in v] for k, v in groups.items()} == {
            "vsphere_api": ["r1", "r4"], "mock": ["r2"], "proxmox_api": ["r3", "r5"]}

    def test_old_three_column_rows_fall_back_to_the_env(self, monkeypatch):
        monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
        assert list(periodic.group_by_backend([("r1", "A", None)])) == ["mock"]


# --------------------------------------------------------------------------- #
# Health check
# --------------------------------------------------------------------------- #


class TestHealthCheckRanges:
    def test_routes_by_range_backend_with_one_provisioner_and_session_each(self, db, backends, notes):
        for rid, backend in (("r1", "vsphere_api"), ("r2", "mock"), ("r3", None), ("r4", "vsphere_api")):
            _range(db, rid, backend)
        _range(db, "r5", "vsphere_api", state="destroyed")  # not active: never checked
        backends.script["vsphere_api"] = {"health": {"r4": False}}

        result = tasks.health_check_ranges()

        (vs,) = backends.by("vsphere_api")
        (mk,) = backends.by("mock")
        (px,) = backends.by("proxmox_api")  # r3 has no backend recorded: the env's
        assert sorted(vs.calls) == [("health", "r1"), ("health", "r4")]
        assert mk.calls == [("health", "r2")] and px.calls == [("health", "r3")]
        assert vs.sessions == mk.sessions == px.sessions == 1
        assert result["checked"] == 4 and result["healthy"] == 3 and result["unhealthy"] == 1
        assert result["unhealthy_ids"] == ["r4"] and result["skipped"] == 0
        assert result["by_backend"] == {"vsphere_api": 2, "mock": 1, "proxmox_api": 1}
        assert ("range", {"id": "r4", "event": "health_check_failed"}) in notes
        assert ("system", {"event": "health_check_alert", "unhealthy_count": 1, "unhealthy_ids": ["r4"]}) in notes

    def test_a_check_that_raises_is_unhealthy_and_the_rest_still_run(self, db, backends, notes):
        _range(db, "r1", "vsphere_api")
        _range(db, "r2", "vsphere_api")
        backends.script["vsphere_api"] = {"health": {"r1": RuntimeError("vCenter said no")}}
        result = tasks.health_check_ranges()
        assert result["unhealthy_ids"] == ["r1"] and result["healthy"] == 1
        # As before: an error is counted, not announced per range.
        assert not [n for n in notes if n[1].get("event") == "health_check_failed"]

    def test_an_unknown_backend_marks_its_ranges_only(self, db, backends, notes):
        _range(db, "r1", "no_such_backend")
        _range(db, "r2", "mock")
        result = tasks.health_check_ranges()
        assert result["unhealthy_ids"] == ["r1"] and result["healthy"] == 1

    def test_budget_times_out_a_slow_range_and_skips_the_rest(self, db, backends, notes):
        for rid in ("r1", "r2", "r3"):
            _range(db, rid, "vsphere_api")
        backends.script["vsphere_api"] = {"delay": 5.0}
        started = time.monotonic()
        result = periodic.health_check_run(time.monotonic() + 0.2)
        assert time.monotonic() - started < 2
        # The first range was asked and gave no answer in time: unhealthy. The others were
        # never reached: skipped, not reported as unhealthy.
        assert result["unhealthy"] == 1 and result["skipped"] == 2 and result["checked"] == 1

    def test_no_active_ranges(self, db, backends):
        assert tasks.health_check_ranges() == {"status": "ok", "checked": 0, "healthy": 0, "unhealthy": 0}
        assert backends.made == []


# --------------------------------------------------------------------------- #
# Run lock (skip while the previous run is going) and schedule expiry
# --------------------------------------------------------------------------- #


class FakeRedis:
    held: dict[str, float] = {}
    log: list[tuple] = []

    @classmethod
    def from_url(cls, url, **kw):
        cls.log.append(("connect", kw))
        return cls()

    def lock(self, name, timeout):
        outer = self

        class Lock:
            def acquire(self, blocking=True):
                outer.log.append(("acquire", name, timeout, blocking))
                if name in FakeRedis.held:
                    return False
                FakeRedis.held[name] = timeout
                return True

            def release(self):
                outer.log.append(("release", name))
                FakeRedis.held.pop(name, None)

        return Lock()


@pytest.fixture
def redis(monkeypatch):
    FakeRedis.held, FakeRedis.log = {}, []
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=FakeRedis))
    return FakeRedis


class TestRunLock:
    def test_a_run_is_skipped_while_the_previous_one_holds_the_lock(self, db, backends, redis):
        _range(db, "r1", "mock")
        redis.held["truenorth:periodic:health_check_ranges"] = 65
        assert tasks.health_check_ranges() == {"status": "skipped", "reason": "previous run still in progress"}
        assert backends.made == []  # nothing was read, no hypervisor login
        redis.held["truenorth:periodic:collect_range_metrics"] = 30
        assert tasks.collect_range_metrics()["status"] == "skipped"
        assert backends.made == []

    def test_the_lock_is_taken_without_waiting_expires_and_is_released(self, db, backends, redis, notes):
        _range(db, "r1", "mock")
        tasks.health_check_ranges()
        acquire = next(e for e in redis.log if e[0] == "acquire")
        assert acquire == ("acquire", "truenorth:periodic:health_check_ranges",
                           tasks.HEALTH_CHECK_BUDGET + 20, False)
        # It outlives the run's own budget and Celery's hard limit, so an overrun is not overlapped.
        assert acquire[2] > tasks.health_check_ranges.time_limit
        assert ("release", "truenorth:periodic:health_check_ranges") in redis.log and not redis.held

    def test_the_lock_is_released_when_the_run_fails(self, db, backends, redis, monkeypatch):
        monkeypatch.setattr(periodic, "_active_ranges", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        with pytest.raises(RuntimeError, match="db down"):
            tasks.collect_range_metrics()
        assert not redis.held

    def test_without_redis_the_run_goes_ahead(self, db, backends):
        _range(db, "r1", "mock")
        assert tasks.health_check_ranges()["checked"] == 1

    def test_runs_are_time_limited_and_scheduled_runs_expire(self):
        for task, budget, every in ((tasks.health_check_ranges, tasks.HEALTH_CHECK_BUDGET, 60),
                                    (tasks.collect_range_metrics, tasks.METRICS_BUDGET, 30)):
            assert budget < task.soft_time_limit < task.time_limit <= every
        beat = celery_app.app.conf.beat_schedule
        assert beat["health-check-ranges"]["options"]["expires"] == 60
        assert beat["collect-range-metrics"]["options"]["expires"] == 30


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def _vm(name, power="poweredOn", cpu=(500, 4000), mem=(1024, 4096), tools="guestToolsRunning"):
    return {"vm_id": f"vm-{name}", "name": name, "power_state": power, "tools_status": tools,
            "cpu_usage_mhz": cpu[0], "cpu_capacity_mhz": cpu[1], "memory_active_mb": mem[0],
            "memory_configured_mb": mem[1], "uptime_seconds": 60}


class TestCollectRangeMetrics:
    def test_metrics_come_from_each_ranges_own_backend(self, db, backends, ingested):
        _range(db, "r1", "vsphere_api")
        _range(db, "r2", None)  # env: proxmox_api, which has no metrics
        backends.script["vsphere_api"] = {"metrics": {"r1": MetricsResult(
            status="ok", source="vsphere",
            vms=[_vm("a"), _vm("b", cpu=(1500, 4000), mem=(3072, 4096)), _vm("c", power="poweredOff",
                                                                             cpu=(0, 2000), mem=(0, 2048))])}}

        result = tasks.collect_range_metrics()

        assert result == {"status": "ok", "ranges_collected": 1, "failed": 0, "unsupported": 1, "skipped": 0}
        assert backends.by("vsphere_api")[0].calls == [("metrics", "r1")]
        assert backends.by("proxmox_api")[0].calls == [("metrics", "r2")]
        (rid, (event,)), = ingested  # an unsupported backend sends nothing
        assert rid == "r1" and event["event_type"] == "range_metrics" and event["range_id"] == "r1"
        # Powered-on VMs only: (500 + 1500) / (4000 + 4000), (1024 + 3072) / (4096 + 4096).
        assert event["cpu_pct"] == 25.0 and event["memory_pct"] == 50.0
        assert event["cpu_usage_mhz"] == 2000 and event["cpu_capacity_mhz"] == 8000
        assert event["memory_active_mb"] == 4096 and event["memory_configured_mb"] == 8192
        assert event["vm_count"] == 3 and event["vms_powered_on"] == 2 and event["vms_tools_running"] == 3
        # Not measured by any backend: present (the old keys), and null.
        for key in ("disk_read_mbps", "disk_write_mbps", "network_in_mbps", "network_out_mbps"):
            assert key in event and event[key] is None
        assert event["synthetic"] is False and event["metrics_status"] == "ok"
        assert event["metrics_source"] == "vsphere" and event["backend"] == "vsphere_api"
        assert [v["name"] for v in event["vm_metrics"]] == ["a", "b", "c"]
        json.dumps(event)  # it goes through Celery's JSON serializer
        # Only the range actually read gets its updated_at bumped.
        stamps = dict(db("SELECT id, updated_at FROM ranges"))
        assert stamps["r2"] == "before" and stamps["r1"] != "before"  # only the range read is bumped

    def test_a_failed_read_is_reported_with_nulls_not_numbers(self, db, backends, ingested):
        _range(db, "r1", "vsphere_api")
        _range(db, "r2", "vsphere_api")
        backends.script["vsphere_api"] = {"metrics": {
            "r1": RuntimeError("vCenter unreachable"),
            "r2": MetricsResult(status="failed", source="vsphere", errors=["vCenter login failed: bad password"]),
        }}
        result = tasks.collect_range_metrics()
        assert result["ranges_collected"] == 0 and result["failed"] == 2
        events = {rid: evs[0] for rid, evs in ingested}
        assert events["r1"]["metrics_status"] == "failed" and events["r1"]["metrics_errors"] == ["vCenter unreachable"]
        for ev in events.values():
            assert ev["cpu_pct"] is None and ev["memory_pct"] is None and ev["vms_powered_on"] == 0
            assert ev["vm_count"] == 2  # what the range has recorded, though none could be read
        assert {u for _, u in db("SELECT id, updated_at FROM ranges")} == {"before"}

    def test_partial_reads_count_as_collected(self, db, backends, ingested):
        _range(db, "r1", "vsphere_api")
        backends.script["vsphere_api"] = {"metrics": {"r1": MetricsResult(
            status="partial", vms=[_vm("a"), {**_vm("b"), "power_state": "notFound", "cpu_usage_mhz": None}],
            errors=["VM b: not found in vCenter"])}}
        assert tasks.collect_range_metrics()["ranges_collected"] == 1
        event = ingested[0][1][0]
        assert event["metrics_status"] == "partial" and event["metrics_errors"] == ["VM b: not found in vCenter"]
        assert event["cpu_pct"] == 12.5  # VM a only

    def test_budget_skips_ranges_it_did_not_reach(self, db, backends, ingested):
        for rid in ("r1", "r2"):
            _range(db, rid, "vsphere_api")
        result = periodic.collect_metrics_run(time.monotonic() - 1)
        assert result["skipped"] == 2 and ingested == [] and backends.by("vsphere_api")[0].calls == []


class TestNoRandomNumbers:
    """The old task reported random.uniform() values as range CPU, memory, disk and network."""

    def test_tasks_module_does_not_use_random(self):
        assert not hasattr(tasks, "random")

    def test_mock_ranges_report_the_same_labelled_numbers_every_run(self, db, monkeypatch, ingested):
        monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
        _range(db, "r1", "mock", vms=("web", "db"))

        def boom(*a, **k):
            raise AssertionError("metrics must not draw random numbers")

        for name in ("random", "uniform", "randint", "gauss", "choice"):
            monkeypatch.setattr(random, name, boom)

        first = tasks.collect_range_metrics()
        second = tasks.collect_range_metrics()
        assert first["ranges_collected"] == second["ranges_collected"] == 1
        a, b = (evs[0] for _, evs in ingested)
        assert {k: v for k, v in a.items() if "timestamp" not in k} == \
            {k: v for k, v in b.items() if "timestamp" not in k}
        assert a["synthetic"] is True and a["metrics_source"] == "mock"
        assert a["disk_read_mbps"] is None and a["network_out_mbps"] is None

    def test_mock_metrics_are_deterministic_and_marked_synthetic(self):
        out = {"vms": [{"vm_id": "1", "name": "a", "cpu": 4, "memory_mb": 8192, "status": "running"},
                       {"vm_id": "2", "name": "b", "status": "stopped"}]}
        one = _run(MockProvisioner().collect_metrics("r1", out))
        two = _run(MockProvisioner().collect_metrics("r1", out))
        assert one.vms == two.vms and one.synthetic is True and one.source == "mock" and one.status == "ok"
        assert [v["power_state"] for v in one.vms] == ["poweredOn", "poweredOff"]

    @pytest.mark.parametrize("cls", [ProxmoxAPIProvisioner, HypervProvisioner, TerraformProvisioner])
    def test_backends_without_metrics_say_unsupported_and_invent_nothing(self, cls):
        result = _run(cls().collect_metrics("r1", {"vms": [{"name": "a", "vm_id": "100"}]}))
        assert result.status == "unsupported" and result.vms == [] and not result.synthetic
        assert "does not report VM metrics" in result.errors[0]

    def test_the_default_session_scope_does_nothing(self):
        async def scoped():
            prov = ProxmoxAPIProvisioner()
            async with prov.session() as p:
                return p is prov

        assert _run(scoped())
