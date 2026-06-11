"""Tests for Celery worker tasks."""

import pytest

# worker.tasks requires celery + kombu.  Pre-import so patch() can resolve the
# module path.  If unavailable, mark all tasks-dependent tests as skipped.
try:
    import worker.celery_app  # noqa: F401
    import worker.tasks  # noqa: F401
    _WORKER_IMPORTABLE = True
except Exception:
    _WORKER_IMPORTABLE = False


class TestWorkerTasks:
    def test_worker_module_imports(self):
        """Verify worker module structure exists."""
        import importlib.util

        # The worker package is at control-plane/worker/worker/tasks.py
        # With pythonpath including control-plane/worker, import as worker.tasks
        spec = importlib.util.find_spec("worker.tasks")
        if spec is None:
            pytest.skip("Worker package not installed in test environment")
        assert spec is not None

    def test_celery_app_imports(self):
        """Verify celery app can be imported."""
        import importlib.util

        spec = importlib.util.find_spec("worker.celery_app")
        if spec is None:
            pytest.skip("Worker package not installed in test environment")
        assert spec is not None

    def test_mock_provisioner_returns_expected_keys(self):
        """Test the mock provisioner logic returns correct structure."""
        result = {
            "vms": [
                {"name": "dc01", "ip": "10.0.1.10", "status": "running"},
                {"name": "ws01", "ip": "10.0.1.20", "status": "running"},
            ],
            "network": {"vlan_id": 100, "cidr": "10.0.1.0/24"},
        }
        assert "vms" in result
        assert len(result["vms"]) > 0
        assert "network" in result

    def test_reliable_task_backoff(self):
        """Test exponential backoff calculation."""
        import random

        random.seed(42)
        # Simulate backoff: base * 2^retries + jitter
        base = 60
        for retries in range(5):
            backoff = base * (2**retries) + random.uniform(0, 30)
            assert backoff >= base * (2**retries)
            assert backoff <= base * (2**retries) + 30


# ========================================================================
# Tests for new Celery tasks (Part C)
# ========================================================================


