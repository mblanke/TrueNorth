"""The setup reconcile task (worker/setup_reconcile.py): it reads a ``running`` product /
Security Onion setup's status in the guest later and records the real outcome, with secrets
re-derived each run (never stored). The guest reads themselves are the provisioner's
(test_vsphere_provision.py); here the task's own logic is checked with fakes."""

from __future__ import annotations

import json
import time

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from worker import db_ops
from worker import setup_reconcile as sr
from worker.provisioners.vsphere_guest import RangeAccount, Secret


def _product_vm(name="r1-exch", status="running", since=None):
    return {"name": name, "vm_id": "vm-1", "role_setup": {"status": status, "detail": "being set up",
            "image": "srv2022-exchange2019",
            "reconcile": {"role": "exchange", "dc_node": "dc01", "netbios": "CORP", "domain": "corp.local",
                          "status_file": "C:\\x", "since": time.time() if since is None else since,
                          "timeout": 7200}}}


def _so_vm(name="r1-so", since=None, admin_last=False):
    return {"name": name, "vm_id": "vm-2", "securityonion": {"status": "running", "detail": "x",
            "web_url": "https://10.60.202.60",
            "reconcile": {"kind": "securityonion", "node": "so01", "web_user": "analyst@range.local",
                          "web_account": "Analyst", "admin_user": "soadmin", "install_from_env": False,
                          "admin_last": admin_last, "since": time.time() if since is None else since,
                          "timeout": 7200}}}


def test_running_picks_out_only_running_setups():
    out = {"vms": [_product_vm(), _product_vm("done", "ok"), _so_vm(), {"name": "plain", "vm_id": "v3"}]}
    products, sos = sr._running(out)
    assert [v["name"] for v in products] == ["r1-exch"] and [v["name"] for v in sos] == ["r1-so"]


def test_domain_passwords_come_from_the_range_never_the_site_default():
    declared = {"dc01": [RangeAccount("Administrator", Secret("Range-Own-1!"), admin=True)]}
    assert sr._domain_passwords(declared, [_product_vm()]) == {"dc01": "Range-Own-1!"}
    assert sr._domain_passwords({}, [_product_vm()]) == {}  # no account: no login, ends by its timeout


def test_so_logins_carry_the_declared_admin_password():
    env = {"VSPHERE_SO_INSTALL_USER": "builder", "VSPHERE_SO_INSTALL_PASSWORD": "Build-1!"}
    declared = {"so01": [RangeAccount("soadmin", Secret("Range-1!"), admin=True)]}
    vm = _so_vm()
    vm["securityonion"]["reconcile"]["install_from_env"] = True
    assert sr._so_logins(declared, [vm], env) == ({"so01": ("builder", "Build-1!", "Range-1!")}, [])
    vm["securityonion"]["reconcile"]["install_from_env"] = False
    assert sr._so_logins(declared, [vm], {}) == ({"so01": ("soadmin", "Range-1!", "Range-1!")}, [])
    assert sr._so_logins({}, [vm], {}) == ({}, [])


def test_an_admin_that_is_the_build_login_is_never_tried_without_the_build_login():
    # admin_last: the declared password is not set yet, so it cannot log in: skip, warn, no login.
    declared = {"so01": [RangeAccount("soadmin", Secret("Range-1!"), admin=True)]}
    vm = _so_vm(admin_last=True)
    vm["securityonion"]["reconcile"]["install_from_env"] = True
    logins, warnings = sr._so_logins(declared, [vm], {})  # VSPHERE_SO_INSTALL_* not set here
    assert logins == {} and any("VSPHERE_SO_INSTALL_USER" in w for w in warnings)


