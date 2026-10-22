"""A range's accounts are what its VMs get at deploy (ADR 0012 §12, ADR 0013 §3).

On the fake vCenter of test_vsphere_provision.py:

* Windows: Sysprep sets the range's ``Administrator`` password (declared, or the implicit
  staff login the API adds), and the guest operations that install software log in with
  that same password; with none, a random one per VM. Never a site-wide default or the
  template password, whatever the environment holds;
* Linux: each account is a cloud-init user (a SHA-512-crypt hash, never plain text; sudo
  for an admin other than the image's own users, the build user included, which keep
  theirs; password logins on); the build user's row is how its template password is
  replaced. With no account, nothing (the template-login check then reports it);
* the template-login check (vsphere_credcheck): one login attempt with the template
  password per VM after its own login worked: refused passes, accepted fails the VM
  (``enforce``) or warns (``warn``), anything else is inconclusive;
* H1: on EVERY Linux VM with declared accounts, with or without software, the worker logs
  in as the ephemeral install user once cloud-init has run, checks each account exists
  (``getent passwd``), removes that user, and rewrites the guestinfo without hashes or
  install user; if it cannot log in, it rewrites it anyway, warns that tn-install may
  remain and records the accounts as not set;
* each built VM reports ``accounts_applied``: only accounts verified to exist (Linux:
  getent; Windows: a guest-operations login with the declared Administrator password);
* a lab session's range gets its accounts like any other range (ADR 0013 §3);
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


@pytest.fixture(autouse=True)
def site_defaults(monkeypatch):
    """A site that still has the old default and template passwords in its environment:
    no VM may be given them (ADR 0013 §3)."""
    monkeypatch.setenv("TN_RANGE_DEFAULT_PASSWORD", DEFAULT_PW)
    monkeypatch.setenv("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", DEFAULT_PW)
    monkeypatch.delenv("TN_TEMPLATE_LINUX_USER", raising=False)
    monkeypatch.delenv("TN_TEMPLATE_LINUX_PASSWORD", raising=False)
    monkeypatch.delenv("RANGE_TEMPLATE_LOGIN_CHECK", raising=False)


def _with(template: dict, accounts: dict[str, list[guest.RangeAccount]]) -> dict:
    out = dict(template)
    out["vms"] = []
    for vm in template["vms"]:
        vm = dict(vm)
        if vm["node_id"] in accounts:
            vm["_accounts"] = accounts[vm["node_id"]]
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
        tpl = _with(_rendered(SW_TEMPLATE), {"dc01": [_acct("Administrator", DC_PW, admin=True)]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        dc = vc.vms[_vm_out(result, "dc01")["vm_id"]]
        assert dc.guest_password("Administrator") == DC_PW  # Sysprep set exactly the declared one
        assert _vm_out(result, "dc01")["accounts_applied"] == ["Administrator"]
        logins = {s[1] for s in vc.guest_ops.started if s[0] == f"{R8}-dc01"}
        assert logins == {"Administrator"}
        assert DC_PW in vc.guest_ops.passwords_seen  # the installs authenticated with it

    def test_no_site_default_or_template_password_whatever_the_environment(self, vc):
        """ADR 0013 §3: the #185 fallback is gone. With no Administrator row (the API always
        adds one; a backend or an old range may not) Sysprep sets a random one."""
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), {}), {}))
        assert result.status == "ok", result.errors
        assert vc.vms[_vm_out(result, "dc01")["vm_id"]].guest_password("Administrator") not in (DEFAULT_PW, None)

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
                                                       _acct("analyst", ANALYST_PW)]})
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

    def test_no_default_login_in_cloud_config_without_accounts(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), {}), {}))
        assert result.status == "ok", result.errors
        for vm in vc.vms.values():
            for doc in _all_userdata(vm):
                assert not {"users", "password", "chpasswd", "ssh_pwauth"} & set(doc), vm.name
        assert vc.guest_ops.started == []  # no install user, no scrub: nothing to take out

    def test_the_build_users_row_replaces_its_template_password_and_keeps_its_sudo(self, vc, monkeypatch):
        """ADR 0013 §3: the implicit staff login for the build user (TN_TEMPLATE_LINUX_USER)
        is a cloud-init user like any other: its password is the range's, and no
        password-required sudo rule overrides the one its image gives it."""
        monkeypatch.setenv("TN_TEMPLATE_LINUX_USER", "tnbuild")
        tpl = _with(_rendered(), {"web01": [_acct("tnbuild", WEB_PW, admin=True)]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        users = {u["name"]: u for u in _first_userdata(vc.vms[_vm_out(result, "web01")["vm_id"]])["users"]
                 if isinstance(u, dict)}
        assert _crypt_ok(users["tnbuild"]["hashed_passwd"], WEB_PW) and "sudo" not in users["tnbuild"]
        assert _vm_out(result, "web01")["accounts_applied"] == ["tnbuild"]

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
        assert [s[1] for s in steps] == ["tn-install"] * 3  # wait for cloud-init, check, clean up
        assert "cloud-init status --wait" in steps[0][3]
        assert (steps[1][2], steps[1][3]) == ("/usr/bin/getent", "passwd ubuntu")
        assert "userdel" in steps[2][3]
        assert _vm_out(result, "web01")["accounts_applied"] == ["ubuntu"]

    def test_only_accounts_that_exist_are_reported_applied(self, vc):
        """Item 6: getent per account; one cloud-init did not create is not applied."""
        vc.guest_ops.exit_codes = {"passwd analyst": 2}
        tpl = _with(_rendered(), {"web01": [_acct("opsadmin", WEB_PW, True), _acct("analyst", ANALYST_PW)]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        assert _vm_out(result, "web01")["accounts_applied"] == ["opsadmin"]
        assert any(f"{R8}-web01" in w and "not verified" in w and "analyst" in w for w in result.warnings)
        assert "users" not in _userdata(vc.vms[_vm_out(result, "web01")["vm_id"]])

    def test_nothing_in_any_guestinfo_is_ever_plain_text(self, depot):
        vc = depot
        tpl = _with(_rendered(SW_TEMPLATE), {"web01": [_acct("analyst", ANALYST_PW)],
                                             "sensor": [_acct("analyst", ANALYST_PW)]})
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
        assert any(f"{R8}-web01" in w and "tn-install user may remain" in w for w in result.warnings)

    def test_a_windows_administrator_is_applied_only_when_its_password_logs_in(self, vc):
        """Item 6: Windows is verified by a guest-operations login with the declared password."""
        tpl = _with(_rendered(), {"dc01": [_acct("Administrator", DC_PW, True)]})
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        assert _vm_out(result, "dc01")["accounts_applied"] == ["Administrator"]
        assert DC_PW in vc.guest_ops.passwords_seen

    def test_a_windows_administrator_that_does_not_log_in_is_not_applied(self, vc, monkeypatch):
        def refuse(vm, auth):
            raise _vsp.vim.fault.InvalidGuestLogin()

        vc.guest_ops.ValidateCredentialsInGuest = refuse
        prov = _prov(vc)
        prov._install_timeout = 0
        clock = [0.0]
        real = time.monotonic
        monkeypatch.setattr(_vsp.guest_mod, "time", SimpleNamespace(
            monotonic=lambda: real() + clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + 50)))
        result = _run(prov.provision(RANGE_ID, _with(_rendered(), {"dc01": [_acct("Administrator", DC_PW, True)]}),
                                     {}))
        assert result.status == "ok", result.errors
        assert _vm_out(result, "dc01")["accounts_applied"] == []
        assert any(f"{R8}-dc01" in w and "not verified" in w for w in result.warnings)

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
                                             "web01": [_acct("analyst", ANALYST_PW)]})
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
    @staticmethod
    @contextmanager
    def _session():
        yield None

    def test_attach_copies_and_matches_on_node_id(self, monkeypatch):
        rows = [("dc01", "Administrator", DC_PW, "admin"), ("web01", "analyst", ANALYST_PW, "user"),
                ("ghost", "x", "y", "user")]
        monkeypatch.setattr(range_accounts.db_ops, "range_accounts", lambda db, rid: rows)
        template = _rendered()
        out = range_accounts.attach(template, self._session, RANGE_ID)
        assert all("_accounts" not in vm for vm in template["vms"])  # the caller's copy is untouched
        by_node = {vm["node_id"]: vm for vm in out["vms"]}
        assert [(a.username, a.admin) for a in by_node["dc01"]["_accounts"]] == [("Administrator", True)]
        assert [a.password.reveal() for a in by_node["web01"]["_accounts"]] == [ANALYST_PW]
        assert "_accounts" not in by_node["fw"]

    def test_no_default_login_is_attached_whatever_the_environment(self, monkeypatch):
        monkeypatch.setattr(range_accounts.db_ops, "range_accounts", lambda db, rid: [])
        out = range_accounts.attach(_rendered(), self._session, RANGE_ID)
        assert all(not [k for k in vm if k.startswith("_") and "login" in k] for vm in out["vms"])
        assert not hasattr(range_accounts, "default_password")
        assert DEFAULT_PW not in repr(out)

    def test_a_lab_sessions_range_gets_its_accounts_like_any_other(self, monkeypatch):
        """ADR 0013 §3 (owner decision 2026-10-21): real per-lab generated credentials."""
        monkeypatch.setattr(range_accounts.db_ops, "range_accounts",
                            lambda db, rid: [("dc01", "Administrator", DC_PW, "admin")])
        monkeypatch.setattr(range_accounts.db_ops, "is_lab_range", lambda db, rid: True)
        out = range_accounts.attach(_rendered(), self._session, RANGE_ID)
        dc = next(vm for vm in out["vms"] if vm["node_id"] == "dc01")
        assert [a.password.reveal() for a in dc["_accounts"]] == [DC_PW]

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


# ── The template-login check (ADR 0013 §3, provisioners/vsphere_credcheck.py) ────────────
TEMPLATE_WIN = "Template-Win-1!"
TEMPLATE_LINUX = "Template-Linux-2!"


class TestTemplateLoginCheck:
    @pytest.fixture(autouse=True)
    def template_passwords(self, monkeypatch):
        monkeypatch.setenv("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", TEMPLATE_WIN)
        monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", TEMPLATE_LINUX)

    def _accounts(self):
        return {"dc01": [_acct("Administrator", DC_PW, admin=True)], "web01": [_acct("ubuntu", WEB_PW, admin=True)]}

    def _template_still_works(self, vc, *names):
        """The fake guests of ``names`` still accept their template password."""
        real = vc.guest_ops._check

        def check(vm, auth):
            if any(vm.name.endswith(f"-{n}") for n in names) and auth.password in (TEMPLATE_WIN, TEMPLATE_LINUX):
                vc.guest_ops.passwords_seen.add(auth.password)
                return
            real(vm, auth)

        vc.guest_ops._check = check

    def test_a_refused_template_password_passes_once_the_own_login_worked(self, vc, caplog):
        caplog.set_level("DEBUG")
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), self._accounts()), {}))
        assert result.status == "ok", result.errors
        for node, user in (("dc01", "Administrator"), ("web01", "ubuntu")):
            assert _vm_out(result, node)["template_login"] == {
                "status": "rejected", "username": user, "detail": "the golden template's password is refused"}
        assert _vm_out(result, "fw")["template_login"]["status"] == "not_checked"  # appliance
        assert TEMPLATE_WIN in vc.guest_ops.passwords_seen and TEMPLATE_LINUX in vc.guest_ops.passwords_seen
        dumped = json.dumps(result.__dict__, default=str)
        for pw in (TEMPLATE_WIN, TEMPLATE_LINUX, DC_PW, WEB_PW):
            assert pw not in dumped and pw not in caplog.text

    def test_exactly_one_attempt_per_vm(self, vc):
        attempts = []
        real = vc.guest_ops.ValidateCredentialsInGuest

        def validate(vm, auth):
            if auth.password in (TEMPLATE_WIN, TEMPLATE_LINUX):
                attempts.append(vm.name)
            return real(vm, auth)

        vc.guest_ops.ValidateCredentialsInGuest = validate
        _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), self._accounts()), {}))
        assert len(attempts) == len(set(attempts)) and {f"{R8}-dc01", f"{R8}-web01"} <= set(attempts)
        assert f"{R8}-fw" not in attempts  # appliances are not tried

    def test_an_accepted_template_password_fails_the_vm_under_enforce(self, vc):
        self._template_still_works(vc, "web01")
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), self._accounts()), {}))
        assert _vm_out(result, "web01")["template_login"]["status"] == "accepted"
        assert f"{R8}-web01" in result.not_ready
        assert any("Not safe" in e and f"{R8}-web01" in e for e in result.errors)
        assert _vm_out(result, "dc01")["template_login"]["status"] == "rejected"

    def test_warn_mode_records_it_without_failing_the_vm(self, vc, monkeypatch):
        monkeypatch.setenv("RANGE_TEMPLATE_LOGIN_CHECK", "warn")
        self._template_still_works(vc, "dc01")
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), self._accounts()), {}))
        assert result.status == "ok", result.errors
        assert _vm_out(result, "dc01")["template_login"]["status"] == "accepted"
        assert any("Not safe" in w and f"{R8}-dc01" in w for w in result.warnings)

    def test_inconclusive_without_the_template_password_or_a_proven_login(self, vc, monkeypatch):
        monkeypatch.delenv("TN_TEMPLATE_LINUX_PASSWORD")
        result = _run(_prov(vc).provision(RANGE_ID, _with(_rendered(), {"dc01": []}), {}))
        assert result.status == "ok", result.errors
        assert _vm_out(result, "web01")["template_login"]["status"] == "inconclusive"  # password unknown
        # No account set on dc01, so no own login proven: a refusal is not a pass.
        assert _vm_out(result, "dc01")["template_login"]["status"] == "inconclusive"


class TestCredcheckUnit:
    class Session:
        def __init__(self, answer):
            self.answer, self.tried = answer, []

        def try_login(self):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    @pytest.mark.parametrize(("answer", "proven", "status"), [
        (True, True, "accepted"), (True, False, "accepted"), (False, True, "rejected"),
        (False, False, "inconclusive"), (None, True, "inconclusive"), (RuntimeError("x"), True, "inconclusive"),
    ])
    def test_classification(self, monkeypatch, answer, proven, status):
        from worker.provisioners import vsphere_credcheck as cc

        monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", TEMPLATE_LINUX)
        seen = []
        session = self.Session(answer)
        result = cc.check(lambda creds: seen.append(creds) or session, "linux", proven=proven)
        assert result["status"] == status and len(seen) == 1 and seen[0].username == "ubuntu"
        assert TEMPLATE_LINUX not in json.dumps(result)

    def test_securityonion_uses_its_build_login(self, monkeypatch):
        from worker.provisioners import vsphere_credcheck as cc

        monkeypatch.setenv("VSPHERE_SO_INSTALL_USER", "soadmin")
        monkeypatch.setenv("VSPHERE_SO_INSTALL_PASSWORD", "So-Template-3!")
        seen = []
        cc.check(lambda creds: seen.append(creds) or self.Session(False), "linux", proven=True, securityonion=True)
        assert (seen[0].username, seen[0].password.reveal()) == ("soadmin", "So-Template-3!")

    def test_the_mode_defaults_to_enforce(self, monkeypatch):
        from worker.provisioners import vsphere_credcheck as cc

        monkeypatch.delenv("RANGE_TEMPLATE_LOGIN_CHECK", raising=False)
        assert cc.mode() == "enforce"
        monkeypatch.setenv("RANGE_TEMPLATE_LOGIN_CHECK", "WARN")
        assert cc.mode() == "warn"
