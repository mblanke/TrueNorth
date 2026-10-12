"""collect_range_metrics publishes measured figures or nothing (worker/range_metrics.py).

Until 2026-10-11 it published ``random.uniform`` CPU, memory, disk and network numbers
as ``range_metrics`` telemetry. These tests pin the contract: an event only from a
backend's real, non-synthetic reading, tagged with its source; otherwise no event and
``available: False`` with a reason.
"""

from __future__ import annotations

import ast
import json
import random
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from worker import range_metrics, range_sweep
from worker.provisioners.base import BaseProvisioner
from worker.provisioners.mock import MockProvisioner
from worker.provisioners.results import MetricsResult

WORKER = Path(__file__).resolve().parents[2] / "control-plane" / "worker" / "worker"
NOW = "2026-10-11T12:00:00+00:00"


def _vm(power="poweredOn", cpu=None, cpu_max=None, mem=None, mem_cfg=None):
    return {"vm_id": "vm-1", "name": "web", "power_state": power, "cpu_usage_mhz": cpu,
            "cpu_capacity_mhz": cpu_max, "memory_active_mb": mem, "memory_configured_mb": mem_cfg}


def _row(rid="r1", name="Range A", vms=2, backend="vsphere"):
    output = json.dumps({"vms": [{"name": f"v{i}", "vm_id": f"vm-{i}"} for i in range(vms)]})
    return (rid, name, output, backend)


class _Published:
    def __init__(self):
        self.calls: list[tuple[str, list[dict]]] = []

    def __call__(self, range_id, events):
        self.calls.append((range_id, events))


def _sweep_returning(monkeypatch, results: dict):
    monkeypatch.setattr(range_sweep, "metrics", lambda rows: dict(results))