def test_merge_keeps_what_the_build_stored_and_drops_the_being_set_up_warning():
    output = {"vms": [_product_vm(), _so_vm()],
              "warnings": ["VM r1-exch: Exchange 2019 setup running: being set up",
                           "VM r1-so: Security Onion setup running: continues in the guest", "keep me"]}
    changes = {
        "r1-exch": {"role_setup": {"status": "failed", "detail": "Exchange /Mode:Install failed"}, "role": "exchange"},
        "r1-so": {"securityonion": {"status": "ok", "detail": "healthy"}, "node": "so01",
                  "accounts_applied": ["Analyst", "soadmin"]},
    }
    merged, applied = sr._merge(output, changes)
    exch = next(v for v in merged["vms"] if v["name"] == "r1-exch")
    assert exch["role_setup"]["status"] == "failed" and exch["role_setup"]["image"] == "srv2022-exchange2019"
    assert "reconcile" not in exch["role_setup"] and exch["roles"]["exchange"]["status"] == "failed"
    so = next(v for v in merged["vms"] if v["name"] == "r1-so")
    assert so["securityonion"]["status"] == "ok" and so["securityonion"]["web_url"] == "https://10.60.202.60"
    assert so["accounts_applied"] == ["Analyst", "soadmin"]
    assert "keep me" in merged["warnings"]
    assert "VM r1-exch: setup failed: Exchange /Mode:Install failed" in merged["warnings"]
    assert not [w for w in merged["warnings"] if "being set up" in w or "continues in the guest" in w]
    assert applied == [{"name": "r1-so", "node_id": "so01", "accounts_applied": ["Analyst", "soadmin"]}]


@pytest.fixture
def world(monkeypatch):
    """Fake DB helpers, accounts and provisioner for _reconcile_range."""
    import worker.range_accounts as ra
    import worker.tasks as tasks

    state = {"rows_written": 1, "marked": None, "written": None, "only": None, "lab": False, "backend_calls": 0,
             "changes": {"r1-so": {"securityonion": {"status": "ok", "detail": "healthy"}, "node": "so01",
                                   "accounts_applied": ["Analyst"]}}}

    class FakeProv:
        supports_setup_reconcile = True

        async def reconcile_setups(self, range_id, out, **kw):
            return state["changes"], []

    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def backend(*a, **k):
        state["backend_calls"] += 1
        return FakeProv()

    def set_output(db, range_id, out, *, only_states):
        state["written"], state["only"] = json.loads(out), only_states
        return state["rows_written"]

    monkeypatch.setattr(sr, "run_async", lambda coro: __import__("asyncio").run(coro))
    monkeypatch.setattr(tasks, "_db_session", lambda: FakeDB())
    monkeypatch.setattr(tasks, "_get_backend", backend)
    monkeypatch.setattr(ra, "load", lambda session, rid: ({}, state["lab"]))
    monkeypatch.setattr(db_ops, "lock_range", lambda db, rid: None)
    monkeypatch.setattr(db_ops, "range_output_and_backend", lambda db, rid: (json.dumps({"vms": [_so_vm()]}), "v"))
    monkeypatch.setattr(db_ops, "set_range_output", set_output)
    monkeypatch.setattr(db_ops, "mark_accounts_applied", lambda db, rid, pairs: state.__setitem__("marked", pairs))
    return state


def test_a_stopped_or_restarted_range_is_still_reconciled(world):
    # stop/start moves a range to stopped/running; it never returns to ready.
    assert set(db_ops.LIVE_RANGE_STATES) == {"ready", "running", "stopping", "stopped", "starting"}
    sr._reconcile_range("r1", "Range 1", {"vms": [_so_vm()]}, "vsphere")
    assert world["only"] == db_ops.LIVE_RANGE_STATES
    assert world["written"]["vms"][0]["securityonion"]["status"] == "ok"
    assert world["marked"] == {("so01", "Analyst")}  # the declared name, in the same transaction


def test_accounts_are_marked_applied_only_when_the_guarded_write_happened(world):
    world["rows_written"] = 0  # the range was destroyed meanwhile
    sr._reconcile_range("r1", "Range 1", {"vms": [_so_vm()]}, "vsphere")
    assert world["marked"] is None


def test_a_lab_sessions_range_is_skipped(world):
    world["lab"] = True
    sr._reconcile_range("r1", "Range 1", {"vms": [_product_vm()]}, "vsphere")
    assert world["backend_calls"] == 0 and world["written"] is None


def _ip_pending_vm(name="r1-ubu02"):
    return {"name": name, "vm_id": "vm-2", "ip": "", "planned_ip": "10.70.10.12", "ip_verified": None,
            "ip_check": {"status": "running", "planned": ["10.70.10.12"], "deadline": time.time() + 600}}


