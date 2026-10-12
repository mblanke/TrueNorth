"""Review of #131 (per-tenant vCenter credentials), 2026-10-11: regression tests.

Blocker 1: a tenant connection with no password made vsphere_api fall back to the worker's
VSPHERE_PASSWORD and send it, as Basic auth, to the host the tenant had typed in.

Blocker 2: every operation re-resolved the range's connection, nothing recorded which
vCenter built it, and destroy/power/snapshot acted on bare ``vm-NN`` ids, so a range whose
tenant changed connections had its teardown delete whatever VMs carried those ids on a
different vCenter. Now: the build records the connection (worker/connection_binding.py),
later operations reach only that one, and each VM's range annotation is checked first.

Should-fix 4: the scheduled health/metrics runs logged in once per range per run and never
logged out (worker/range_sweep.py). Should-fix 5: lab leak cleanup looked only at the
worker's own vCenter. And a range built on an experimental backend stays destroyable after
the gate is switched off.

Everything here runs against fakes (httpx.MockTransport, fake pyVmomi objects); no vCenter.
"""

from __future__ import annotations

import base64
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import sqlalchemy as sa
import test_vsphere_provision as _vsp
from test_vsphere_metrics import FakeCollector
from test_vsphere_provision import RANGE_ID, FakeVM, _prov, _run
from worker import base_tasks, connection_binding, db_ops, lab_tasks, range_alloc, range_sweep, secretbox, tasks
from worker.provisioners import ConnectionBindingError, ExperimentalProvisionerError, get_provisioner
from worker.provisioners import vsphere_api as mod
from worker.provisioners.results import DestroyResult, HealthResult
from worker.tables import hypervisor_connections, ranges

vc = _vsp.vc  # the fake vCenter fixture
NOW = datetime(2026, 10, 11, tzinfo=UTC)
TENANT = str(uuid.uuid4())
PLATFORM_SECRET = "platform-VSPHERE_PASSWORD"


# -- a database with connections and ranges, the worker's _db_session on it ---
@pytest.fixture
def world(monkeypatch):
    eng = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False})
    meta = sa.MetaData()
    for table in (hypervisor_connections, ranges):
        cols = [sa.Column(c.name, c.type, primary_key=c.primary_key) for c in table.columns]  # NOT NULL relaxed
        sa.Table(table.name, meta, *cols)
    meta.create_all(eng)

    @contextmanager
    def session():
        with eng.begin() as db:
            yield db

    monkeypatch.setattr(tasks, "_db_session", session)
    monkeypatch.setattr(mod, "VSPHERE_URL", "https://vc-platform.test")
    monkeypatch.setattr(mod, "VSPHERE_USERNAME", "platform-svc")
    monkeypatch.setattr(mod, "VSPHERE_PASSWORD", PLATFORM_SECRET)

    def add_conn(host, *, password="pw-tenant", username="svc-tenant", primary=True, tenant=TENANT):
        cid = str(uuid.uuid4())
        with eng.begin() as db:
            db.execute(hypervisor_connections.insert().values(
                id=cid, name=host, hypervisor_type="vsphere", host=host, port=443, username=username,
                password_encrypted=secretbox.seal(password) if password else None, api_token=None, verify_ssl=False,
                is_primary=primary, is_active=True, datacenter="DC-T", tenant_id=tenant, created_at=NOW,
                updated_at=NOW))
        return cid

    def add_range(state="provisioning", output=None, tenant=TENANT):
        rid = str(uuid.uuid4())
        with eng.begin() as db:
            db.execute(ranges.insert().values(id=rid, tenant_id=tenant, name="r", state=state,
                                              provisioner_backend="vsphere_api",
                                              provisioner_output=json.dumps(output) if output else None))
        return rid

    def output(rid):
        with eng.begin() as db:
            raw = db.execute(sa.select(ranges.c.provisioner_output).where(ranges.c.id == rid)).scalar()
        return json.loads(raw) if raw else {}

    def run(stmt):
        with eng.begin() as db:
            db.execute(stmt)

    return SimpleNamespace(add_conn=add_conn, add_range=add_range, output=output, run=run)


