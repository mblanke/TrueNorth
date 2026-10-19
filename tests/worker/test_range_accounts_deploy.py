"""A range's declared accounts are what its VMs get at deploy (ADR 0012 §12).

On the fake vCenter of test_vsphere_provision.py:

* Windows: Sysprep sets the declared ``Administrator`` password, and the guest
  operations that install software log in with that same password; with none declared,
  TN_RANGE_DEFAULT_PASSWORD; with neither, a random one per VM (as before);
* Linux: each declared account is a cloud-init user (a SHA-512-crypt hash, never plain
  text; sudo for an admin other than the image's default user, which keeps its own;
  password logins on); with none declared, the default password goes to the image's user;
* H1: on EVERY Linux VM that got account material, with or without software, the worker
  logs in as the ephemeral install user once cloud-init has run, removes that user, and
  rewrites the guestinfo without hashes, default login or install user; if it cannot see
  cloud-init finish, it rewrites it anyway and records the accounts as not set;
* each built VM reports ``accounts_applied`` (usernames), which the worker records;
* a lab session's range gets no default login (random per VM, as before; H2);
* passwords never reach the result, the logs or an error.
"""

from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import test_vsphere_provision as _vsp
import yaml
from test_vsphere_provision import R8, RANGE_ID, SW_TEMPLATE, TEMPLATE, _prov, _rendered, _run, _userdata, _vm_out
from worker import range_accounts
from worker.provisioners import vsphere_guest as guest
from worker.provisioners.sha512_crypt import sha512_crypt

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
    return hashed.startswith("$6$") and sha512_crypt(password, hashed.split("$")[2]) == hashed


def _all_userdata(vm) -> list[dict]:
    out = []
    for spec in vm.reconfig_specs:
        extra = {o.key: o.value for o in spec.extraConfig or []}
        if "guestinfo.userdata" in extra:
            out.append(yaml.safe_load(base64.b64decode(extra["guestinfo.userdata"])))
    return out


class TestWindows:
    def test_sysprep_and_guest_operations_use_the_declared_administrator(self, depot):
        vc = depot
        tpl = _with(_rendered(SW_TEMPLATE), {"dc01": [_acct("Administrator", DC_PW, admin=True)]}, DEFAULT_PW)
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        dc = vc.vms[_vm_out(result, "dc01")["vm_id"]]
        assert dc.guest_password("Administrator") == DC_PW  # Sysprep set exactly the declared one
        assert _vm_out(result, "dc01")["accounts_applied"] == ["Administrator"]
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
        assert _vm_out(result, "dc01")["accounts_applied"] == ["Administrator"]  # M2: jsmith is not

    def test_appliances_set_nothing(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), {"fw": [_acct("admin", "Fw-Pass-1!")]}), {}))
        assert result.status == "ok", result.errors
        assert _vm_out(result, "fw")["accounts_applied"] == []
        assert any(f"{R8}-fw" in w and "admin" in w for w in result.warnings)


class TestLinux:
    def test_declared_accounts_are_cloud_init_users_beside_the_install_user(self, depot):
        vc = depot
        tpl = _with(_rendered(SW_TEMPLATE), {"web01": [_acct("ubuntu", WEB_PW, admin=True),
                                                       _acct("opsadmin", WEB_PW, admin=True),
                                                       _acct("analyst", ANALYST_PW)]}, DEFAULT_PW)
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        first = _first_userdata(web)
        users = {u["name"]: u for u in first["users"] if isinstance(u, dict)}
        assert first["users"][0] == "default" and set(users) == {"tn-install", "ubuntu", "opsadmin", "analyst"}
        # Low (e): the image's default user keeps the passwordless sudo its image gives it.
        assert "sudo" not in users["ubuntu"] and users["opsadmin"]["sudo"] == "ALL=(ALL:ALL) ALL"
        assert "sudo" not in users["analyst"]
        assert users["analyst"]["lock_passwd"] is False and first["ssh_pwauth"] is True
        assert "password" not in first  # declared accounts: the default user is left alone
        for name, pw in (("ubuntu", WEB_PW), ("opsadmin", WEB_PW), ("analyst", ANALYST_PW)):
            assert "plain_text_passwd" not in users[name] and _crypt_ok(users[name]["hashed_passwd"], pw)
        assert sorted(_vm_out(result, "web01")["accounts_applied"]) == ["analyst", "opsadmin", "ubuntu"]
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
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        doc = _first_userdata(web)
        assert _crypt_ok(doc["password"], DEFAULT_PW)  # a hash, never the password
        assert doc["chpasswd"] == {"expire": False} and doc["ssh_pwauth"] is True
        assert not {"password", "ssh_pwauth", "users"} & set(_userdata(web))  # scrubbed after first boot

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


