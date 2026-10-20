"""The setup reconcile task (worker/setup_reconcile.py): it reads a ``running`` product /
Security Onion setup's status in the guest later and records the real outcome, with secrets
re-derived each run (never stored). The guest reads themselves are the provisioner's
(test_vsphere_provision.py); here the task's own logic is checked with fakes."""

from __future__ import annotations

import json

from worker import setup_reconcile as sr
from worker.provisioners.vsphere_guest import RangeAccount, Secret


def _product_vm(name="r1-exch", status="running"):
    return {"name": name, "vm_id": "vm-1", "role_setup": {"status": status, "detail": "being set up",
            "reconcile": {"role": "exchange", "dc_node": "dc01", "netbios": "CORP", "domain": "corp.local",
                          "status_file": "C:\\x", "since": 0.0, "timeout": 7200}}}


def _so_vm(name="r1-so"):
    return {"name": name, "vm_id": "vm-2", "securityonion": {"status": "running", "detail": "x",
            "reconcile": {"kind": "securityonion", "node": "so01", "web_user": "analyst@range.local",
                          "admin_user": "soadmin", "install_from_env": False, "since": 0.0, "timeout": 7200}}}


def test_running_picks_out_only_running_setups():
    out = {"vms": [_product_vm(), _product_vm("done", "ok"), _so_vm(),
                   {"name": "plain", "vm_id": "v3"}]}
    products, sos = sr._running(out)
    assert [v["name"] for v in products] == ["r1-exch"]
    assert [v["name"] for v in sos] == ["r1-so"]


def test_domain_passwords_prefer_the_declared_dc_admin_then_the_default():
    declared = {"dc01": [RangeAccount("Administrator", Secret("Declared-1!"), admin=True)]}
    pw = sr._domain_passwords(declared, [_product_vm()], Secret("Default-9!"))
    assert pw == {"dc01": "Declared-1!"}
    # No declared Administrator for the DC: fall back to the template default.
    assert sr._domain_passwords({}, [_product_vm()], Secret("Default-9!")) == {"dc01": "Default-9!"}
    # Neither: the DC is absent from the map (reconcile warns, does not log in).
    assert sr._domain_passwords({}, [_product_vm()], None) == {}


def test_so_logins_from_env_or_the_declared_admin():
    env = {"VSPHERE_SO_INSTALL_USER": "builder", "VSPHERE_SO_INSTALL_PASSWORD": "Build-1!"}
    vm = _so_vm()
    vm["securityonion"]["reconcile"]["install_from_env"] = True
    assert sr._so_logins({}, [vm], env) == {"so01": ("builder", "Build-1!")}
    vm["securityonion"]["reconcile"]["install_from_env"] = False
    declared = {"so01": [RangeAccount("soadmin", Secret("Range-1!"), admin=True)]}
    assert sr._so_logins(declared, [vm], {}) == {"so01": ("soadmin", "Range-1!")}


def test_merge_records_the_outcome_and_drops_the_being_set_up_warning():
    output = {"vms": [_product_vm(), _so_vm()],
              "warnings": ["VM r1-exch: Exchange 2019 setup running: being set up",
                           "VM r1-so: Security Onion setup running: continues in the guest", "keep me"]}
    changes = {
        "r1-exch": {"role_setup": {"status": "failed", "detail": "Exchange /Mode:Install failed"}, "role": "exchange"},
        "r1-so": {"securityonion": {"status": "ok", "detail": "healthy"}, "node": "so01",
                  "accounts_applied": ["soadmin", "analyst@range.local"]},
    }
    merged, applied = sr._merge(output, changes)
    exch = next(v for v in merged["vms"] if v["name"] == "r1-exch")
    assert exch["role_setup"]["status"] == "failed" and exch["roles"]["exchange"]["status"] == "failed"
    so = next(v for v in merged["vms"] if v["name"] == "r1-so")
    assert so["securityonion"]["status"] == "ok" and so["accounts_applied"] == ["soadmin", "analyst@range.local"]
    assert "keep me" in merged["warnings"]
    assert not [w for w in merged["warnings"] if "being set up" in w or "continues in the guest" in w]
    assert "VM r1-exch: setup failed: Exchange /Mode:Install failed" in merged["warnings"]
    assert not [w for w in merged["warnings"] if "r1-so" in w]  # ok: no warning
    assert applied == [{"name": "r1-so", "node_id": "so01", "accounts_applied": ["soadmin", "analyst@range.local"]}]


def test_reconcile_range_writes_back_only_while_ready(monkeypatch):
    output = {"vms": [_product_vm()], "warnings": ["VM r1-exch: Exchange 2019 setup running: being set up"]}
    writes: dict = {}

    class FakeProv:
        supports_setup_reconcile = True

        async def reconcile_setups(self, range_id, out, *, domain_passwords, so_logins):
            assert domain_passwords == {"dc01": "Default-9!"}
            return {"r1-exch": {"role_setup": {"status": "ok", "detail": "set up"}, "role": "exchange"}}, []

    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    stored = {"out": json.dumps(output)}

    def fake_set_output(db, range_id, out, *, only_states=("ready",)):
        writes["out"] = json.loads(out)
        return 1

    monkeypatch.setattr(sr, "run_async", lambda coro: __import__("asyncio").run(coro))
    import worker.db_ops as db_ops
    import worker.range_accounts as ra
    import worker.tasks as tasks
    monkeypatch.setattr(tasks, "_db_session", lambda: FakeDB())
    monkeypatch.setattr(tasks, "_get_backend", lambda *a, **k: FakeProv())
    monkeypatch.setattr(ra, "load", lambda session, rid: ({}, False))
    monkeypatch.setattr(ra, "default_password", lambda: Secret("Default-9!"))
    monkeypatch.setattr(ra, "record_applied", lambda *a, **k: None)
    monkeypatch.setattr(db_ops, "lock_range", lambda db, rid: None)
    monkeypatch.setattr(db_ops, "range_output_and_backend", lambda db, rid: (stored["out"], "vsphere"))
    monkeypatch.setattr(db_ops, "set_range_output", fake_set_output)

    sr._reconcile_range("r1", "Range 1", output, "vsphere")
    vm = writes["out"]["vms"][0]
    assert vm["role_setup"]["status"] == "ok" and vm["roles"]["exchange"]["status"] == "ok"
    assert not [w for w in writes["out"]["warnings"] if "being set up" in w]


def test_reconcile_range_skips_a_backend_without_support(monkeypatch):
    class FakeProv:
        supports_setup_reconcile = False

    import worker.range_accounts as ra
    import worker.tasks as tasks
    called = {"reconcile": False}
    monkeypatch.setattr(tasks, "_get_backend", lambda *a, **k: FakeProv())
    monkeypatch.setattr(ra, "load", lambda session, rid: called.__setitem__("load", True))
    # Should return before loading accounts or reconciling.
    sr._reconcile_range("r1", "Range 1", {"vms": [_product_vm()]}, "mock")
    assert "load" not in called