def _auth_seen(handler_log):
    return [base64.b64decode(a.split(" ", 1)[1]).decode() for a in handler_log if a.startswith("Basic ")]


# ============================================================================
# Blocker 1: the platform's password never goes to a tenant's host
# ============================================================================
class TestNoPlatformCredentialsForATenantHost:
    def test_a_tenant_connection_without_a_password_is_refused_not_filled_from_the_env(self, world):
        world.add_conn("attacker.example", password=None)
        rid = world.add_range()
        with pytest.raises(ConnectionBindingError, match="password"):
            base_tasks._get_backend("vsphere_api", rid)

    @pytest.mark.parametrize("missing", ["host", "username", "password"])
    def test_incomplete_credentials_are_refused(self, missing):
        creds = {"host": "vc-t.test", "username": "u", "password": "p", **{missing: ""}}
        with pytest.raises(ConnectionBindingError, match=missing):
            mod.VsphereAPIProvisioner(creds)

    def test_only_the_tenants_login_reaches_the_tenants_host(self, world, monkeypatch):
        """Mocked transport: every request's host and Basic auth, through a real login."""
        world.add_conn("vc-tenant.test", password="pw-tenant")
        rid = world.add_range()
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host, request.headers.get("authorization", "")))
            return httpx.Response(201, json="token-1")

        prov = base_tasks._get_backend("vsphere_api", rid)
        prov._transport = httpx.MockTransport(handler)
        _run(prov._get_session())
        assert {h for h, _ in seen} == {"vc-tenant.test"}
        logins = _auth_seen([a for _, a in seen])
        assert logins == ["svc-tenant:pw-tenant"]
        assert all(PLATFORM_SECRET not in a for a in logins)

    def test_verify_ssl_comes_from_the_row_and_defaults_to_on(self, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_VERIFY_SSL", False)
        assert mod.VsphereAPIProvisioner({"host": "h", "username": "u", "password": "p"})._verify_ssl is True
        creds = {"host": "h", "username": "u", "password": "p", "verify_ssl": False}
        assert mod.VsphereAPIProvisioner(creds)._verify_ssl is False

    def test_a_range_with_no_tenant_connection_uses_the_platform_vcenter_whole(self, world):
        rid = world.add_range()
        prov = base_tasks._get_backend("vsphere_api", rid)
        assert (prov._base_url, prov._username, prov._password) == (
            "https://vc-platform.test", "platform-svc", PLATFORM_SECRET)


# ============================================================================
# Blocker 2a: the build's vCenter is recorded and every later operation reaches it
# ============================================================================
class TestTheBuildsVcenterIsKept:
    def test_a_build_records_its_connection_before_any_vm_exists(self, world):
        cid = world.add_conn("vc-a.test")
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        assert world.output(rid)["connection"] == {"id": cid, "host": "https://vc-a.test"}

    def test_the_stored_output_keeps_the_record(self, world):
        cid = world.add_conn("vc-a.test")
        prov = base_tasks._get_backend("vsphere_api", world.add_range())
        built = SimpleNamespace(vms=[{"name": "a", "vm_id": "vm-1"}], networks=[])
        out = range_alloc.stored_output("vsphere_api", "r", built, prov)
        assert out["connection"] == {"id": cid, "host": "https://vc-a.test"}

    def test_a_new_primary_connection_does_not_move_a_built_range(self, world):
        cid = world.add_conn("vc-a.test")
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        world.run(sa.update(hypervisor_connections).values(is_primary=False))
        world.add_conn("vc-new.test", password="pw-new")  # now the tenant's primary
        later = base_tasks._get_backend("vsphere_api", rid, teardown=True)
        assert later.connection_record() == {"id": cid, "host": "https://vc-a.test"}
        assert later._password == "pw-tenant"

    def test_a_deactivated_connection_still_serves_the_ranges_built_on_it(self, world):
        world.add_conn("vc-a.test")
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        world.run(sa.update(hypervisor_connections).values(is_active=False))
        assert base_tasks._get_backend("vsphere_api", rid)._base_url == "https://vc-a.test"

    def test_a_deleted_connection_fails_closed(self, world):
        world.add_conn("vc-a.test")
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        world.run(sa.delete(hypervisor_connections))
        world.add_conn("vc-new.test")  # there IS another connection: it must not be used
        with pytest.raises(ConnectionBindingError, match="no longer exists"):
            base_tasks._get_backend("vsphere_api", rid, teardown=True)

    def test_a_connection_repointed_to_another_host_fails_closed(self, world):
        world.add_conn("vc-a.test")
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        world.run(sa.update(hypervisor_connections).values(host="vc-elsewhere.test"))
        with pytest.raises(ConnectionBindingError, match="vc-elsewhere"):
            base_tasks._get_backend("vsphere_api", rid)

    def test_a_range_built_on_the_platform_vcenter_stays_there(self, world, monkeypatch):
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        assert world.output(rid)["connection"] == {"id": None, "host": "https://vc-platform.test"}
        world.add_conn("vc-tenant-later.test")  # the tenant adds its own afterwards
        assert base_tasks._get_backend("vsphere_api", rid)._base_url == "https://vc-platform.test"
        monkeypatch.setattr(mod, "VSPHERE_URL", "https://vc-platform-2.test")  # the worker env moved
        with pytest.raises(ConnectionBindingError):
            base_tasks._get_backend("vsphere_api", rid)

    def test_another_tenants_connection_is_never_used_even_if_recorded(self, world):
        theirs = world.add_conn("vc-theirs.test", tenant=str(uuid.uuid4()))
        rid = world.add_range(output={"connection": {"id": theirs, "host": "https://vc-theirs.test"}})
        with pytest.raises(ConnectionBindingError):
            base_tasks._get_backend("vsphere_api", rid)

    def test_a_range_with_nothing_recorded_keeps_todays_lookup(self, world):
        world.add_conn("vc-a.test")
        rid = world.add_range(state="ready", output={"vms": [{"name": "a", "vm_id": "vm-1"}]})
        prov = base_tasks._get_backend("vsphere_api", rid)
        assert prov._base_url == "https://vc-a.test"
        assert "connection" not in world.output(rid)  # only a build records

    def test_a_legacy_range_is_backfilled_once_confirmed_and_then_stays_put(self, world):
        """Review 2 should-fix 2d: the hook _get_backend sets writes the record, once."""
        cid = world.add_conn("vc-a.test")
        rid = world.add_range(state="ready", output={"vms": [{"name": "a", "vm_id": "vm-1"}]})
        prov = base_tasks._get_backend("vsphere_api", rid)
        prov.on_connection_confirmed(rid)
        assert world.output(rid)["connection"] == {"id": cid, "host": "https://vc-a.test"}
        world.run(sa.update(hypervisor_connections).values(is_primary=False))
        world.add_conn("vc-new.test")  # a new preferred connection no longer moves it
        assert base_tasks._get_backend("vsphere_api", rid)._base_url == "https://vc-a.test"
        assert connection_binding.record_confirmed(rid, prov) is False  # never over a record

    def test_a_range_with_a_record_gets_no_backfill_hook(self, world):
        world.add_conn("vc-a.test")
        rid = world.add_range()
        base_tasks._get_backend("vsphere_api", rid, build=True)
        assert base_tasks._get_backend("vsphere_api", rid).on_connection_confirmed is None

    def test_a_recorded_url_spelled_differently_still_matches(self, world):
        """verify compares id + (host, port), not URL strings."""
        cid = world.add_conn("vc-a.test")
        rid = world.add_range(output={"connection": {"id": cid, "host": "HTTPS://VC-A.test:443/"}})
        assert base_tasks._get_backend("vsphere_api", rid)._base_url == "https://vc-a.test"
        world.run(sa.update(hypervisor_connections).values(port=8443))  # now another endpoint
        with pytest.raises(ConnectionBindingError):
            base_tasks._get_backend("vsphere_api", rid)

    def test_binding_errors_are_final(self):
        from worker.fencing import FINAL_ERRORS

        assert issubclass(ConnectionBindingError, FINAL_ERRORS)


# ============================================================================
# Blocker 2b: no VM is deleted, powered or snapshotted unless it is the range's
# ============================================================================
def _tagged(vc, vm_id_name: str, range_id: str | None, name: str | None = None) -> FakeVM:
    vm = FakeVM(name or vm_id_name, [])
    vm._moId = vm_id_name
    if range_id:
        vm.config.annotation = mod.range_annotation(range_id)
    vc.vms[vm._moId] = vm
    vc.power[vm._moId] = "POWERED_ON"
    return vm


R8 = RANGE_ID[:8]
OTHER_RANGE = "99999999-0000-0000-0000-000000000000"


class TestOwnershipCheck:
    def test_destroy_skips_a_vm_of_another_range_and_keeps_the_port_groups(self, vc):
        _tagged(vc, "vm-1", RANGE_ID, f"{R8}-web")
        _tagged(vc, "vm-2", OTHER_RANGE, "prod-dc01")  # same id, a different vCenter's VM
        out = {"vms": [{"name": f"{R8}-web", "vm_id": "vm-1"}, {"name": f"{R8}-dc01", "vm_id": "vm-2"}],
               "networks": [{"name": "lan", "portgroup": f"tn-{R8}-v100"}]}
        result = _run(_prov(vc).destroy(RANGE_ID, out))
        assert "vm-1" not in vc.vms  # its own VM: deleted
        assert "vm-2" in vc.vms  # not its own: untouched
        assert result.status == "partial" and any("vm-2" in e and OTHER_RANGE in e for e in result.errors)
        assert not [r for r in vc.requests if r[0] == "DELETE" and "vm-2" in r[1]]

    def test_destroy_deletes_nothing_when_every_id_names_someone_elses_vm(self, vc):
        _tagged(vc, "vm-7", None, "finance-db")  # untagged, unrelated name
        result = _run(_prov(vc).destroy(RANGE_ID, {"vms": [{"name": f"{R8}-web", "vm_id": "vm-7"}]}))
        assert result.status == "failed" and "vm-7" in vc.vms

    def test_a_vm_built_before_the_tag_is_still_its_ranges_by_name(self, vc):
        _tagged(vc, "vm-3", None, f"{R8}-old")
        result = _run(_prov(vc).destroy(RANGE_ID, {"vms": [{"name": f"{R8}-old", "vm_id": "vm-3"}]}))
        assert result.status == "ok" and "vm-3" not in vc.vms

    def test_a_vm_already_gone_still_counts_as_deleted(self, vc):
        prov = _prov(vc)
        out = {"vms": [{"name": f"{R8}-x", "vm_id": "vm-404"}], "connection": prov.connection_record()}
        result = _run(prov.destroy(RANGE_ID, out))
        assert result.status == "ok", result.errors

    def test_a_range_with_nothing_recorded_and_none_of_its_vms_here_is_not_destroyed(self, vc):
        """Review 2 should-fix 2a: likely the wrong vCenter; 404s must not count as deleted."""
        out = {"vms": [{"name": f"{R8}-x", "vm_id": "vm-404"}, {"name": f"{R8}-y", "vm_id": "vm-405"}]}
        result = _run(_prov(vc).destroy(RANGE_ID, out))
        assert result.status == "failed" and "none of the range's 2 recorded VMs" in result.errors[0]

    def test_one_of_its_vms_found_backfills_the_record_so_a_retry_can_finish(self, vc):
        _tagged(vc, "vm-1", RANGE_ID, f"{R8}-web")
        prov, seen = _prov(vc), []
        prov.on_connection_confirmed = seen.append
        out = {"vms": [{"name": f"{R8}-web", "vm_id": "vm-1"}, {"name": f"{R8}-old", "vm_id": "vm-404"}]}
        assert _run(prov.destroy(RANGE_ID, out)).status == "ok"
        assert seen == [RANGE_ID]
        # The retry (every VM gone now) reads the record the hook wrote, and finishes.
        again = _run(_prov(vc).destroy(RANGE_ID, {**out, "connection": prov.connection_record()}))
        assert again.status == "ok", again.errors

    def test_a_vm_of_another_range_confirms_nothing(self, vc):
        _tagged(vc, "vm-2", OTHER_RANGE)
        prov, seen = _prov(vc), []
        prov.on_connection_confirmed = seen.append
        _run(prov.stop(RANGE_ID, {"vms": [{"name": f"{R8}-a", "vm_id": "vm-2"}]}))
        assert seen == []

    def test_health_backfills_a_range_with_nothing_recorded(self, vc):
        _tagged(vc, "vm-1", RANGE_ID, f"{R8}-web")
        prov, seen = _prov(vc), []
        prov.on_connection_confirmed = seen.append
        _run(prov.health_check(RANGE_ID, {"vms": [{"name": f"{R8}-web", "vm_id": "vm-1"}]}))
        assert seen == [RANGE_ID]
        seen.clear()  # recorded already: no ownership read at all
        recorded = {"vms": [{"name": f"{R8}-web", "vm_id": "vm-1"}], "connection": prov.connection_record()}
        _run(prov.health_check(RANGE_ID, recorded))
        assert seen == []

    def test_power_leaves_another_ranges_vm_alone_and_says_so(self, vc):
        _tagged(vc, "vm-1", RANGE_ID, f"{R8}-web")
        _tagged(vc, "vm-2", OTHER_RANGE)
        out = {"vms": [{"name": f"{R8}-web", "vm_id": "vm-1"}, {"name": f"{R8}-dc", "vm_id": "vm-2"}]}
        result = _run(_prov(vc).stop(RANGE_ID, out))
        assert (vc.power["vm-1"], vc.power["vm-2"]) == ("POWERED_OFF", "POWERED_ON")
        assert result.status == "partial" and result.vms_stopped == 1

    def test_snapshots_only_touch_the_ranges_own_vms(self, monkeypatch):
        collector = FakeCollector({
            "vm-1": {"name": f"{R8}-a", "config.annotation": mod.range_annotation(RANGE_ID)},
            "vm-2": {"name": "prod", "config.annotation": mod.range_annotation(OTHER_RANGE)},
        })
        si = SimpleNamespace(_stub=None, content=SimpleNamespace(propertyCollector=collector))
        monkeypatch.setattr(mod, "SmartConnect", MagicMock(return_value=si))
        monkeypatch.setattr(mod, "Disconnect", MagicMock())
        acted: list[list[str]] = []

        def fn(vm_ids, name):
            acted.append(list(vm_ids))
            return {}, set(vm_ids)

        prov = mod.VsphereAPIProvisioner()
        out = {"vms": [{"name": f"{R8}-a", "vm_id": "vm-1"}, {"name": f"{R8}-b", "vm_id": "vm-2"}]}
        done, errors, _ = _run(prov._vim_op(RANGE_ID, out, fn, "tnsnap"))
        assert acted == [["vm-1"]] and done == 1 and "vm-2" in errors[0]
        assert collector.calls[0][1] == list(mod.OWNER_PATHS)
        assert mod.SmartConnect.call_count == 1  # one session: the check and the operation

    def test_a_failed_ownership_read_touches_nothing(self, monkeypatch):
        monkeypatch.setattr(mod, "SmartConnect", MagicMock(side_effect=OSError("unreachable")))
        fn = MagicMock()
        prov = mod.VsphereAPIProvisioner()
        done, errors, _ = _run(prov._vim_op(RANGE_ID, {"vms": [{"name": "a", "vm_id": "vm-1"}]}, fn, "x"))
        assert done == 0 and errors and not fn.called


# ============================================================================
# Experimental backends: destroy is allowed after the gate is switched off
# ============================================================================
def test_an_experimental_backend_can_still_be_torn_down(monkeypatch):
    monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)
    with pytest.raises(ExperimentalProvisionerError):
        get_provisioner("proxmox_api")
    assert get_provisioner("proxmox_api", teardown=True) is not None
    assert base_tasks._get_backend("proxmox_api", None, teardown=True) is not None
    # destroy_range asks for teardown: test_vsphere_tenant_credentials.test_destroy_uses_the_ranges_connection


