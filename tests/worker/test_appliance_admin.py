"""Range appliances never keep the template password (issue #248, ADR 0013 §3).

pfSense, OPNsense and VyOS get their range's generated admin password at deploy as a
**hash** (bcrypt for pfSense's ``bcrypt-hash`` and OPNsense's ``password``, SHA-512-crypt
for VyOS's ``encrypted-password``) through the boot-time config paths the worker already
has, never the password. The template-login check is an SSH attempt when the worker can
reach the appliance, else the appliance's own acknowledgement; neither is ``inconclusive``.

Here: the hash formats, the guestinfo / user data the worker writes, the check's classes,
and the whole path on the vSphere fakes of test_vsphere_provision.py (the boot hook's side
is tests/contracts/test_appliance_admin_hooks.py). Nothing here touches a real appliance.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace

import bcrypt
import pytest
import test_vsphere_provision as _vsp
from test_appliance_configs import _soc_router, _with_templates
from test_vsphere_provision import R8, RANGE_ID, TEMPLATE, _prov, _run, _vm_out
from worker import appliance_admin as aa
from worker import pfsense_config as pf
from worker import range_accounts, range_alloc, render
from worker import vyos_config as vy
from worker.provisioners import vsphere_guest as guest
from worker.provisioners.sha512_crypt import sha512_crypt

vc, uplink, depot, worker_reserves = _vsp.vc, _vsp.uplink, _vsp.depot, _vsp.worker_reserves

FW_PW = "Fw-Range-Pass-1!x"
TEMPLATE_PW = "Template-Build-9!"


def _acct(name: str, pw: str = FW_PW) -> guest.RangeAccount:
    return guest.RangeAccount(name, guest.Secret(pw), True)


def _bcrypt_ok(hashed: str, password: str) -> bool:
    # $2y$ and $2b$ are the same algorithm; Python's bcrypt reads $2b$.
    return bcrypt.checkpw(password.encode(), ("$2b$" + hashed[4:]).encode())


def _crypt_ok(hashed: str, password: str) -> bool:
    return hashed.startswith("$6$") and sha512_crypt(password, hashed.split("$")[2]) == hashed


# --------------------------------------------------------------------------- #
# Hashes and what the worker writes
# --------------------------------------------------------------------------- #
class TestHashes:
    @pytest.mark.parametrize("product", ["pfsense", "opnsense"])
    def test_firewalls_get_bcrypt_in_the_form_php_writes(self, product):
        hashed = aa.hash_password(product, FW_PW)
        assert hashed.startswith("$2y$10$") and pf.BCRYPT_RE.match(hashed)
        assert _bcrypt_ok(hashed, FW_PW) and not _bcrypt_ok(hashed, TEMPLATE_PW)
        assert aa.hash_password(product, FW_PW) != hashed  # salted

    def test_vyos_gets_sha512_crypt(self):
        hashed = aa.hash_password("vyos", FW_PW)
        assert vy.SHA512_CRYPT_RE.match(hashed) and _crypt_ok(hashed, FW_PW)

    def test_no_bcrypt_library_is_an_error_not_a_plain_password(self, monkeypatch):
        monkeypatch.setattr(aa, "bcrypt", None)
        with pytest.raises(aa.ApplianceAdminError):
            aa.hash_password("pfsense", FW_PW)

    def test_fingerprint_is_sha256_of_the_hash_not_the_password(self):
        import hashlib

        hashed = aa.hash_password("vyos", FW_PW)
        assert aa.fingerprint(hashed) == hashlib.sha256(hashed.encode()).hexdigest()[:16]
        assert FW_PW not in aa.fingerprint(hashed)


class TestPlan:
    @pytest.mark.parametrize("os_name,user", [("pfsense", "admin"), ("opnsense", "root"), ("vyos", "vyos")])
    def test_each_product_takes_its_admin_account(self, os_name, user):
        admin = aa.plan({"os": os_name, "_accounts": [_acct("operator", "Other-1!xx"), _acct(user)]})
        assert (admin.product, admin.username, admin.account) == (os_name, user, user)
        assert admin.password.reveal() == FW_PW and FW_PW not in admin.hash.reveal()
        assert FW_PW not in repr(admin) and admin.hash.reveal() not in repr(admin)
        assert admin.summary() == {"username": user, "hash_format": aa.HASH_FORMATS[os_name],
                                   "delivery": aa.delivery(os_name)}

    def test_matching_is_case_insensitive_and_reports_the_stored_name(self):
        admin = aa.plan({"os": "pfsense", "_accounts": [_acct("Admin")]})
        assert (admin.username, admin.account) == ("admin", "Admin")

    def test_nothing_without_an_account_or_for_another_image(self):
        assert aa.plan({"os": "pfsense", "_accounts": [_acct("operator")]}) is None
        assert aa.plan({"os": "pfsense"}) is None
        assert aa.plan({"os": "ubuntu-2404", "role": "router", "_accounts": [_acct("admin")]}) is None


class TestGuestinfo:
    def _pf(self, product="pfsense"):
        vm, rng = _soc_router(product)
        cfg = pf.build_config(vm, networks=rng["networks"], hostname="fw01", product=product)
        return cfg, [f"00:50:56:aa:00:{i:02x}" for i in range(len(vm["nics"]))]

    @pytest.mark.parametrize("product", ["pfsense", "opnsense"])
    def test_the_hash_rides_its_own_key_and_the_config_holds_no_credential(self, product):
        cfg, macs = self._pf(product)
        hashed = aa.hash_password(product, FW_PW)
        values = pf.guestinfo(cfg, macs, hashed)
        assert values[f"guestinfo.tn.{product}.admin"] == hashed
        assert pf.guestinfo(cfg, macs) == {k: v for k, v in values.items() if not k.endswith(".admin")}
        assert FW_PW not in json.dumps(values) and FW_PW not in pf.decode(values[f"guestinfo.tn.{product}.config"])

    @pytest.mark.parametrize("value", [FW_PW, "$6$abc$" + "x" * 86, "$2y$10$short", ""])
    def test_anything_but_a_bcrypt_hash_is_refused(self, value):
        cfg, macs = self._pf()
        with pytest.raises(pf.PfsenseConfigError):
            pf.guestinfo(cfg, macs, value)

    def _vy(self):
        vm, rng = _soc_router("vyos")
        return vy.build_config(vm, networks=rng["networks"], hostname="fw01"), \
            [f"00:50:56:aa:00:{i:02x}" for i in range(len(vm["nics"]))]

    def test_vyos_gets_an_encrypted_password_command_after_the_pins(self):
        cfg, macs = self._vy()
        hashed = aa.hash_password("vyos", FW_PW)
        values = vy.guestinfo(cfg, macs, "r-fw", hashed)
        cmds = vy.commands_of(values)
        assert cmds[len(macs)] == f"set system login user vyos authentication encrypted-password '{hashed}'"
        assert FW_PW not in base64.b64decode(values["guestinfo.userdata"]).decode()
        # The instance id does not depend on the hash: taking it out later re-runs nothing.
        assert values["guestinfo.metadata"] == vy.guestinfo(cfg, macs, "r-fw")["guestinfo.metadata"]

    @pytest.mark.parametrize("value", [FW_PW, "$2y$10$" + "a" * 53, "$6$salt$short", "$6$s$" + "x" * 86 + "'; rm"])
    def test_vyos_refuses_anything_but_sha512_crypt(self, value):
        cfg, macs = self._vy()
        with pytest.raises(vy.VyosConfigError):
            vy.guestinfo(cfg, macs, "r-fw", value)

    def test_scrub_values_drop_the_hash(self):
        cfg, macs = self._vy()
        hashed = aa.hash_password("vyos", FW_PW)
        scrubbed = aa.scrub_values({"_vyos": cfg, "name": "r-fw"}, macs)
        assert set(scrubbed) == {"guestinfo.userdata"}
        assert hashed not in base64.b64decode(scrubbed["guestinfo.userdata"]).decode()
        assert scrubbed["guestinfo.userdata"] == vy.guestinfo(cfg, macs, "r-fw")["guestinfo.userdata"]
        pcfg, _ = self._pf("opnsense")
        assert aa.scrub_values({"_pfsense": pcfg}, macs) == {"guestinfo.tn.opnsense.admin": ""}


# --------------------------------------------------------------------------- #
# The check's classes (appliance_admin.check)
# --------------------------------------------------------------------------- #
def _admin(product="pfsense") -> aa.AdminLogin:
    return aa.plan({"os": product, "_accounts": [_acct(aa.ADMIN_USERS[product])]})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += max(s, 1.0)


def _check(admin, *, acks=("",), ssh=None, reach=False, mgmt="10.30.32.101", env=None, deadline=30.0):
    clock = Clock()
    seq = list(acks)
    reads = []

    def read_ack():
        reads.append(clock.now)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    tried = []

    def ssh_login(host, user, pw):
        tried.append((host, user, pw.reveal()))
        return ssh(pw.reveal()) if ssh else None

    env = {"TN_TEMPLATE_LINUX_PASSWORD": TEMPLATE_PW} if env is None else env
    res = aa.check(admin, mgmt_ip=mgmt, read_ack=read_ack, deadline=deadline, poll=5, env=env,
                   ssh_login=ssh_login, is_reachable=lambda host: reach, clock=clock, sleep=clock.sleep)
    return res, tried, reads


class TestCheck:
    def test_ssh_refusal_after_the_own_login_worked_is_rejected(self):
        admin = _admin()
        res, tried, reads = _check(admin, reach=True, ssh=lambda pw: pw == FW_PW)
        assert (res["status"], res["method"], res["username"]) == ("rejected", "ssh", "admin")
        assert [t[2] for t in tried] == [FW_PW, TEMPLATE_PW] and reads == []  # no ack needed

    def test_ssh_template_login_working_is_accepted(self):
        res, _, _ = _check(_admin(), reach=True, ssh=lambda pw: True)
        assert (res["status"], res["method"]) == ("accepted", "ssh")

    def test_ssh_without_proof_of_the_own_login_falls_back_to_the_ack(self):
        admin = _admin()
        res, tried, _ = _check(admin, reach=True, ssh=lambda pw: False, acks=(admin.fingerprint,))
        assert (res["status"], res["method"]) == ("rejected", "guestinfo_ack")
        assert len(tried) == 1  # the template password is never tried when the own one did not work
        assert "answered no login decisively" in res["detail"]

    def test_unreachable_management_address_uses_the_ack(self):
        admin = _admin("vyos")
        res, tried, _ = _check(admin, acks=("", "", admin.fingerprint))
        assert (res["status"], res["method"], res["username"]) == ("rejected", "guestinfo_ack", "vyos")
        assert tried == [] and "not reachable" in res["detail"] and "no login attempt" in res["detail"]

    def test_no_management_address_or_template_password_skips_ssh(self):
        admin = _admin()
        res, tried, _ = _check(admin, mgmt="", reach=True, ssh=lambda pw: True, acks=(admin.fingerprint,))
        assert tried == [] and res["status"] == "rejected" and "no management address" in res["detail"]
        res, tried, _ = _check(admin, env={}, reach=True, ssh=lambda pw: True, acks=(admin.fingerprint,))
        assert tried == [] and "does not know the template password" in res["detail"]

    def test_a_failed_hook_is_inconclusive_and_says_why(self):
        res, _, _ = _check(_admin(), acks=("failed no single admin user in /cf/conf/config.xml",))
        assert (res["status"], res["method"]) == ("inconclusive", "guestinfo_ack")
        assert "no single admin user" in res["detail"]

    def test_another_hash_until_the_deadline_is_inconclusive(self):
        res, _, reads = _check(_admin(), acks=("0123456789abcdef",), deadline=30)
        assert (res["status"], res["method"]) == ("inconclusive", "none")
        assert "a different admin password hash" in res["detail"] and len(reads) > 1

    def test_no_ack_at_all_is_inconclusive(self):
        res, _, reads = _check(_admin(), deadline=0)
        assert res["status"] == "inconclusive" and "acknowledged nothing" in res["detail"] and len(reads) == 1

    def test_reading_the_ack_failing_is_never_a_pass(self):
        def boom():
            raise RuntimeError(FW_PW)

        res = aa.check(_admin(), read_ack=boom, deadline=0, env={})
        assert res["status"] == "inconclusive" and FW_PW not in json.dumps(res)

    def test_no_admin_delivered_is_inconclusive(self):
        res = aa.check(None, read_ack=lambda: "", deadline=0)
        assert (res["status"], res["method"]) == ("inconclusive", "none")

    def test_results_never_hold_a_password_or_hash(self):
        admin = _admin()
        for kw in ({"reach": True, "ssh": lambda pw: pw == FW_PW}, {"acks": (admin.fingerprint,)},
                   {"acks": ("failed x",)}, {"deadline": 0}):
            res, _, _ = _check(admin, **kw)
            dumped = json.dumps(res)
            assert FW_PW not in dumped and TEMPLATE_PW not in dumped and admin.hash.reveal() not in dumped


# --------------------------------------------------------------------------- #
# On the vSphere fakes
# --------------------------------------------------------------------------- #
def _build(vc, os_name: str, accounts: dict, allocations: dict | None = None, ack=None):
    """Build TEMPLATE with its firewall on ``os_name``. ``ack(vm, extra)``: what the fake
    appliance's boot hook publishes when it powers on (None: nothing)."""
    tpl = {**TEMPLATE, "nodes": [{**TEMPLATE["nodes"][0], "os": os_name}, *TEMPLATE["nodes"][1:]]}
    images = {os_name: f"tmpl-{os_name}", "ubuntu-2404": "tmpl-ubuntu-2404", "windows-server-2022": "tmpl-win2022"}
    out = render.render_topology(tpl, RANGE_ID, images.get)
    vms = []
    for vm in out["vm_definitions"]:
        vm = dict(vm)
        if vm["node_id"] in accounts:
            vm["_accounts"] = accounts[vm["node_id"]]
        vms.append(vm)
    built = {"name": "lab", "vms": vms, "networks": out["network_definitions"], "network": tpl["network"]}
    if os_name != "pfsense":
        _with_templates(vc, f"tmpl-{os_name}")
    if ack is not None:
        real = vc.on_power_on

        def on_power_on(vm):
            real(vm)
            if vm.name == f"{R8}-fw":
                extra = {o.key: o.value for s in vm.reconfig_specs for o in (s.extraConfig or [])}
                if (value := ack(vm, extra)) is not None:
                    vm.config.extraConfig = [SimpleNamespace(key=f"guestinfo.tn.{os_name}.admin_ack", value=value)]

        vc.on_power_on = on_power_on
    result = _run(_prov(vc).provision(RANGE_ID, built, allocations or {}))
    fw = next(v for v in vc.vms.values() if v.name == f"{R8}-fw")
    return result, fw