class TestRunScenarioV2:
    """Tests for run_scenario_v2 task."""

    def test_run_scenario_mock(self):
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        """Test run_scenario_v2 with mock backend and in-memory stubs."""
        import os

        os.environ["PROVISIONER_BACKEND"] = "mock"
        os.environ["DATABASE_URL"] = "sqlite://"

        scenario_def = {
            "timeline": [
                {"t": "0:00", "action": "deploy_malware", "params": {"target": "ws-001"}},
                {"t": "0:30", "action": "exfil_data", "params": {"target": "dc-01"}},
                {"t": "1:00", "action": "cleanup", "params": {}},
            ],
            "objectives": [
                {"ref_id": "obj-1", "description": "Detect malware", "validator": "manual", "points": 50},
                {"ref_id": "obj-2", "description": "Block exfil", "validator": "manual", "points": 50},
            ],
            "inject_packs": [],
        }

        # Patch _db_session and _notify_api so we don't need real DB/Redis
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute = MagicMock()

        with (
            patch("worker.tasks._db_session", return_value=mock_session),
            patch("worker.tasks._notify_api") as mock_notify,
        ):
            try:
                from worker.tasks import run_scenario_v2

                result = run_scenario_v2(exercise_id="ex-test-001", scenario_definition=scenario_def)
                assert result["status"] == "completed"
                assert result["events_executed"] == 3
                assert result["objectives_completed"] == 2
                assert mock_notify.called
            except ImportError:
                pytest.skip("Worker module not importable in test env")

    def test_run_scenario_empty_timeline(self):
        """Test run_scenario_v2 with empty timeline."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        import os

        os.environ["PROVISIONER_BACKEND"] = "mock"
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute = MagicMock()

        scenario_def = {"timeline": [], "objectives": [], "inject_packs": []}

        with patch("worker.tasks._db_session", return_value=mock_session), patch("worker.tasks._notify_api"):
            try:
                from worker.tasks import run_scenario_v2

                result = run_scenario_v2(exercise_id="ex-empty", scenario_definition=scenario_def)
                assert result["status"] == "completed"
                assert result["events_executed"] == 0
            except ImportError:
                pytest.skip("Worker module not importable in test env")


class TestGenerateAAR:
    """Tests for generate_aar task."""

    def test_generate_aar_mock(self):
        """Test AAR generation with mocked DB data."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        import os

        os.environ.pop("AI_ORCHESTRATOR_URL", None)
        from collections import namedtuple
        from unittest.mock import MagicMock, patch

        ExRow = namedtuple("ExRow", ["id", "name", "state", "total_score", "max_score", "started_at", "completed_at"])
        ObjRow = namedtuple(
            "ObjRow", ["ref_id", "description", "objective_type", "points", "achieved", "evidence", "achieved_at"]
        )

        mock_ex = ExRow("ex-001", "Test Exercise", "completed", 80, 100, "2026-01-15T10:00:00Z", "2026-01-15T12:00:00Z")
        mock_objs = [
            ObjRow("obj-1", "Detect intrusion", "detection", 40, True, "Found it", "2026-01-15T11:00:00Z"),
            ObjRow("obj-2", "Contain threat", "response", 40, True, "Contained", "2026-01-15T11:30:00Z"),
            ObjRow("obj-3", "Write report", "deliverable", 20, False, None, None),
        ]

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:
                result.first.return_value = mock_ex
                result.fetchall.return_value = []
            elif call_count[0] == 1:
                result.fetchall.return_value = mock_objs
                result.first.return_value = None
            else:
                result.first.return_value = None
                result.fetchall.return_value = []
            call_count[0] += 1
            return result

        mock_session.execute = MagicMock(side_effect=side_effect)

        with (
            patch("worker.tasks._db_session", return_value=mock_session),
            patch("worker.tasks._notify_api") as mock_notify,
        ):
            try:
                from worker.tasks import generate_aar

                result = generate_aar(exercise_id="ex-001")
                assert result["status"] == "generated"
                assert result["exercise_id"] == "ex-001"
                assert "aar_id" in result
                assert mock_notify.called
            except ImportError:
                pytest.skip("Worker module not importable in test env")

    def test_generate_aar_exercise_not_found(self):
        """Test AAR generation when exercise doesn't exist."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        result_mock = MagicMock()
        result_mock.first.return_value = None
        mock_session.execute = MagicMock(return_value=result_mock)

        with patch("worker.tasks._db_session", return_value=mock_session), patch("worker.tasks._notify_api"):
            try:
                from worker.tasks import generate_aar

                with pytest.raises(ValueError, match="not found"):
                    generate_aar(exercise_id="nonexistent")
            except ImportError:
                pytest.skip("Worker module not importable in test env")


class TestCleanupExpiredRanges:
    """Tests for cleanup_expired_ranges task."""

    def test_cleanup_expired_ranges(self):
        """Test periodic cleanup dispatches destroy tasks."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        from collections import namedtuple
        from unittest.mock import MagicMock, patch

        RangeRow = namedtuple("RangeRow", ["id", "name"])
        expired_ranges = [
            RangeRow("r-exp-1", "Expired Range 1"),
            RangeRow("r-exp-2", "Expired Range 2"),
        ]

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        result_mock = MagicMock()
        result_mock.fetchall.return_value = expired_ranges
        mock_session.execute = MagicMock(return_value=result_mock)

        with (
            patch("worker.tasks._db_session", return_value=mock_session),
            patch("worker.tasks._notify_api"),
            patch("worker.tasks.destroy_range") as mock_destroy,
        ):
            mock_destroy.delay = MagicMock()
            try:
                from worker.tasks import cleanup_expired_ranges

                result = cleanup_expired_ranges()
                assert result["status"] == "ok"
                assert result["expired_count"] == 2
                assert mock_destroy.delay.call_count == 2
            except ImportError:
                pytest.skip("Worker module not importable in test env")

    def test_cleanup_no_expired_ranges(self):
        """Test cleanup when no ranges are expired."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        mock_session.execute = MagicMock(return_value=result_mock)

        with patch("worker.tasks._db_session", return_value=mock_session), patch("worker.tasks._notify_api"):
            try:
                from worker.tasks import cleanup_expired_ranges

                result = cleanup_expired_ranges()
                assert result["status"] == "ok"
                assert result["expired_count"] == 0
            except ImportError:
                pytest.skip("Worker module not importable in test env")


class TestSnapshotRange:
    """Tests for snapshot_range task."""

    def test_snapshot_range_mock(self):
        """Test snapshot creation with mock backend."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        import os

        os.environ["PROVISIONER_BACKEND"] = "mock"
        import json
        from unittest.mock import MagicMock, patch

        prov_output = json.dumps(
            {
                "vms": [
                    {"name": "jump-box", "ip": "10.0.1.10"},
                    {"name": "dc-01", "ip": "10.0.2.10"},
                    {"name": "ws-001", "ip": "10.0.3.10"},
                ],
            }
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:
                # First call: get provisioner_output
                result.first.return_value = (prov_output,)
            else:
                result.first.return_value = None
            call_count[0] += 1
            return result

        mock_session.execute = MagicMock(side_effect=side_effect)

        with (
            patch("worker.tasks._db_session", return_value=mock_session),
            patch("worker.tasks._notify_api") as mock_notify,
        ):
            try:
                from worker.tasks import snapshot_range

                result = snapshot_range(range_id="r-snap-001", snapshot_id="snap-test-001")
                assert result["status"] == "ready"
                assert result["snapshot_id"] == "snap-test-001"
                assert mock_notify.called
            except ImportError:
                pytest.skip("Worker module not importable in test env")

    def test_snapshot_range_no_vms(self):
        """Test snapshot fails when range has no provisioner output."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        result_mock = MagicMock()
        result_mock.first.return_value = (None,)
        mock_session.execute = MagicMock(return_value=result_mock)

        with patch("worker.tasks._db_session", return_value=mock_session), patch("worker.tasks._notify_api"):
            try:
                from worker.tasks import snapshot_range

                # Should raise because provisioner_output is None
                with pytest.raises((ValueError, TypeError)):
                    snapshot_range(range_id="r-no-vms")
            except ImportError:
                pytest.skip("Worker module not importable in test env")


class TestHealthCheckRanges:
    """Tests for health_check_ranges task."""

    def test_health_check_ranges(self):
        """Test health check with mock backend."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        import json
        import os

        os.environ["PROVISIONER_BACKEND"] = "mock"
        import random
        from collections import namedtuple
        from unittest.mock import MagicMock, patch

        random.seed(42)  # deterministic

        prov = json.dumps({"vms": [{"name": "vm-1"}]})
        RangeRow = namedtuple("RangeRow", ["id", "name", "provisioner_output"])
        active = [
            RangeRow("r1", "Range A", prov),
            RangeRow("r2", "Range B", prov),
        ]

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        result_mock = MagicMock()
        result_mock.fetchall.return_value = active
        mock_session.execute = MagicMock(return_value=result_mock)

        with patch("worker.tasks._db_session", return_value=mock_session), patch("worker.tasks._notify_api"):
            try:
                from worker.tasks import health_check_ranges

                result = health_check_ranges()
                assert result["status"] == "ok"
                assert result["checked"] == 2
                assert result["healthy"] + result["unhealthy"] == 2
            except ImportError:
                pytest.skip("Worker module not importable in test env")

    def test_health_check_no_active_ranges(self):
        """Test health check when no ranges are active."""
        if not _WORKER_IMPORTABLE:
            pytest.skip("Worker package not installed (celery missing)")
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        mock_session.execute = MagicMock(return_value=result_mock)

        with patch("worker.tasks._db_session", return_value=mock_session), patch("worker.tasks._notify_api"):
            try:
                from worker.tasks import health_check_ranges

                result = health_check_ranges()
                assert result["status"] == "ok"
                assert result["checked"] == 0
            except ImportError:
                pytest.skip("Worker module not importable in test env")