# ============================================================================
# Should-fix 4: one login per vCenter per scheduled run, and a logout
# ============================================================================
class TestScheduledRunsShareALogin:
    def _rows(self, n, output=None):
        out = json.dumps(output or {"vms": [{"name": "a", "vm_id": "vm-1"}]})
        return [(str(uuid.uuid4()), f"r{i}", out, "vsphere_api") for i in range(n)]

    def test_ranges_on_one_vcenter_share_one_login_and_it_is_logged_out(self, vc, monkeypatch):
        _tagged(vc, "vm-1", None)
        monkeypatch.setattr(base_tasks, "_get_backend", lambda b, rid=None, **kw: _prov(vc))  # a new instance each
        checks = range_sweep.health_checks(self._rows(5))
        assert all(isinstance(r, HealthResult) and r.healthy for r in checks.values())
        assert vc.logins == 1
        assert [r for r in vc.requests if r[:2] == ("DELETE", "/api/session")]

    def test_two_connections_two_logins(self, vc, monkeypatch):
        _tagged(vc, "vm-1", None)
        rows = self._rows(6)
        conn_of = {row[0]: ("conn-a" if i % 2 else "conn-b") for i, row in enumerate(rows)}

        def get(b, rid=None, **kw):
            prov = _prov(vc)
            prov._connection_id = conn_of[rid]
            return prov

        monkeypatch.setattr(base_tasks, "_get_backend", get)
        range_sweep.health_checks(rows)
        assert vc.logins == 2

    def test_a_range_whose_connection_is_gone_is_reported_and_the_rest_are_checked(self, vc, monkeypatch):
        _tagged(vc, "vm-1", None)
        rows = self._rows(2)

        def get(b, rid=None, **kw):
            if rid == rows[0][0]:
                raise ConnectionBindingError("gone")
            return _prov(vc)

        monkeypatch.setattr(base_tasks, "_get_backend", get)
        checks = range_sweep.health_checks(rows)
        assert isinstance(checks[rows[0][0]], ConnectionBindingError)
        assert checks[rows[1][0]].healthy
        with pytest.raises(ConnectionBindingError):
            range_sweep.result(checks, rows[0][0])

    def test_the_health_task_reads_each_range_once_through_the_sweep(self, monkeypatch):
        rows = self._rows(3)

        @contextmanager
        def session():
            yield None

        monkeypatch.setattr(tasks, "_db_session", session)
        monkeypatch.setattr(tasks.db_ops, "active_ranges", lambda db: rows)
        monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
        backend = MagicMock(health_check=AsyncMock(return_value=HealthResult(healthy=True)))
        backend.connection_record.return_value = {"id": "c", "host": "https://h"}
        monkeypatch.setattr(base_tasks, "_get_backend", lambda *a, **k: backend)
        assert tasks.health_check_ranges.run()["healthy"] == 3
        backend.session.assert_called_once()  # one scope (one login) for the three ranges


