"""Tests for TrueNorth Range provisioner backends."""
from __future__ import annotations

import asyncio
import os

import pytest

# Ensure mock mode with zero failure rate for deterministic tests
os.environ["MOCK_PROVISION_DELAY"] = "0"
os.environ["MOCK_FAILURE_RATE"] = "0"

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "control-plane", "worker"))

from worker.provisioners import get_provisioner, MockProvisioner, TerraformProvisioner
from worker.provisioners.mock import MockProvisioner as _MockDirect
from worker.provisioners.results import (
    DestroyResult,
    HealthResult,
    ProvisionResult,
    SnapshotResult,
    StartResult,
    StopResult,
)


def _run(coro):
    """Helper to run an async coroutine in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture
def mock_provisioner() -> MockProvisioner:
    return MockProvisioner()


@pytest.fixture
def sample_template() -> dict:
    return {
        "name": "test-range",
        "vms": [
            {"name": "dc01", "cpu": 2, "memory_mb": 4096},
            {"name": "ws01", "cpu": 2, "memory_mb": 2048},
            {"name": "attacker", "cpu": 1, "memory_mb": 1024},
        ],
        "networks": [{"name": "range-net", "cidr": "10.0.1.0/24", "vlan_id": 200}],
    }


@pytest.fixture
def sample_allocations() -> dict:
    return {"base_vmid": 5000, "vlan_start": 200}


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------

class TestMockProvisioner:
    def test_mock_provision_returns_vms(self, mock_provisioner, sample_template, sample_allocations):
        """Provisioning with the mock backend should return VMs matching the template."""
        result = _run(mock_provisioner.provision("range-001", sample_template, sample_allocations))

        assert isinstance(result, ProvisionResult)
        assert result.status == "ok"
        assert len(result.vms) == 3
        assert result.vms[0]["name"] == "dc01"
        assert result.vms[1]["name"] == "ws01"
        assert result.vms[2]["name"] == "attacker"
        for vm in result.vms:
            assert vm["status"] == "running"
            assert "ip" in vm
            assert "vm_id" in vm
        assert len(result.networks) == 1
        assert result.duration_seconds >= 0
        assert result.errors == []

    def test_mock_destroy(self, mock_provisioner, sample_template, sample_allocations):
        """Destroying a provisioned range should succeed and report resources removed."""
        _run(mock_provisioner.provision("range-002", sample_template, sample_allocations))
        result = _run(mock_provisioner.destroy("range-002", {}))

        assert isinstance(result, DestroyResult)
        assert result.status == "ok"
        assert result.resources_removed > 0
        assert result.errors == []

    def test_mock_failure_rate_zero(self, sample_template, sample_allocations):
        """With MOCK_FAILURE_RATE=0, provision should never fail."""
        os.environ["MOCK_FAILURE_RATE"] = "0"
        prov = MockProvisioner()
        for _ in range(10):
            result = _run(prov.provision(f"range-{_}", sample_template, sample_allocations))
            assert result.status == "ok"

    def test_mock_snapshot(self, mock_provisioner, sample_template, sample_allocations):
        """Snapshot should succeed for a provisioned range."""
        _run(mock_provisioner.provision("range-snap", sample_template, sample_allocations))
        result = _run(mock_provisioner.snapshot("range-snap", {}, "snap-1"))

        assert isinstance(result, SnapshotResult)
        assert result.status == "ok"
        assert result.snapshot_name == "snap-1"
        assert result.vms_snapped == 3
        assert result.errors == []

    def test_mock_health_check(self, mock_provisioner, sample_template, sample_allocations):
        """Health check should report healthy for a running range."""
        _run(mock_provisioner.provision("range-hc", sample_template, sample_allocations))
        result = _run(mock_provisioner.health_check("range-hc", {}))

        assert isinstance(result, HealthResult)
        assert result.healthy is True
        assert result.status == "ok"
        assert len(result.vm_statuses) == 3
        for vs in result.vm_statuses:
            assert vs["healthy"] is True

    def test_mock_health_check_unknown_range(self, mock_provisioner):
        """Health check for a non-existent range should report unhealthy."""
        result = _run(mock_provisioner.health_check("nonexistent", {}))
        assert result.healthy is False
        assert result.status == "unhealthy"


class TestProvisionerRegistry:
    def test_provisioner_registry_mock(self):
        """Registry should return MockProvisioner for 'mock' backend."""
        prov = get_provisioner("mock")
        assert isinstance(prov, MockProvisioner)

    def test_provisioner_registry_terraform(self):
        """Registry should return TerraformProvisioner for 'terraform' backend."""
        prov = get_provisioner("terraform")
        assert isinstance(prov, TerraformProvisioner)

    def test_unknown_backend_raises(self):
        """Unknown backend should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown provisioner backend"):
            get_provisioner("nonexistent_backend")


class TestResultDataclasses:
    def test_result_dataclasses(self):
        """All result dataclasses should instantiate with defaults."""
        pr = ProvisionResult(status="ok")
        assert pr.status == "ok"
        assert pr.vms == []
        assert pr.networks == []
        assert pr.duration_seconds == 0.0
        assert pr.terraform_state is None
        assert pr.errors == []

        dr = DestroyResult(status="ok")
        assert dr.resources_removed == 0

        stop = StopResult(status="ok")
        assert stop.vms_stopped == 0

        start = StartResult(status="ok")
        assert start.vms_started == 0

        snap = SnapshotResult(status="ok")
        assert snap.snapshot_name == ""
        assert snap.vms_snapped == 0

        hr = HealthResult()
        assert hr.healthy is True
        assert hr.status == "ok"
        assert hr.vm_statuses == []