class TestGuestinfoScrub:
    """H1: account hashes do not stay in a VM's guestinfo for its life."""

    def test_a_vm_without_software_is_scrubbed_after_cloud_init_ran(self, vc):
        """The reviewer's probe (scratchpad test_probe_guestinfo.py): no software, no uplink."""
        tpl = _with(_rendered(), {"web01": [_acct("ubuntu", "Summer2026!", True)]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        assert "users" not in _userdata(web), "range account credential material persists in VM extraConfig"
        steps = [s for s in vc.guest_ops.started if s[0] == f"{R8}-web01"]
        assert [s[1] for s in steps] == ["tn-install", "tn-install"]  # wait for cloud-init, then clean up
        assert "cloud-init status --wait" in steps[0][3] and "userdel" in steps[1][3]
        assert _vm_out(result, "web01")["accounts_applied"] == ["ubuntu"]

    def test_nothing_in_any_guestinfo_is_ever_plain_text(self, depot):
        vc = depot
        tpl = _with(_rendered(SW_TEMPLATE), {"web01": [_acct("analyst", ANALYST_PW)],
                                             "sensor": [_acct("analyst", ANALYST_PW)]}, DEFAULT_PW)
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        for vm in vc.vms.values():
            for doc in _all_userdata(vm):
                text = yaml.safe_dump(doc)
                assert "plain_text_passwd" not in text
                assert ANALYST_PW not in text and DEFAULT_PW not in text
            if _all_userdata(vm):
                assert not {"users", "password", "ssh_pwauth"} & set(_all_userdata(vm)[-1]), vm.name

    def test_scrubbed_anyway_when_cloud_init_cannot_be_seen(self, vc, monkeypatch):
        def refuse(vm, auth):
            raise _vsp.vim.fault.InvalidGuestLogin()

        vc.guest_ops.ValidateCredentialsInGuest = refuse
        prov = _prov(vc)
        prov._scrub_timeout = 0  # the 60 s floor; the guest clock below runs 50 s per poll
        clock = [0.0]
        real = time.monotonic
        monkeypatch.setattr(_vsp.guest_mod, "time", SimpleNamespace(
            monotonic=lambda: real() + clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + 50)))
        result = _run(prov.provision(RANGE_ID, _with(_rendered(), {"web01": [_acct("analyst", ANALYST_PW)]}), {}))
        assert result.status == "ok", result.errors
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        assert "users" not in _userdata(web)
        assert _vm_out(result, "web01")["accounts_applied"] == []  # M2: not seen applied, not claimed
        assert any(f"{R8}-web01" in w and "recorded as not set" in w for w in result.warnings)

    def test_vms_without_account_material_are_not_touched(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        assert vc.guest_ops.started == []


class TestSha512Crypt:
    def test_the_specification_vectors_and_openssl(self):
        assert sha512_crypt("Hello world!", "saltstring") == (
            "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1")
        assert sha512_crypt("Hello world!", "saltstringsaltstring", 10000) == (
            "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8vOuZeHBy/"
            "YTBmSK6H9qs/y3RnOaw5v.")
        # openssl passwd -6 -salt 'Ab3/x.Yz' 'Pw-With-Ümlaut 1!'
        assert sha512_crypt("Pw-With-Ümlaut 1!", "Ab3/x.Yz") == (
            "$6$Ab3/x.Yz$Jx4EXvG9IHPq8vbbTrqswZGn4r6D32s4mr1Pnea9ZOE/UU5mwAkQEDZronTUO4msOdCsYX66T29WIkUcNUWMY.")

    def test_matches_glibc_where_the_platform_has_it(self):
        try:
            import warnings  # noqa: PLC0415

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                import crypt  # noqa: PLC0415
            ref = crypt.crypt("Range-Pass-1!", "$6$q1w2e3r4t5y6u7i8")
        except Exception:  # noqa: BLE001
            ref = None
        if not ref or not ref.startswith("$6$"):
            pytest.skip("no SHA-512 crypt(3) here (macOS); the vectors above still apply")
        assert sha512_crypt("Range-Pass-1!", "q1w2e3r4t5y6u7i8") == ref


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
        monkeypatch.setattr(range_accounts.db_ops, "is_lab_range", lambda db, rid: False)
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
        monkeypatch.setattr(range_accounts.db_ops, "is_lab_range", lambda db, rid: False)
        monkeypatch.setenv(range_accounts.DEFAULT_PASSWORD_ENV, f"  {DEFAULT_PW}  ")
        out = range_accounts.attach(_rendered(), session, RANGE_ID)
        assert {vm["_default_login"].reveal() for vm in out["vms"]} == {DEFAULT_PW}
        assert DEFAULT_PW not in repr(out)

    def test_a_lab_sessions_range_keeps_random_passwords(self, monkeypatch):
        """H2: no shared default on lab VMs (and no accounts: labs hold none)."""
        @contextmanager
        def session():
            yield None

        monkeypatch.setattr(range_accounts.db_ops, "is_lab_range", lambda db, rid: True)
        monkeypatch.setattr(range_accounts.db_ops, "range_accounts",
                            lambda db, rid: pytest.fail("a lab range's accounts are not read"))
        monkeypatch.setenv(range_accounts.DEFAULT_PASSWORD_ENV, DEFAULT_PW)
        template = _rendered()
        assert range_accounts.attach(template, session, RANGE_ID) is template
        assert all("_default_login" not in vm for vm in template["vms"])

    def test_what_the_build_set_is_recorded_per_machine(self, monkeypatch):
        @contextmanager
        def session():
            yield None

        recorded = []
        monkeypatch.setattr(range_accounts.db_ops, "mark_accounts_applied",
                            lambda db, rid, pairs: recorded.append(sorted(pairs)))
        range_accounts.record_applied(session, RANGE_ID, [
            {"name": f"{R8}-dc01", "node_id": "dc01", "accounts_applied": ["Administrator"]},
            {"name": f"{R8}-ws-0", "node_id": "ws", "accounts_applied": ["a", "b"]},
            {"name": f"{R8}-ws-1", "node_id": "ws", "accounts_applied": ["a"]},  # b not set on every copy
            {"name": f"{R8}-mock", "node_id": "mock"},  # a provisioner that reports nothing
        ])
        assert recorded == [[("dc01", "Administrator"), ("ws", "a")]]