# ============================================================================
# Should-fix 5: lab leak cleanup looks on each range's own vCenter
# ============================================================================
class _LabProv:
    def __init__(self, host, inventory):
        self.host, self.inventory, self.destroyed = host, inventory, []
        self.listings = 0

    def connection_record(self):
        return {"id": self.host, "host": f"https://{self.host}"}

    async def find_vms(self, prefix):
        self.listings += 1
        return list(self.inventory)

    async def destroy(self, range_id, output):
        self.destroyed.append((range_id, [v["vm_id"] for v in output["vms"]]))
        return DestroyResult(status="ok", resources_removed=len(output["vms"]))


def test_lab_cleanup_finds_each_ranges_leftovers_on_its_own_vcenter(monkeypatch):
    ra, rb, rc, broken = (str(uuid.uuid4()) for _ in range(4))
    vc_a = _LabProv("vc-a", [{"vm_id": "vm-1", "name": "x", "range_id": ra},
                             {"vm_id": "vm-2", "name": "y", "range_id": rc}])
    vc_b = _LabProv("vc-b", [{"vm_id": "vm-1", "name": "z", "range_id": rb}])  # same id, other vCenter
    where = {ra: vc_a, rc: vc_a, rb: vc_b}
    asked = []

    def get(backend, range_id=None, **kw):
        asked.append((range_id, kw.get("teardown")))
        if range_id == broken:
            raise ConnectionBindingError("its connection is gone")
        return where[range_id]

    monkeypatch.setattr(lab_tasks, "_get_backend", get)
    out = lab_tasks.reconcile_lab_vms.run([ra, rb, rc, broken], "vsphere_api")
    assert vc_a.destroyed == [(ra, ["vm-1"]), (rc, ["vm-2"])]
    assert vc_b.destroyed == [(rb, ["vm-1"])]
    assert (vc_a.listings, vc_b.listings) == (1, 1)  # one inventory listing per vCenter
    assert out["removed"] == {ra: 1, rb: 1, rc: 1}
    assert broken in out["unresolved"]
    assert all(t is True for _, t in asked)  # teardown: allowed past the experimental gate


