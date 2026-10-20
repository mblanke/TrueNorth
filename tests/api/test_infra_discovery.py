"""Scheduled rediscovery of hypervisor connections (app/infra_discovery).

The Infrastructure page's figures used to change only when someone pressed Discover. The
API now rediscovers every active connection every INFRA_DISCOVERY_SECONDS through the same
call the Discover endpoint makes, one discovery per connection at a time (a lease), each
connection on its own, and records when each last succeeded and why it last failed.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest import mock

import httpx
import pytest
import respx
from app.auth import CurrentUser, get_current_user
from app.infra_discovery import runner, service
from app.infra_discovery.models import HypervisorDiscoveryState
from app.main import app as fastapi_app
from app.models import HypervisorConnection, HypervisorNode, UserRole
from sqlalchemy.orm import sessionmaker

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
VCENTER = "https://vcsa.range.test"


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=DEV_TENANT,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _conn(db, name: str, *, kind: str = "vsphere", active: bool = True) -> HypervisorConnection:
    conn = HypervisorConnection(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(DEV_TENANT),
        name=name,
        hypervisor_type=kind,
        host=f"{name}.range.test",
        port=443,
        username="svc-truenorth@vsphere.local",
        password_encrypted="x",
        is_active=active,
    )
    db.add(conn)
    db.commit()
    return conn


def _factory(db_session):
    """Sessions on the test's connection, so a round's commits stay inside its transaction.
    Savepoints: the service rolls back a failed discovery's half-written rows, and in the
    test's joined transaction a plain rollback would end the test's own."""
    return sessionmaker(bind=db_session.connection(), autoflush=False, join_transaction_mode="create_savepoint")


def _session(db_session):
    return _factory(db_session)()


def _state(db, conn) -> HypervisorDiscoveryState | None:
    db.expire_all()
    return db.get(HypervisorDiscoveryState, conn.id)


class _Backend:
    """A stand-in hypervisor backend: per connection name, succeed, fail (as a result) or raise."""

    def __init__(self, outcomes: dict[str, str]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def discover(self, conn_id, conn, db):
        self.calls.append(conn.name)
        outcome = self.outcomes.get(conn.name, "ok")
        if outcome == "raise":
            raise RuntimeError("TN_SECRETS_KEY cannot unseal this password")
        if outcome == "fail":
            return {"message": "vSphere discovery failed: timed out", "nodes_discovered": 0, "error": "timed out"}
        db.add(HypervisorNode(connection_id=conn_id, node_name=f"esx-{conn.name}", status="online", vm_count=2))
        db.commit()
        return {"message": "Discovered 1 new hosts (1 total)", "nodes_discovered": 1}


@contextmanager
def backend(outcomes: dict[str, str] | None = None):
    fake = _Backend(outcomes or {})
    with mock.patch("app.infra_discovery.service.get_hypervisor_backend", return_value=fake):
        yield fake


@pytest.fixture
def every_300(monkeypatch):
    monkeypatch.setenv("INFRA_DISCOVERY_SECONDS", "300")


# -- The round ---------------------------------------------------------------
class TestRound:
    def test_rediscovers_every_active_connection_and_records_success(self, db_session, every_300):
        a, b = _conn(db_session, "vc-a"), _conn(db_session, "vc-b")
        off = _conn(db_session, "vc-off", active=False)
        with backend() as fake:
            counts = service.run_round(_factory(db_session))

        assert sorted(fake.calls) == ["vc-a", "vc-b"]  # never the inactive one
        assert counts == {"ok": 2, "failed": 0, "skipped": 0}
        for c in (a, b):
            st = _state(db_session, c)
            assert st.last_discovered_at is not None and st.last_error is None
            assert st.lease_holder is None and st.lease_expires_at is None  # released
        assert _state(db_session, off) is None

    def test_a_failing_or_raising_connection_does_not_stop_the_others(self, db_session, every_300):
        bad, boom, good = _conn(db_session, "vc-bad"), _conn(db_session, "vc-boom"), _conn(db_session, "vc-good")
        with backend({"vc-bad": "fail", "vc-boom": "raise"}) as fake:
            counts = service.run_round(_factory(db_session))

        assert sorted(fake.calls) == ["vc-bad", "vc-boom", "vc-good"]
        assert counts == {"ok": 1, "failed": 2, "skipped": 0}
        assert _state(db_session, good).last_discovered_at is not None
        assert _state(db_session, bad).last_error == "timed out"
        assert _state(db_session, bad).last_discovered_at is None
        assert "cannot unseal" in _state(db_session, boom).last_error
        for c in (bad, boom, good):
            assert _state(db_session, c).lease_holder is None  # every lease given back

    def test_a_failure_keeps_the_last_success_time_and_a_success_clears_the_error(self, db_session, every_300):
        c = _conn(db_session, "vc-flaky")
        with backend():
            service.discover_connection(_session(db_session), c)
        first_ok = _state(db_session, c).last_discovered_at
        with backend({"vc-flaky": "fail"}):
            service.discover_connection(_session(db_session), c)
        st = _state(db_session, c)
        assert st.last_discovered_at == first_ok and st.last_error == "timed out"
        with backend():
            service.discover_connection(_session(db_session), c)
        st = _state(db_session, c)
        assert st.last_error is None and st.last_discovered_at >= first_ok

    def test_a_connection_is_rediscovered_once_per_interval_however_many_processes_tick(self, db_session, every_300):
        _conn(db_session, "vc-a")
        with backend() as fake:
            service.run_round(_factory(db_session))
            second = service.run_round(_factory(db_session))  # another API process, moments later
        assert fake.calls == ["vc-a"]
        assert second == {"ok": 0, "failed": 0, "skipped": 1}

    def test_due_again_once_the_interval_has_passed(self, db_session, every_300):
        c = _conn(db_session, "vc-a")
        with backend() as fake:
            service.run_round(_factory(db_session))
            st = _state(db_session, c)
            st.last_attempt_at = datetime.now(UTC) - timedelta(seconds=301)
            db_session.commit()
            service.run_round(_factory(db_session))
        assert fake.calls == ["vc-a", "vc-a"]


# -- The lease ---------------------------------------------------------------
class TestLease:
    def test_a_held_lease_is_skipped_not_waited_on(self, db_session, every_300):
        """A slow vCenter still being discovered gets no second, overlapping run."""
        c = _conn(db_session, "vc-slow")
        assert service.claim(db_session, c.id, "other-process")
        with backend() as fake:
            counts = service.run_round(_factory(db_session))
            assert service.discover_connection(_session(db_session), c) is None  # the Discover button too
        assert fake.calls == []
        assert counts["skipped"] == 1
        assert _state(db_session, c).lease_holder == "other-process"  # not taken from its holder

    def test_an_expired_lease_is_taken_over(self, db_session, every_300):
        """A process that died mid-discovery does not block the connection for good."""
        c = _conn(db_session, "vc-a")
        service.claim(db_session, c.id, "dead-process")
        st = _state(db_session, c)
        st.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        st.last_attempt_at = datetime.now(UTC) - timedelta(seconds=service.LEASE_SECONDS)
        db_session.commit()
        with backend() as fake:
            service.run_round(_factory(db_session))
        assert fake.calls == ["vc-a"]
        assert _state(db_session, c).lease_holder is None

    def test_release_by_a_superseded_holder_changes_nothing(self, db_session):
        c = _conn(db_session, "vc-a")
        service.claim(db_session, c.id, "new-holder")
        service.release(db_session, c.id, "old-holder", error=None)
        st = _state(db_session, c)
        assert st.lease_holder == "new-holder" and st.last_discovered_at is None


# -- The real vSphere backend through the scheduled path ----------------------
def _mock_vcenter(router: respx.MockRouter) -> None:
    router.post(f"{VCENTER}/api/session").mock(return_value=httpx.Response(201, json="tok"))
    router.delete(f"{VCENTER}/api/session").mock(return_value=httpx.Response(204))
    router.get(f"{VCENTER}/api/appliance/system/version").mock(return_value=httpx.Response(200, json={"version": "8.0.3"}))
    router.get(url__startswith=f"{VCENTER}/sdk/").mock(return_value=httpx.Response(404))  # no VI/JSON
    router.get(f"{VCENTER}/api/vcenter/host").mock(
        return_value=httpx.Response(200, json=[{"host": "host-1", "name": "10.20.0.11", "connection_state": "CONNECTED"}])
    )
    router.get(f"{VCENTER}/api/vcenter/vm", params={"hosts": "host-1"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"vm": "vm-1", "name": "range-web01", "power_state": "POWERED_ON"},
                {"vm": "vm-2", "name": "vCLS-2f8a", "power_state": "POWERED_ON"},  # cluster agent
                {"vm": "vm-3", "name": "WIN11-TMPL", "power_state": "POWERED_OFF"},  # ignored filter
            ],
        )
    )


def test_scheduled_round_counts_vms_by_the_backends_rules(db_session, every_300):
    """The round calls the vSphere backend itself, so vCLS agents and powered-off VMs (and
    so templates) are not VMs here either (#176, #177)."""
    c = _conn(db_session, "vcsa")
    with respx.mock() as router:
        _mock_vcenter(router)
        counts = service.run_round(_factory(db_session))
    assert counts["ok"] == 1
    node = db_session.query(HypervisorNode).filter(HypervisorNode.connection_id == c.id).one()
    assert node.vm_count == 1
    assert _state(db_session, c).last_discovered_at is not None


def test_scheduled_round_records_an_unreachable_vcenter(db_session, every_300):
    c = _conn(db_session, "vcsa")
    with respx.mock() as router:
        router.route().mock(side_effect=httpx.ConnectError("connection refused"))
        counts = service.run_round(_factory(db_session))
    assert counts["failed"] == 1
    st = _state(db_session, c)
    assert "connection refused" in st.last_error and st.last_discovered_at is None


# -- The endpoints -------------------------------------------------------------
class TestEndpoints:
    def test_discover_endpoint_goes_through_the_service_and_records_it(self, client, db_session):
        c = _conn(db_session, "vc-a")
        with acting_as(UserRole.admin), backend() as fake:
            r = client.post(f"/hypervisors/connections/{c.id}/discover")
        assert r.status_code == 200, r.text
        assert r.json()["nodes_discovered"] == 1
        assert fake.calls == ["vc-a"]
        assert _state(db_session, c).last_discovered_at is not None

    def test_discover_endpoint_is_409_while_a_discovery_runs(self, client, db_session):
        c = _conn(db_session, "vc-a")
        service.claim(db_session, c.id, "scheduled-round")
        with acting_as(UserRole.admin), backend() as fake:
            r = client.post(f"/hypervisors/connections/{c.id}/discover")
        assert r.status_code == 409
        assert fake.calls == []

    def test_status_dates_each_connection_and_says_which_is_running(self, client, db_session, every_300):
        ok, bad, never = _conn(db_session, "vc-ok"), _conn(db_session, "vc-bad"), _conn(db_session, "vc-new")
        with backend({"vc-bad": "fail"}):
            service.discover_connection(_session(db_session), ok)
            service.discover_connection(_session(db_session), bad)
        service.claim(db_session, ok.id, "running-now")
        with acting_as(UserRole.admin):
            body = client.get("/hypervisors/discovery").json()

        assert body["interval_seconds"] == 300
        rows = {r["connection_id"]: r for r in body["connections"]}
        assert rows[str(ok.id)]["running"] is True and rows[str(ok.id)]["failing"] is False
        assert rows[str(ok.id)]["last_discovered_at"].endswith(("+00:00", "Z"))  # an instant, not local time
        assert rows[str(bad.id)]["failing"] is True and rows[str(bad.id)]["last_error"] == "timed out"
        assert rows[str(never.id)]["last_discovered_at"] is None and rows[str(never.id)]["running"] is False

    def test_status_hides_the_error_text_from_read_only_callers(self, client, db_session):
        bad = _conn(db_session, "vc-bad")
        with backend({"vc-bad": "fail"}):
            service.discover_connection(_session(db_session), bad)
        with acting_as(UserRole.instructor):
            r = client.get("/hypervisors/discovery")
        assert r.status_code == 200, r.text
        row = r.json()["connections"][0]
        assert row["failing"] is True and row["last_error"] is None

    def test_status_shows_only_the_callers_tenant(self, client, db_session):
        other = HypervisorConnection(
            id=uuid.uuid4(), tenant_id=None, name="shared", hypervisor_type="vsphere",
            host="shared.range.test", port=443, username="u", password_encrypted="x",
        )
        db_session.add(other)
        mine = _conn(db_session, "vc-mine")
        with acting_as(UserRole.admin):
            body = client.get("/hypervisors/discovery").json()
        assert [r["connection_id"] for r in body["connections"]] == [str(mine.id)]

    def test_deleting_a_connection_drops_its_discovery_state(self, client, db_session):
        c = _conn(db_session, "vc-a")
        with backend():
            service.discover_connection(_session(db_session), c)
        with acting_as(UserRole.admin):
            r = client.delete(f"/hypervisors/connections/{c.id}")
        assert r.status_code in (200, 204), r.text
        assert _state(db_session, c) is None


# -- The loop ----------------------------------------------------------------
class TestRunner:
    def test_zero_turns_the_schedule_off(self, monkeypatch):
        monkeypatch.setenv("INFRA_DISCOVERY_SECONDS", "0")
        with mock.patch.object(runner, "_round") as round_:
            asyncio.run(asyncio.wait_for(runner.loop(), timeout=1))
        round_.assert_not_called()

    def test_default_is_five_minutes(self, monkeypatch):
        monkeypatch.delenv("INFRA_DISCOVERY_SECONDS", raising=False)
        assert service.interval() == 300

    def test_each_tick_runs_a_round_and_a_failed_round_does_not_end_the_loop(self, monkeypatch):
        monkeypatch.setenv("INFRA_DISCOVERY_SECONDS", "300")
        slept: list[float] = []
        ticks = {"n": 0}

        async def fake_sleep(seconds):
            slept.append(seconds)
            if len(slept) > 3:
                raise asyncio.CancelledError

        def fake_round():
            ticks["n"] += 1
            if ticks["n"] == 1:
                raise RuntimeError("database away")
            return {"ok": 1, "failed": 0, "skipped": 0}

        monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(runner, "_round", fake_round)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner.loop())
        assert slept[:3] == [300.0, 300.0, 300.0]
        assert ticks["n"] == 3  # the first round raised; the loop went on


def test_the_api_starts_the_loop():
    import inspect

    from app import main

    src = inspect.getsource(main.lifespan)
    assert "infra_discovery.runner import loop" in src and "infra_discovery_task" in src