# -- no invented numbers ---------------------------------------------------------
class TestNothingInvented:
    @pytest.mark.parametrize("module", ["tasks.py", "range_metrics.py", "range_sweep.py"])
    def test_the_metrics_path_does_not_use_random(self, module):
        tree = ast.parse((WORKER / module).read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not names & {"random", "secrets", "numpy.random"}, f"{module} imports a random source"

    def test_a_published_figure_never_comes_from_random(self, monkeypatch):
        """Every random source raises: the run still publishes, from the backend's numbers."""
        for fn in ("random", "uniform", "randint", "choice", "gauss", "triangular", "randrange"):
            monkeypatch.setattr(random, fn, MagicMock(side_effect=AssertionError(f"random.{fn} fed a metric")))
        _sweep_returning(monkeypatch, {"r1": MetricsResult(status="ok", source="vsphere", vms=[
            _vm(cpu=480, cpu_max=4800, mem=512, mem_cfg=2048)])})
        pub = _Published()
        out = range_metrics.collect([_row()], publish=pub)
        assert out["ranges_collected"] == 1
        ((_, (event,)),) = pub.calls
        assert (event["cpu_pct"], event["memory_pct"]) == (10.0, 25.0)

    def test_no_disk_or_network_figures_are_published(self, monkeypatch):
        _sweep_returning(monkeypatch, {"r1": MetricsResult(status="ok", source="vsphere", vms=[
            _vm(cpu=1, cpu_max=2, mem=1, mem_cfg=2)])})
        pub = _Published()
        range_metrics.collect([_row()], publish=pub)
        event = pub.calls[0][1][0]
        assert not [k for k in event if k.startswith(("disk_", "network_"))]


# -- quickStats to percentages ------------------------------------------------------
class TestSummarise:
    def test_vsphere_quickstats_become_range_percentages(self):
        result = MetricsResult(status="ok", source="vsphere", vms=[
            _vm(cpu=250, cpu_max=4800, mem=512, mem_cfg=2048),
            _vm(cpu=950, cpu_max=4800, mem=1536, mem_cfg=2048),
            _vm(power="poweredOff", cpu=0, cpu_max=0, mem=0, mem_cfg=8192),  # off: not in the share
        ])
        event, usage = range_metrics.summarise("r1", "Range A", "vsphere", result, 3, NOW)
        assert usage == {"available": True, "source": "vsphere", "vm_count": 3, "vms_powered_on": 2,
                         "vms_reported": 2, "timestamp": NOW, "cpu_pct": 12.5, "memory_pct": 50.0}
        assert event["event_type"] == "range_metrics" and event["source"] == "vsphere"
        assert event["range_id"] == "r1" and event["@timestamp"] == NOW and "available" not in event

    def test_a_figure_no_vm_reported_is_left_out_not_zero(self):
        result = MetricsResult(status="ok", source="vsphere", vms=[_vm(mem=100, mem_cfg=400)])
        event, usage = range_metrics.summarise("r1", "A", "vsphere", result, 1, NOW)
        assert "cpu_pct" not in event and "cpu_pct" not in usage
        assert event["memory_pct"] == 25.0

    def test_partial_reading_is_marked(self):
        result = MetricsResult(status="partial", source="vsphere", errors=["VM b: not found in vCenter"],
                               vms=[_vm(cpu=1, cpu_max=4), {**_vm(), "power_state": "notFound"}])
        event, usage = range_metrics.summarise("r1", "A", "vsphere", result, 2, NOW)
        assert event["partial"] is True and usage["cpu_pct"] == 25.0

    @pytest.mark.parametrize("result, reason", [
        (MetricsResult(status="unsupported", errors=["HyperVProvisioner does not report VM metrics"]),
         "HyperVProvisioner does not report VM metrics"),
        (MetricsResult(status="failed", source="vsphere", errors=["vCenter login failed: refused"]),
         "vCenter login failed: refused"),
        (MetricsResult(status="ok", source="mock", synthetic=True, vms=[_vm(cpu=1, cpu_max=2)]),
         "the mock backend's figures are made up; not published"),
        (MetricsResult(status="ok", source="vsphere", vms=[_vm(power="poweredOff", cpu=0, cpu_max=0)]),
         "no powered-on VM reported CPU or memory figures"),
        (RuntimeError("ConnectionBindingError: no vSphere connection"),
         "metrics read failed: ConnectionBindingError: no vSphere connection"),
        (None, "the vsphere backend returned no metrics"),
    ])
    def test_nothing_measured_is_no_event_and_available_false(self, result, reason):
        event, usage = range_metrics.summarise("r1", "A", "vsphere", result, 4, NOW)
        assert event is None
        assert usage["available"] is False and usage["reason"] == reason
        assert usage["vm_count"] == 4 and usage["timestamp"] == NOW
        assert not {"cpu_pct", "memory_pct"} & usage.keys()


# -- the run over every range -----------------------------------------------------
class _NoMetrics(BaseProvisioner):
    """A backend that never overrode collect_metrics: BaseProvisioner's 'unsupported'."""

    async def provision(self, *a, **k): ...
    async def destroy(self, *a, **k): ...
    async def snapshot(self, *a, **k): ...
    async def restore(self, *a, **k): ...
    async def health_check(self, *a, **k): ...
    async def start(self, *a, **k): ...
    async def stop(self, *a, **k): ...


class TestCollect:
    def test_an_unsupported_backend_publishes_nothing(self, monkeypatch):
        monkeypatch.setattr(range_sweep.base_tasks, "_get_backend", lambda *a, **k: _NoMetrics())
        pub = _Published()
        out = range_metrics.collect([_row(backend="hyperv", vms=3)], publish=pub)
        assert pub.calls == []
        assert out["ranges_collected"] == 0 and out["ranges_unavailable"] == 1
        usage = out["resource_usage"]["r1"]
        assert usage["available"] is False and usage["vm_count"] == 3
        assert "does not report VM metrics" in usage["reason"]

    def test_the_mock_backends_made_up_numbers_are_not_published(self, monkeypatch):
        monkeypatch.setattr(range_sweep.base_tasks, "_get_backend", lambda *a, **k: MockProvisioner())
        pub = _Published()
        out = range_metrics.collect([_row(backend="mock")], publish=pub)
        assert pub.calls == [] and out["resource_usage"]["r1"]["available"] is False
        assert out["resource_usage"]["r1"]["source"] == "mock"

    def test_one_range_reported_and_one_not_in_the_same_run(self, monkeypatch):
        _sweep_returning(monkeypatch, {
            "r1": MetricsResult(status="ok", source="vsphere", vms=[_vm(cpu=1, cpu_max=4)]),
            "r2": MetricsResult(status="unsupported", errors=["no metrics here"]),
        })
        pub = _Published()
        out = range_metrics.collect([_row("r1"), _row("r2", backend="proxmox_api")], publish=pub)
        assert [rid for rid, _ in pub.calls] == ["r1"]
        assert out["ranges_collected"] == 1 and out["ranges_unavailable"] == 1
        assert out["resource_usage"]["r2"] == {"available": False, "reason": "no metrics here",
                                               "source": "proxmox_api", "vm_count": 2,
                                               "timestamp": out["resource_usage"]["r2"]["timestamp"]}

    def test_a_range_the_sweep_did_not_read_is_unavailable(self, monkeypatch):
        _sweep_returning(monkeypatch, {})
        out = range_metrics.collect([_row()], publish=_Published())
        assert "was not read" in out["resource_usage"]["r1"]["reason"]

    def test_a_publish_failure_does_not_stop_the_other_ranges(self, monkeypatch):
        ok = MetricsResult(status="ok", source="vsphere", vms=[_vm(cpu=1, cpu_max=4)])
        _sweep_returning(monkeypatch, {"r1": ok, "r2": ok})
        calls = []

        def publish(rid, events):
            calls.append(rid)
            if rid == "r1":
                raise ConnectionError("broker down")

        out = range_metrics.collect([_row("r1"), _row("r2")], publish=publish)
        assert calls == ["r1", "r2"] and out["ranges_collected"] == 1

    def test_no_active_ranges_reads_nothing(self, monkeypatch):
        sweep = MagicMock()
        monkeypatch.setattr(range_sweep, "metrics", sweep)
        assert range_metrics.collect([], publish=_Published())["ranges_collected"] == 0
        sweep.assert_not_called()

    def test_the_sweep_logs_in_once_per_endpoint_and_calls_collect_metrics(self, monkeypatch):
        backend = MagicMock()
        backend.connection_record.return_value = {"id": "conn-1"}
        backend.collect_metrics = AsyncMock(return_value=MetricsResult(status="ok", source="vsphere"))
        entered = []

        class _Scope:
            async def __aenter__(self):
                entered.append(1)

            async def __aexit__(self, *exc):
                return False

        backend.session = _Scope
        monkeypatch.setattr(range_sweep.base_tasks, "_get_backend", lambda *a, **k: backend)
        got = range_sweep.metrics([_row("r1"), _row("r2")])
        assert set(got) == {"r1", "r2"} and entered == [1]
        assert [c.args[0] for c in backend.collect_metrics.await_args_list] == ["r1", "r2"]
        assert backend.health_check.call_count == 0


def test_the_task_delegates_and_returns_the_usage(monkeypatch):
    from worker import tasks

    monkeypatch.setattr(tasks, "_db_session", MagicMock())
    monkeypatch.setattr(tasks.db_ops, "active_ranges", lambda db: [_row()])
    _sweep_returning(monkeypatch, {"r1": MetricsResult(status="unsupported")})
    published = MagicMock()
    monkeypatch.setattr(tasks.ingest_telemetry_batch, "delay", published)
    out = tasks.collect_range_metrics()
    published.assert_not_called()
    assert out["resource_usage"]["r1"]["available"] is False