def test_db_creds_by_id_never_returns_another_tenants_row(world):
    theirs = world.add_conn("vc-x.test", tenant=str(uuid.uuid4()))
    rid = world.add_range()
    with tasks._db_session() as db:
        assert db_ops.hypervisor_creds_by_id(db, "vsphere", theirs, rid) == {}
    assert connection_binding.recorded({"connection": {"id": None}}) is None  # no host: not a record


# ============================================================================
# The connection's port (coordinator follow-up, 2026-10-11)
# ============================================================================
@pytest.mark.parametrize(("host", "port", "url"), [
    ("vc.test", 443, "https://vc.test"),
    ("vc.test", None, "https://vc.test"),
    ("vc.test", 8443, "https://vc.test:8443"),
    ("vc.test", 8006, "https://vc.test"),  # the API's old default for every family: never a vCenter port
    ("vc.test:9443", 8443, "https://vc.test:9443"),  # a port typed into the host wins
    ("https://vc.test/", "8443", "https://vc.test:8443"),
    ("fd00::10", None, "https://[fd00::10]"),  # a bare IPv6 address is bracketed
    ("fd00::10", 8443, "https://[fd00::10]:8443"),
    ("[fd00::10]", 443, "https://[fd00::10]"),
    ("[fd00::10]:9443", 8443, "https://[fd00::10]:9443"),
])
def test_the_connections_port_reaches_the_endpoint(host, port, url):
    assert mod.endpoint_url(host, port) == url