@pytest.fixture
def ip_world(world, monkeypatch):
    """A range the build left ready with a guest network check still running (its budget
    ran out first, vsphere_readiness.assess); the provisioner's verdict is ``world["ips"]``."""
    import worker.tasks as tasks

    output = {"vms": [_ip_pending_vm()], "warnings": [
        "VM r1-ubu02: guest network not verified yet (the build's time ran out within 40s of power-on); x"]}
    world.update(ips={}, states=[], notified=[])

    class Prov:
        async def reconcile_guest_ips(self, range_id, out):
            return world["ips"]

    def state(db, range_id, new_state, **kw):
        world["states"].append((new_state, kw))
        return 1

    monkeypatch.setattr(tasks, "_get_backend", lambda *a, **k: Prov())
    monkeypatch.setattr(tasks, "_notify_api", lambda channel, msg: world["notified"].append(msg))
    monkeypatch.setattr(db_ops, "update_range_state", state)
    monkeypatch.setattr(db_ops, "range_output_and_backend", lambda db, rid: (json.dumps(output), "v"))
    return output


def test_a_guest_network_check_left_running_settles_ready_once_the_address_shows(ip_world, world):
    world["ips"] = {"r1-ubu02": {"ip": "10.70.10.12", "guest_ips": ["10.70.10.12"], "ip_verified": True,
                                 "ip_check": {"status": "ok"}}}
    assert sr._ip_pending(ip_world) == ip_world["vms"]
    sr._reconcile_range("r1", "Range 1", ip_world, "vsphere")
    (vm,) = world["written"]["vms"]
    assert (vm["ip"], vm["ip_verified"], vm["ip_check"]) == ("10.70.10.12", True, {"status": "ok"})
    assert world["written"]["warnings"] == []  # "not verified yet" gone
    assert world["states"] == [] and world["notified"] == []  # still ready


def test_a_guest_network_check_past_its_window_fails_the_ready_range(ip_world, world):
    reason = "planned address 10.70.10.12 not reported by VMware Tools before its readiness window ended"
    world["ips"] = {"r1-ubu02": {"ip": "", "guest_ips": [], "ip_verified": False,
                                 "ip_check": {"status": "failed", "detail": reason}, "not_ready": reason}}
    sr._reconcile_range("r1", "Range 1", ip_world, "vsphere")
    assert world["written"]["not_ready"] == ["r1-ubu02"] and "not_ready" not in world["written"]["vms"][0]
    ((new, kw),) = world["states"]
    assert new == "failed" and kw["only_from"] == ("ready",)  # never over a range Students started
    assert kw["error"] == f"1 VM(s) built but not ready: VM r1-ubu02: {reason}"
    assert world["notified"] == [{"id": "r1", "state": "failed", "error": kw["error"]}]


def test_a_guest_network_check_still_inside_its_window_writes_nothing(ip_world, world):
    world["ips"] = {}  # the provisioner's settle: None while the window is open
    sr._reconcile_range("r1", "Range 1", ip_world, "vsphere")
    assert world["written"] is None and world["states"] == []


def test_the_query_covers_every_live_state():
    from sqlalchemy.dialects import postgresql

    class Rec:
        def execute(self, stmt):
            Rec.sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            return type("R", (), {"fetchall": lambda self: []})()

    db_ops.ranges_with_running_setups(Rec())
    for s in ("ready", "running", "stopping", "stopped", "starting"):
        assert f"'{s}'" in Rec.sql
    assert "'destroying'" not in Rec.sql and "'failed'" not in Rec.sql


