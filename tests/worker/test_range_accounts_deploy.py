"""A range's declared accounts are what its VMs get at deploy (ADR 0012 §12).

On the fake vCenter of test_vsphere_provision.py:

* Windows: Sysprep sets the declared ``Administrator`` password, and the guest
  operations that install software log in with that same password; with none declared,
  TN_RANGE_DEFAULT_PASSWORD; with neither, a random one per VM (as before);
* Linux: each declared account is a cloud-init user (hashed password, sudo for an admin,
  password logins on); the ephemeral install user stays separate and is the only one the
  clean-up deletes; with none declared, the default password goes to the image's user;
* passwords never reach the result, the logs or an error, and the in-memory keys are
  gone from the VM plans when the build returns.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager

import pytest
import test_vsphere_provision as _vsp
import yaml
from test_vsphere_provision import R8, RANGE_ID, SW_TEMPLATE, TEMPLATE, _prov, _rendered, _run, _userdata, _vm_out
from worker import range_accounts
from worker.provisioners import vsphere_guest as guest

vc, uplink, depot, worker_reserves = _vsp.vc, _vsp.uplink, _vsp.depot, _vsp.worker_reserves

DC_PW = "Dc-Declared-1!"
WEB_PW = "Web-Declared-2!"
ANALYST_PW = "Analyst-Declared-3!"
DEFAULT_PW = "Range-Default-4!"


def _with(template: dict, accounts: dict[str, list[guest.RangeAccount]], default: str | None = None) -> dict:
    out = dict(template)
    out["vms"] = []
    for vm in template["vms"]:
        vm = dict(vm)
        if vm["node_id"] in accounts:
            vm["_accounts"] = accounts[vm["node_id"]]
        if default:
            vm["_default_login"] = guest.Secret(default)
        out["vms"].append(vm)
    return out


def _acct(name: str, pw: str, admin: bool = False) -> guest.RangeAccount:
    return guest.RangeAccount(name, guest.Secret(pw), admin)


def _first_userdata(vm) -> dict:
    extra = {o.key: o.value for o in vm.reconfig_specs[0].extraConfig}
    return yaml.safe_load(base64.b64decode(extra["guestinfo.userdata"]))


def _crypt_ok(hashed: str, password: str) -> bool:
    try:
        import crypt  # noqa: PLC0415 — deprecated; the worker image (3.11) has it
    except ImportError:
        return False
    return crypt.crypt(password, hashed) == hashed


class TestWindows:
    def test_sysprep_and_guest_operations_use_the_declared_administrator(self, depot):
        vc = depot
        tpl = _with(_rendered(SW_TEMPLATE), {"dc01": [_acct("Administrator", DC_PW, admin=True)]}, DEFAULT_PW)
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        dc = vc.vms[_vm_out(result, "dc01")["vm_id"]]
        assert dc.guest_password("Administrator") == DC_PW  # Sysprep set exactly the declared one
        logins = {s[1] for s in vc.guest_ops.started if s[0] == f"{R8}-dc01"}
        assert logins == {"Administrator"}
        assert DC_PW in vc.guest_ops.passwords_seen  # the installs authenticated with it

    def test_the_platform_default_when_nothing_is_declared(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), {}, DEFAULT_PW), {}))
        assert result.status == "ok", result.errors
        assert vc.vms[_vm_out(result, "dc01")["vm_id"]].guest_password("Administrator") == DEFAULT_PW

    def test_random_per_vm_without_either(self, vc):
        two = {**TEMPLATE, "nodes": [*TEMPLATE["nodes"], {**TEMPLATE["nodes"][2], "id": "dc02", "ip": "10.60.201.11"}]}
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(two), {}))
        assert result.status == "ok", result.errors
        pws = {vc.vms[_vm_out(result, n)["vm_id"]].guest_password("Administrator") for n in ("dc01", "dc02")}
        assert len(pws) == 2 and not pws & {DC_PW, DEFAULT_PW, None}

    def test_other_windows_accounts_are_reported_not_silently_dropped(self, vc):
        tpl = _with(_rendered(), {"dc01": [_acct("Administrator", DC_PW, True), _acct("jsmith", "Js-1!xyz")]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        assert any(f"{R8}-dc01" in w and "jsmith" in w and "Administrator" not in w.split(":")[-1]
                   for w in result.warnings)


class TestLinux:
    def test_declared_accounts_are_cloud_init_users_beside_the_install_user(self, depot):
        vc = depot
        tpl = _with(_rendered(SW_TEMPLATE), {"web01": [_acct("ubuntu", WEB_PW, admin=True),
                                                       _acct("analyst", ANALYST_PW)]}, DEFAULT_PW)
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        first = _first_userdata(web)
        users = {u["name"]: u for u in first["users"] if isinstance(u, dict)}
        assert first["users"][0] == "default" and set(users) == {"tn-install", "ubuntu", "analyst"}
        assert users["ubuntu"]["sudo"] == "ALL=(ALL:ALL) ALL" and "sudo" not in users["analyst"]
        assert users["analyst"]["lock_passwd"] is False and first["ssh_pwauth"] is True
        assert "password" not in first  # declared accounts: the default user is left alone
        for name, pw in (("ubuntu", WEB_PW), ("analyst", ANALYST_PW)):
            stored = users[name].get("hashed_passwd") or users[name]["plain_text_passwd"]
            assert stored == pw or _crypt_ok(stored, pw)
        # The installs logged in as the ephemeral user, and only it is deleted afterwards.
        lin = [s for s in vc.guest_ops.started if s[0] == f"{R8}-web01"]
        assert {s[1] for s in lin} == {"tn-install"}
        cleanup = lin[-1][3]
        assert "userdel" in cleanup and "tn-install" in cleanup and "analyst" not in cleanup and "ubuntu" not in cleanup
        # Applied by then: the guestinfo is rewritten without any account.
        assert "users" not in _userdata(web)

    def test_default_password_for_the_image_user_when_nothing_is_declared(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), {}, DEFAULT_PW), {}))
        assert result.status == "ok", result.errors
        doc = _first_userdata(vc.vms[_vm_out(result, "web01")["vm_id"]])
        assert doc["password"] == DEFAULT_PW or _crypt_ok(doc["password"], DEFAULT_PW)
        assert doc["chpasswd"] == {"expire": False} and doc["ssh_pwauth"] is True

    def test_nothing_changes_without_accounts_or_default(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        doc = _first_userdata(vc.vms[_vm_out(result, "web01")["vm_id"]])
        assert not {"users", "password", "ssh_pwauth"} & set(doc)

    def test_a_name_linux_refuses_is_reported(self, vc):
        tpl = _with(_rendered(), {"web01": [_acct("CORP\\analyst", ANALYST_PW)]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        assert any("CORP\\analyst" in w and f"{R8}-web01" in w for w in result.warnings)
        assert "users" not in _first_userdata(vc.vms[_vm_out(result, "web01")["vm_id"]])


class TestSecrecy:
    def test_passwords_stay_out_of_results_logs_and_errors(self, depot, caplog):
        caplog.set_level("DEBUG")
        vc = depot
        real_start = vc.guest_ops.StartProgramInGuest

        def leaky(vm, auth, spec):  # a fault that quotes the password back
            if "7zip" in spec.arguments:
                raise RuntimeError(f"login {auth.username}/{auth.password} refused")
            return real_start(vm, auth, spec)

        vc.guest_ops.StartProgramInGuest = leaky
        tpl = _with(_rendered(SW_TEMPLATE), {"dc01": [_acct("Administrator", DC_PW, True)],
                                             "web01": [_acct("analyst", ANALYST_PW)]}, DEFAULT_PW)
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        dumped = json.dumps(result.__dict__, default=str)
        for pw in (DC_PW, ANALYST_PW, DEFAULT_PW):
            assert pw not in dumped
            assert pw not in caplog.text
        assert any("***" in e for e in result.errors)

    def test_declared_passwords_refuse_to_be_serialised(self):
        """A Secret never pickles (a Celery result or broker message) nor prints."""
        import pickle  # noqa: PLC0415

        acct = _acct("Administrator", DC_PW, True)
        with pytest.raises(TypeError):
            pickle.dumps(acct)
        assert DC_PW not in repr(acct) and DC_PW not in str(acct.password)


class TestAttach:
    def test_attach_copies_and_matches_on_node_id(self, monkeypatch):
        rows = [("dc01", "Administrator", DC_PW, "admin"), ("web01", "analyst", ANALYST_PW, "user"),
                ("ghost", "x", "y", "user")]

        @contextmanager
        def session():
            yield None

        monkeypatch.setattr(range_accounts.db_ops, "range_accounts", lambda db, rid: rows)
        monkeypatch.delenv(range_accounts.DEFAULT_PASSWORD_ENV, raising=False)
        template = _rendered()
        out = range_accounts.attach(template, session, RANGE_ID)
        assert all("_accounts" not in vm and "_default_login" not in vm for vm in template["vms"])  # untouched
        by_node = {vm["node_id"]: vm for vm in out["vms"]}
        assert [(a.username, a.admin) for a in by_node["dc01"]["_accounts"]] == [("Administrator", True)]
        assert [a.password.reveal() for a in by_node["web01"]["_accounts"]] == [ANALYST_PW]
        assert "_accounts" not in by_node["fw"] and "_default_login" not in by_node["fw"]

    def test_the_default_comes_from_the_environment(self, monkeypatch):
        @contextmanager
        def session():
            yield None

        monkeypatch.setattr(range_accounts.db_ops, "range_accounts", lambda db, rid: [])
        monkeypatch.setenv(range_accounts.DEFAULT_PASSWORD_ENV, f"  {DEFAULT_PW}  ")
        out = range_accounts.attach(_rendered(), session, RANGE_ID)
        assert {vm["_default_login"].reveal() for vm in out["vms"]} == {DEFAULT_PW}
        assert DEFAULT_PW not in repr(out)