def test_an_ipv6_vcenter_reaches_pyvmomi_unbracketed(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(mod, "SmartConnect", connect)
    mod.VsphereAPIProvisioner({"host": "fd00::10", "port": 8443, "username": "u", "password": "p"})._connect()
    assert (connect.call_args.kwargs["host"], connect.call_args.kwargs["port"]) == ("fd00::10", 8443)


@pytest.mark.parametrize("host", ["https://fd00::10", "vc.test:notaport", "https://"])
def test_an_unusable_address_is_a_binding_error_not_a_crash(host):
    with pytest.raises(ConnectionBindingError):
        mod.VsphereAPIProvisioner({"host": host, "username": "u", "password": "p"})


def test_rest_and_pyvmomi_both_use_the_connections_port(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.port))
        return httpx.Response(201, json="tok")

    prov = mod.VsphereAPIProvisioner({"host": "vc.test", "port": 8443, "username": "u", "password": "p"})
    prov._transport = httpx.MockTransport(handler)
    _run(prov._get_session())
    assert seen == [("vc.test", 8443)]
    connect = MagicMock()
    monkeypatch.setattr(mod, "SmartConnect", connect)
    prov._connect()
    assert (connect.call_args.kwargs["host"], connect.call_args.kwargs["port"]) == ("vc.test", 8443)
    assert prov.connection_record()["host"] == "https://vc.test:8443"