def _all_values(vm) -> list[str]:
    """Every guestinfo value the worker ever wrote to ``vm``, decoded where it is encoded."""
    out = []
    for spec in vm.reconfig_specs:
        for o in spec.extraConfig or []:
            out.append(str(o.value))
            if o.key.endswith(".config"):
                out.append(pf.decode(o.value))
            elif o.key in ("guestinfo.userdata", "guestinfo.metadata"):
                out.append(base64.b64decode(o.value).decode())
    return out


def _pf_hook(vm, extra):
    """The pfSense boot hook, as far as the worker can see it: the fingerprint of what it got."""
    return aa.fingerprint(extra["guestinfo.tn.pfsense.admin"])


class TestProvision:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("TN_TEMPLATE_LINUX_PASSWORD", TEMPLATE_PW)
        monkeypatch.setenv("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", TEMPLATE_PW + "w")
        monkeypatch.delenv("RANGE_TEMPLATE_LOGIN_CHECK", raising=False)

    def test_pfsense_gets_a_bcrypt_hash_and_an_acknowledged_change_is_proof(self, vc, caplog):
        caplog.set_level("DEBUG")
        result, fw = _build(vc, "pfsense", {"fw": [_acct("admin")]}, ack=_pf_hook)
        assert result.status == "ok", result.errors
        first = {o.key: o.value for o in fw.reconfig_specs[0].extraConfig}
        assert _bcrypt_ok(first["guestinfo.tn.pfsense.admin"], FW_PW)
        out = _vm_out(result, "fw")
        assert out["template_login"]["status"] == "rejected"
        assert out["template_login"]["method"] == "guestinfo_ack" and out["template_login"]["username"] == "admin"
        assert out["accounts_applied"] == ["admin"]
        assert out["admin_login"] == {"username": "admin", "hash_format": "bcrypt",
                                      "delivery": "guestinfo.tn.pfsense.admin", "hash_cleared": True}
        # The worker took the hash out of the VM's configuration once it was acknowledged.
        last = {o.key: o.value for o in fw.reconfig_specs[-1].extraConfig}
        assert last == {"guestinfo.tn.pfsense.admin": ""}
        # Never the password: guestinfo, the result, the stored output, the log.
        stored = json.dumps(range_alloc.stored_output("vsphere_api", RANGE_ID, result), default=str)
        for text in (*_all_values(fw), json.dumps(result.__dict__, default=str), stored, caplog.text):
            assert FW_PW not in text
        for text in (json.dumps(result.__dict__, default=str), stored, caplog.text):
            assert first["guestinfo.tn.pfsense.admin"] not in text  # nor the hash

    def test_without_an_ack_nothing_is_marked_set_and_the_hash_stays(self, vc):
        result, fw = _build(vc, "pfsense", {"fw": [_acct("admin")]})
        out = _vm_out(result, "fw")
        assert out["template_login"]["status"] == "inconclusive" and out["accounts_applied"] == []
        assert out["admin_login"]["hash_cleared"] is False
        assert len(fw.reconfig_specs) == 1  # not cleared: the appliance may still need it
        assert any(f"{R8}-fw" in w and "template-password check inconclusive" in w for w in result.warnings)
        assert f"{R8}-fw" not in result.not_ready  # inconclusive warns; only accepted fails readiness

    def test_a_hook_failure_is_recorded_not_a_pass(self, vc):
        result, _ = _build(vc, "pfsense", {"fw": [_acct("admin")]},
                           ack=lambda vm, extra: "failed no single admin user in /cf/conf/config.xml")
        login = _vm_out(result, "fw")["template_login"]
        assert login["status"] == "inconclusive" and "no single admin user" in login["detail"]

    def test_ssh_accepting_the_template_password_fails_the_vm_under_enforce(self, uplink, monkeypatch):
        monkeypatch.setattr(aa, "reachable", lambda host, *a, **k: host == "10.30.32.101")
        monkeypatch.setattr(aa, "paramiko_login", lambda host, user, pw, *a: True)
        result, _ = _build(uplink, "pfsense", {"fw": [_acct("admin")]}, {"uplink_ip": "10.30.32.101"},
                           ack=_pf_hook)
        login = _vm_out(result, "fw")["template_login"]
        assert (login["status"], login["method"]) == ("accepted", "ssh")
        assert f"{R8}-fw" in result.not_ready and any("Not safe" in e for e in result.errors)
        assert _vm_out(result, "fw")["accounts_applied"] == []

    def test_ssh_refusal_is_the_proof_when_the_worker_reaches_the_wan(self, uplink, monkeypatch):
        seen = []
        monkeypatch.setattr(aa, "reachable", lambda host, *a, **k: True)

        def login(host, user, pw, *a):
            seen.append((host, user))
            return pw.reveal() == FW_PW

        monkeypatch.setattr(aa, "paramiko_login", login)
        result, _ = _build(uplink, "pfsense", {"fw": [_acct("admin")]}, {"uplink_ip": "10.30.32.101"})
        login_ = _vm_out(result, "fw")["template_login"]
        assert (login_["status"], login_["method"]) == ("rejected", "ssh")
        assert seen == [("10.30.32.101", "admin"), ("10.30.32.101", "admin")]
        assert _vm_out(result, "fw")["accounts_applied"] == ["admin"]

    def test_opnsense_sets_root(self, vc):
        result, fw = _build(vc, "opnsense", {"fw": [_acct("root")]},
                            ack=lambda vm, extra: aa.fingerprint(extra["guestinfo.tn.opnsense.admin"]))
        first = {o.key: o.value for o in fw.reconfig_specs[0].extraConfig}
        assert _bcrypt_ok(first["guestinfo.tn.opnsense.admin"], FW_PW)
        out = _vm_out(result, "fw")
        assert out["template_login"]["status"] == "rejected" and out["accounts_applied"] == ["root"]
        assert {o.key: o.value for o in fw.reconfig_specs[-1].extraConfig} == {"guestinfo.tn.opnsense.admin": ""}

    def test_vyos_gets_an_encrypted_password_and_the_hash_leaves_the_user_data(self, vc):
        def hook(vm, extra):
            cmds = vy.commands_of(extra)
            line = next(c for c in cmds if "encrypted-password" in c)
            return aa.fingerprint(line.rsplit(" ", 1)[1].strip("'"))

        result, fw = _build(vc, "vyos", {"fw": [_acct("vyos")]}, ack=hook)
        first = {o.key: o.value for o in fw.reconfig_specs[0].extraConfig}
        line = next(c for c in vy.commands_of(first) if "encrypted-password" in c)
        assert line.startswith("set system login user vyos authentication encrypted-password '$6$")
        assert _crypt_ok(line.rsplit(" ", 1)[1].strip("'"), FW_PW)
        out = _vm_out(result, "fw")
        assert (out["template_login"]["status"], out["accounts_applied"]) == ("rejected", ["vyos"])
        last = {o.key: o.value for o in fw.reconfig_specs[-1].extraConfig}
        assert set(last) == {"guestinfo.userdata"}
        assert not any("encrypted-password" in c for c in vy.commands_of(last))
        assert FW_PW not in "".join(_all_values(fw))

    def test_an_appliance_without_its_admin_row_is_inconclusive_and_gets_no_key(self, vc):
        result, fw = _build(vc, "pfsense", {})
        assert not any(k.endswith(".admin") for k in {o.key for o in fw.reconfig_specs[0].extraConfig})
        login = _vm_out(result, "fw")["template_login"]
        assert (login["status"], login["method"]) == ("inconclusive", "none")
        assert "admin_login" not in _vm_out(result, "fw")

    def test_what_was_proven_is_recorded_as_applied(self, vc, monkeypatch):
        result, _ = _build(vc, "pfsense", {"fw": [_acct("admin")]}, ack=_pf_hook)

        @contextmanager
        def session():
            yield None

        recorded = []
        monkeypatch.setattr(range_accounts.db_ops, "mark_accounts_applied",
                            lambda db, rid, pairs: recorded.append(sorted(pairs)))
        range_accounts.record_applied(session, RANGE_ID, result.vms)
        assert ("fw", "admin") in recorded[0]


def test_no_secret_in_the_vm_plan_after_the_build(vc):
    """The admin login (password and hash) ends with the provision call, like the other secrets."""
    tpl = {**TEMPLATE}
    out = render.render_topology(tpl, RANGE_ID, _vsp.IMAGES.get)
    vms = [dict(v, _accounts=[_acct("admin")]) if v["node_id"] == "fw" else dict(v) for v in out["vm_definitions"]]
    prov = _prov(vc)
    seen = []
    real = prov._appliance_check_sync

    def spy(si, vm_def, out_):
        seen.append(vm_def)
        return real(si, vm_def, out_)

    prov._appliance_check_sync = spy
    _run(prov.provision(RANGE_ID, {"name": "lab", "vms": vms, "networks": out["network_definitions"]}, {}))
    assert seen and "_appliance_admin" not in seen[0]