class _Redis:
    def __init__(self, held=False):
        self.store = {sr.LOCK_KEY: b"other"} if held else {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value.encode()
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


def test_runs_never_overlap():
    held = _Redis(held=True)
    with sr._single_run(held) as mine:
        assert mine is False
    free = _Redis()
    with sr._single_run(free) as mine:
        assert mine is True and sr.LOCK_KEY in free.store
    assert sr.LOCK_KEY not in free.store  # released


def test_the_task_reraises_its_soft_time_limit_and_survives_one_ranges_failure(monkeypatch):
    import worker.tasks as tasks

    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    rows = [("r1", "a", json.dumps({"vms": []}), "v"), ("r2", "b", json.dumps({"vms": []}), "v")]
    monkeypatch.setattr(tasks, "_db_session", lambda: FakeDB())
    monkeypatch.setattr(sr, "db_ops_rows", lambda db: rows)
    monkeypatch.setattr(sr, "_single_run", lambda: _Ctx())
    seen = []

    def flaky(range_id, *a):
        seen.append(range_id)
        if range_id == "r1":
            raise RuntimeError("vCenter unreachable")

    monkeypatch.setattr(sr, "_reconcile_range", flaky)
    assert sr.reconcile_setups.run()["ranges"] == 1 and seen == ["r1", "r2"]

    def limit(range_id, *a):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(sr, "_reconcile_range", limit)
    with pytest.raises(SoftTimeLimitExceeded):
        sr.reconcile_setups.run()


class _Ctx:
    def __enter__(self):
        return True

    def __exit__(self, *a):
        return False


def test_the_stale_unchanged_password_warning_goes_once_the_admin_password_is_set():
    vm = _so_vm(admin_last=True)
    stale = ("VM r1-so: Security Onion setup: the password of admin account soadmin (also the build login) "
             "is set only once the node is healthy; it is unchanged")
    out = {"vms": [vm], "warnings": [stale, "keep me"]}
    change = {"r1-so": {"securityonion": {"status": "ok", "detail": "healthy"}, "node": "so01",
                        "accounts_applied": ["soadmin"]}}
    merged, _ = sr._merge(out, change)
    assert stale not in merged["warnings"] and "keep me" in merged["warnings"]
    # Not applied (its declared password did not take): the warning stays true, so it stays.
    out = {"vms": [_so_vm(admin_last=True)], "warnings": [stale]}
    change["r1-so"]["accounts_applied"] = []
    merged, _ = sr._merge(out, change)
    assert stale in merged["warnings"]


def test_an_expired_setup_ends_even_when_vcenter_is_unreachable(world, monkeypatch):
    import worker.tasks as tasks

    def unreachable(*a, **k):
        raise ConnectionError("vCenter unreachable")

    monkeypatch.setattr(tasks, "_get_backend", unreachable)
    sr._reconcile_range("r1", "Range 1", {"vms": [_so_vm(since=0.0)]}, "vsphere")
    assert world["written"]["vms"][0]["securityonion"]["status"] == "timeout"


def test_an_expired_setup_ends_when_screening_refuses(world):
    world["changes"] = {}  # the provisioner returned only a screening note, no outcome
    sr._reconcile_range("r1", "Range 1", {"vms": [_so_vm(since=0.0)]}, "vsphere")
    assert world["written"]["vms"][0]["securityonion"]["status"] == "timeout"


def test_an_expired_setup_ends_on_a_lab_range_too(world, monkeypatch):
    world["lab"] = True  # no guest work at all, but the record still ends
    output = {"vms": [_product_vm(since=0.0)]}
    monkeypatch.setattr(db_ops, "range_output_and_backend", lambda db, rid: (json.dumps(output), "v"))
    sr._reconcile_range("r1", "Range 1", output, "vsphere")
    assert world["backend_calls"] == 0
    assert world["written"]["vms"][0]["role_setup"]["status"] == "timeout"


def test_a_live_setup_with_an_unreachable_vcenter_is_left_running(world, monkeypatch):
    import worker.tasks as tasks

    monkeypatch.setattr(tasks, "_get_backend", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    sr._reconcile_range("r1", "Range 1", {"vms": [_so_vm()]}, "vsphere")
    assert world["written"] is None  # nothing to record; the next run tries again


def test_the_lock_outlives_the_tasks_soft_time_limit():
    from worker.celery_app import app

    soft = app.conf.task_soft_time_limit
    assert soft and sr.lock_ttl() >= soft + 300 and sr.lock_ttl() >= sr.RUN_SECONDS + 300
    seen = {}

    class Rec(_Redis):
        def set(self, key, value, nx=False, ex=None):
            seen["ex"] = ex
            return super().set(key, value, nx=nx, ex=ex)

    with sr._single_run(Rec()):
        pass
    assert seen["ex"] == sr.lock_ttl()
