"""Security Onion per-range setup (worker/provisioners/vsphere_securityonion.py).

Guest operations are faked at the GuestSession level, as in test_vsphere_roles.py. These
tests cover the plan (which NIC is management and which is monitor, by MAC, and every
answer checked against so-setup's own rules), the answer file and the runner, the status
polling, and what a failure or a timeout reports. They also check that no password reaches
a log, a warning, an error or the output, except sealed. The provision flow around it is in
test_vsphere_provision.py, TestSecurityOnion.
"""

from __future__ import annotations

import base64
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from worker import render, secretbox
from worker.provisioners import vsphere_guest as guest
from worker.provisioners import vsphere_infra as infra
from worker.provisioners import vsphere_securityonion as so

ROOT = Path(__file__).resolve().parents[2]
RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
INSTALL = {"VSPHERE_SO_INSTALL_USER": "soadmin", "VSPHERE_SO_INSTALL_PASSWORD": "Build-Pass-77"}
MACS = ["00:50:56:AA:00:01", "00:50:56:AA:00:02"]
NETWORKS = [
    {"name": "soc_tools", "vlan_id": 202, "cidr": "10.60.202.0/24"},
    {"name": "network_monitoring", "vlan_id": 203, "cidr": "10.60.203.0/24"},
    {"name": "management", "vlan_id": 204, "cidr": "10.60.204.0/24"},
]


def _vm(**extra) -> dict:
    nics = [
        {"vlan": 202, "network": "soc_tools", "ip": "10.60.202.60", "prefix": 24, "gateway": "10.60.202.1"},
        {"vlan": 203, "network": "network_monitoring", "ip": "", "prefix": 24, "gateway": "10.60.203.1",
         "monitor": True},
    ]
    return {"name": f"{RANGE_ID[:8]}-seconion01", "node_id": "seconion01", "os": "securityonion",
            "template_name": "securityonion", "nics": nics, **extra}


def _acct(username: str, password: str, admin: bool = False) -> guest.RangeAccount:
    return guest.RangeAccount(username, guest.Secret(password), admin)


def _stage_files(arguments: str) -> dict[str, str]:
    """The files the staging command writes: {path: content}, decoded from its base64."""
    found = re.findall(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d > (\S+)", arguments)
    return {path.rstrip(";"): base64.b64decode(b64).decode() for b64, path in found}


def _answers_from(arguments: str) -> dict[str, str]:
    text = _stage_files(arguments)[so.ANSWERS_FILE]
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = shlex.split(value)[0] if value else ""
    return out


class FakeSession:
    """GuestSession's surface: wait_ready, run, _clock, _sleep. ``codes`` are the status
    numbers successive polls read; the clock moves ``tick`` seconds per sleep."""

    def __init__(self, creds, codes=(), *, stage_code=0, tick=30.0, raise_on_stage: str = "", checks=None):
        self.checks = dict(checks or {})  # label -> exit code of an account check (default 0)
        self.vm = SimpleNamespace(name="seconion01")
        self.creds = creds
        self.codes = list(codes)
        self.stage_code = stage_code
        self.raise_on_stage = raise_on_stage
        self.commands: list[guest.GuestCommand] = []
        self.t = 0.0
        self.tick = tick
        self._clock = lambda: self.t
        self._sleep = self.sleep

    def sleep(self, s):
        self.t += self.tick

    def wait_ready(self, deadline):
        pass

    def run(self, cmd, deadline):
        self.commands.append(cmd)
        if cmd.label == "so-setup-start":
            if self.raise_on_stage:
                raise RuntimeError(self.raise_on_stage)
            return ("ok" if self.stage_code == 0 else "failed"), self.stage_code
        if cmd.label != "so-setup-status":  # account checks and the admin password
            code = self.checks.get(cmd.label, 0)
            return ("ok" if code in cmd.ok_codes else "failed"), code
        code = self.codes.pop(0) if self.codes else 2
        return ("ok" if code in cmd.ok_codes else "failed"), code


def _setup(vm_def=None, codes=(0,), *, deadline=3600.0, budget=3300.0, env=INSTALL, macs=MACS,
           build_wait=100000.0, **kw):
    sessions: list[FakeSession] = []

    def session_for(creds):
        sessions.append(FakeSession(creds, codes, **kw))
        return sessions[-1]

    out, warnings = so.setup(session_for, vm_def or _vm(), macs, NETWORKS, {203}, deadline, budget, poll=30,
                             env=env, build_wait=build_wait)
    return out, warnings, (sessions[0] if sessions else None)


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


class TestPlan:
    def test_soc_training_gets_management_on_soc_tools_and_monitors_the_mirror_zone(self):
        tpl = yaml.safe_load((ROOT / "content/ranges/soc-training/template.yaml").read_text())
        out = render.render_topology(tpl, RANGE_ID, lambda alias: alias)
        vm = next(v for v in out["vm_definitions"] if v["node_id"] == "seconion01")
        assert so.is_securityonion(vm)
        plans, _ = infra.plan_mirrors(infra.mirror_rules(tpl), out["network_definitions"])
        p = so.plan(vm, out["network_definitions"], {pl["dst_vlan"] for pl in plans}, env=INSTALL)
        assert vm["nics"][p.mgmt_index]["network"] == "soc_tools"
        assert vm["nics"][p.monitor_index]["network"] == "network_monitoring"
        assert vm["nics"][p.monitor_index]["ip"] == ""  # a sniffing NIC has no address
        assert (p.hostname, p.mgmt_ip, p.prefix, p.gateway, p.dns) == (
            "seconion01", "10.60.202.60", 24, "10.60.202.1", ["10.60.202.1"])
        assert p.allow == ["10.60.202.0/24", "10.60.204.0/24"]  # its own zone, then so_allow: [management]
        assert p.web_user == "analyst@range.local" and p.web_generated

    def test_monitor_nic_is_the_mirror_destination_when_none_is_flagged(self):
        vm = _vm()
        vm["nics"] = [{**vm["nics"][1], "monitor": False, "network": "span"}, vm["nics"][0]]  # monitor first
        p = so.plan(vm, NETWORKS, {203}, env=INSTALL)
        assert (p.mgmt_index, p.monitor_index) == (1, 0)

    def test_monitor_nic_falls_back_to_a_monitoring_network_name(self):
        vm = _vm()
        vm["nics"][1].pop("monitor")
        assert so.plan(vm, NETWORKS, set(), env=INSTALL).monitor_index == 1

    def test_one_nic_is_not_a_standalone_node(self):
        vm = _vm()
        vm["nics"] = vm["nics"][:1]
        with pytest.raises(so.PlanError, match="two NICs"):
            so.plan(vm, NETWORKS, {203}, env=INSTALL)

    def test_no_install_login_is_a_plan_error(self):
        with pytest.raises(so.PlanError, match="no install login"):
            so.plan(_vm(), NETWORKS, {203}, env={})

    def test_admin_and_web_login_come_from_the_declared_accounts(self):
        """First role: admin = the OS admin; first role: user = the SOC web login. The build
        login (environment) still runs guest operations: a fresh clone has no range account."""
        vm = _vm(_accounts=[_acct("tnadmin", "Range-Admin-1", admin=True), _acct("Analyst", "Soc-Web-Pass-1")])
        p = so.plan(vm, NETWORKS, {203}, env=INSTALL)
        assert (p.install.username, p.install_source) == ("soadmin", "VSPHERE_SO_INSTALL_USER")
        assert (p.admin.username, p.admin.password.reveal(), p.admin_last) == ("tnadmin", "Range-Admin-1", False)
        assert (p.web_user, p.web_password.reveal(), p.web_generated) == ("analyst@range.local", "Soc-Web-Pass-1", False)
        text = so.answers(p, MACS)
        assert "TN_ADMIN_USER=tnadmin" in text and "TN_ADMIN_PASSWORD=Range-Admin-1" in text

    def test_without_a_build_login_the_admin_account_logs_in(self):
        vm = _vm(_accounts=[_acct("tnadmin", "Range-Admin-1", admin=True)])
        p = so.plan(vm, NETWORKS, {203}, env={})
        assert (p.install.username, p.install.password.reveal(), p.install_source) == (
            "tnadmin", "Range-Admin-1", "range admin account")
        assert not p.admin_last

    def test_the_windows_default_password_is_not_the_build_login(self):
        """TN_RANGE_DEFAULT_PASSWORD is the Windows Administrator fallback; a Linux template
        keeps its own build password (ADR 0012 §5), so the SO login needs VSPHERE_SO_INSTALL_*."""
        with pytest.raises(so.PlanError, match="no install login"):
            so.plan(_vm(), NETWORKS, {203},
                    env={"VSPHERE_SO_INSTALL_USER": "soadmin", "TN_RANGE_DEFAULT_PASSWORD": "Platform-Default-1"})

    def test_admin_that_is_the_build_login_gets_its_password_last(self):
        vm = _vm(_accounts=[_acct("soadmin", "Range-Admin-1", admin=True)])
        p = so.plan(vm, NETWORKS, {203}, env=INSTALL)
        assert p.admin_last
        assert "TN_ADMIN_USER=''" in so.answers(p, MACS)  # not before so-setup: it would lock the login out

    def test_admin_with_a_name_linux_refuses_is_a_note(self):
        p = so.plan(_vm(_accounts=[_acct("Bad.Name", "Range-Admin-1", admin=True)]), NETWORKS, {203}, env=INSTALL)
        assert p.admin is None and p.notes == ["admin account 'Bad.Name' is not a Linux login name; not set"]

    def test_web_login_declared_as_an_email_is_kept(self):
        p = so.plan(_vm(_accounts=[_acct("soc@corp.test", "Gen-Pass.123#x")]), NETWORKS, {203}, env=INSTALL)
        assert p.web_user == "soc@corp.test" and not p.web_generated

    def test_api_generated_passwords_pass_so_setup_rules(self):
        """range_credentials/accounts.py generates from letters, digits and -_.!#%+=."""
        assert so._password_problem("aB3-_.!#%+=xyzXYZ0") is None

    @pytest.mark.parametrize("bad", ["short", "has$dollar1", "quote'd-pass", 'dq"pass12', "back\\slash1", "x" * 73])
    def test_web_password_so_setup_would_reject_is_refused_up_front(self, bad):
        """Under a test profile so-setup re-prompts a bad answer with a prompt that returns at
        once: an endless loop. So it never gets one."""
        with pytest.raises(so.PlanError, match="so-setup's rules"):
            so.plan(_vm(_accounts=[_acct("soc", bad)]), NETWORKS, {203}, env=INSTALL)

    def test_management_without_a_gateway_is_refused(self):
        vm = _vm()
        vm["nics"][0]["gateway"] = ""
        with pytest.raises(so.PlanError, match="not valid IPv4"):
            so.plan(vm, NETWORKS, {203}, env=INSTALL)

    def test_unknown_so_allow_entry_is_a_note(self):
        p = so.plan(_vm(so_allow=["management", "nowhere", "192.168.5.0/24"]), NETWORKS, {203}, env=INSTALL)
        assert p.allow == ["10.60.202.0/24", "10.60.204.0/24", "192.168.5.0/24"]
        assert p.notes == ["so_allow entry 'nowhere' is neither a range zone nor an IPv4 CIDR (skipped)"]

    @pytest.mark.parametrize(("vm", "expected"), [
        ({"os": "securityonion"}, True),
        ({"os": "ubuntu-2404", "template_name": "securityonion@esx02-20261019"}, True),
        ({"os": "ubuntu-2404", "template_name": "tmpl-ubuntu-2404"}, False),
    ])
    def test_catalogue_id(self, vm, expected):
        assert so.is_securityonion(vm) is expected


# --------------------------------------------------------------------------- #
# Answer file, staging command, runner
# --------------------------------------------------------------------------- #


class TestAnswersAndRunner:
    def test_answer_file_maps_nics_by_mac_and_overrides_the_test_profile(self):
        vm = _vm()
        vm["nics"] = [vm["nics"][1], vm["nics"][0]]  # the monitor NIC is the VM's first NIC here
        p = so.plan(vm, NETWORKS, {203}, env=INSTALL)
        text = so.answers(p, MACS)
        assert "TN_MON_MAC=00:50:56:aa:00:01" in text and "TN_MGMT_MAC=00:50:56:aa:00:02" in text
        for line in ("install_type=STANDALONE", "HOSTNAME=seconion01", "address_type=STATIC",
                     "manager_ip_mask=10.60.202.60/24", "MGATEWAY=10.60.202.1", "MDNS=10.60.202.1",
                     "MSEARCH=range.local", "is_airgap=true", "ALLOW_CIDR=10.60.202.0/24", "MINION_CIDR=",
                     "WEBUSER=analyst@range.local", "TN_INSTALL_USER=soadmin", "TN_WEB_GENERATED=1"):
            assert line in text.splitlines(), line
        pw = p.web_password.reveal()
        assert f"WEBPASSWD1={shlex.quote(pw)}" in text and f"WEBPASSWD2={shlex.quote(pw)}" in text

    def test_staging_writes_both_files_once_and_starts_the_runner_detached(self):
        p = so.plan(_vm(), NETWORKS, {203}, env=INSTALL)
        cmd = so.as_root("so-setup-start", so.stage_script(so.answers(p, MACS)), p.install)
        assert cmd.program == "/bin/sh"
        assert "/usr/bin/sudo -S -p" in cmd.arguments  # the template's install user may need a sudo password
        files = _stage_files(shlex.split(cmd.arguments)[1])
        assert files[so.RUNNER_FILE] == so.RUNNER_SCRIPT
        assert "TN_MGMT_MAC=00:50:56:aa:00:01" in files[so.ANSWERS_FILE]
        inner = shlex.split(shlex.split(cmd.arguments)[1].split("/bin/sh -c ", 1)[1])[0]
        assert inner.startswith(f"set -e; umask 077; if [ -e {so.STATUS_FILE} ]; then exit 0; fi")  # never twice
        assert inner.endswith(f"setsid nohup /bin/bash {so.RUNNER_FILE} </dev/null >/dev/null 2>&1 &")

    def test_root_login_needs_no_sudo(self):
        creds = guest.GuestCredentials("linux", "root", guest.Secret("Root-Pass-1"))
        cmd = so.as_root("x", "true", creds)
        assert "sudo" not in cmd.arguments and "Root-Pass-1" not in cmd.arguments

    def test_runner_patches_only_the_test_profile_sudoers_line_and_runs_airgap(self):
        r = so.RUNNER_SCRIPT
        assert "grep -c '^[[:space:]]*update_sudoers_for_testing[[:space:]]*$'" in r and "|| finish 12" in r
        assert '\\1. $ANSWERS|' in r and f"ANSWERS={so.ANSWERS_FILE}\n" in r
        assert f'bash "$PATCHED" iso {so.TEST_PROFILE} </dev/null' in r
        # so-setup's test profile: install type from the first word, airgap from "-airgap",
        # and none of the other role or DHCP markers it looks for.
        assert so.TEST_PROFILE.split("-")[0] == "standalone" and "-airgap" in so.TEST_PROFILE
        for marker in ("-sensor", "-search", "-heavynode", "-desktop", "-idh", "-receiver", "-fleet", "-net",
                       "-iso", "-cloud"):
            assert marker not in so.TEST_PROFILE
        assert "until so-status -q" in r and "[ -f /root/success ] || finish 13" in r
        assert 'rm -f "$ANSWERS"' in r  # the answer file (web password) does not outlive setup

    def test_poll_reads_the_status_without_root_or_secrets(self):
        cmd = so.poll_command()
        assert "sudo" not in cmd.arguments and cmd.ok_codes == frozenset({0})
        assert so.STATUS_FILE in cmd.arguments


# --------------------------------------------------------------------------- #
# Running it: outcomes, timeouts, secrets
# --------------------------------------------------------------------------- #


class TestSetup:
    def test_healthy_node_is_ok(self):
        out, warnings, s = _setup(codes=[2, 2, 3, 0])
        assert out["status"] == "ok" and out["detail"] == so.STATUS[0]
        assert out["management_mac"] == "00:50:56:aa:00:01" and out["monitor_mac"] == "00:50:56:aa:00:02"
        assert out["web_url"] == "https://10.60.202.60" and out["web_password"] == "generated"
        labels = [c.label for c in s.commands]
        assert labels == ["so-setup-start"] + ["so-setup-status"] * 4
        assert s.creds.username == "soadmin"
        # Only the generated-password notice: where it is kept, never what it is.
        (note,) = warnings
        assert "generated password" in note and "analyst@range.local" in note

    def test_runner_failure_is_a_warning_with_the_reason(self):
        out, warnings, _ = _setup(codes=[2, 13])
        assert out["status"] == "failed" and out["detail"] == so.STATUS[13]
        assert any(w.endswith(f"Security Onion setup failed: {so.STATUS[13]}") for w in warnings)

    def test_staging_refused_is_a_failure_naming_the_login(self):
        out, warnings, _ = _setup(stage_code=1)
        assert out["status"] == "failed" and "soadmin have sudo" in out["detail"]

    def test_timeout_is_a_warning_and_setup_carries_on(self):
        out, warnings, s = _setup(codes=[2] * 1000, deadline=600, budget=3300)
        assert out["status"] == "timeout"
        assert "VSPHERE_SO_SETUP_TIMEOUT" in out["detail"] and so.STATUS[2] in out["detail"]
        assert s.t >= 600 and s.t < 700  # it stopped waiting at the deadline
        assert any("Security Onion setup timeout" in w for w in warnings)

    def test_provision_budget_ending_first_reports_running(self):
        out, _, s = _setup(codes=[3] * 1000, deadline=3600, budget=900)
        assert out["status"] == "running" and so.STATUS[3] in out["detail"]
        assert s.t < 1000

    def test_no_budget_left_does_not_start(self):
        out, warnings, s = _setup(budget=10)
        assert out["status"] == "skipped" and s.commands == []
        assert any("not started" in w for w in warnings)

    def test_plan_problem_skips_without_a_session(self):
        out, warnings, s = _setup(env={})
        assert s is None and out["status"] == "skipped"
        assert warnings == [f"VM {_vm()['name']}: Security Onion setup skipped: {out['detail']}"]

    def test_passwords_never_reach_logs_warnings_or_output(self, caplog, monkeypatch):
        caplog.set_level("DEBUG")
        monkeypatch.setenv("TN_SECRETS_KEY", "k" * 40)
        web = "Soc-Web-Pass-1"
        vm = _vm(_accounts=[_acct("soc", web), _acct("tnadmin", "Range-Admin-1", admin=True)])
        leak = f"login soadmin/{INSTALL['VSPHERE_SO_INSTALL_PASSWORD']} refused (web {web}, admin Range-Admin-1)"
        out, warnings, s = _setup(vm, raise_on_stage=leak)
        assert out["status"] == "failed" and "***" in out["detail"]
        generated_out, generated_warnings, _ = _setup(codes=[0])
        sealed = generated_out["web_password_sealed"]
        plain = secretbox.unseal(sealed)
        assert sealed.startswith(secretbox.PREFIX) and plain and plain not in sealed
        text = repr((out, warnings, generated_out, generated_warnings)) + caplog.text
        for secret in (INSTALL["VSPHERE_SO_INSTALL_PASSWORD"], web, plain, "Range-Admin-1"):
            assert secret not in text
        assert INSTALL["VSPHERE_SO_INSTALL_PASSWORD"] not in repr(s.creds)

    def test_accounts_are_applied_only_once_verified_in_the_guest(self):
        vm = _vm(_accounts=[_acct("analyst@range.local", "Web-Pass.1"), _acct("tnadmin", "Range-Admin-1", admin=True),
                            _acct("extra", "Extra-Pass-1")])
        out, warnings, s = _setup(vm, codes=[0])
        assert out["accounts_applied"] == ["analyst@range.local", "tnadmin"]
        # The checks: so-user lists the web login (as root), getent finds the admin (no root).
        web = next(c for c in s.commands if c.label == "so-web-check")
        admin = next(c for c in s.commands if c.label == "so-admin-check")
        assert "so-user list" in web.arguments and "grep -qxF analyst@range.local" in web.arguments
        assert admin.arguments == "-c " + shlex.quote("getent passwd tnadmin >/dev/null")
        # Only the first role: user account is the web login; another is not set.
        assert any(w.endswith("account(s) not verified, recorded as not set: extra") for w in warnings)

    def test_a_failed_check_leaves_the_account_not_applied(self):
        vm = _vm(_accounts=[_acct("analyst@range.local", "Web-Pass.1"), _acct("tnadmin", "Range-Admin-1", admin=True)])
        out, warnings, _ = _setup(vm, codes=[0], checks={"so-web-check": 1})
        assert out["status"] == "ok" and out["accounts_applied"] == ["tnadmin"]
        assert any("recorded as not set: analyst@range.local" in w for w in warnings)

    def test_a_failed_setup_still_reports_an_admin_the_runner_created(self):
        vm = _vm(_accounts=[_acct("analyst@range.local", "Web-Pass.1"), _acct("tnadmin", "Range-Admin-1", admin=True)])
        out, _, s = _setup(vm, codes=[13])
        assert out["status"] == "failed" and out["accounts_applied"] == ["tnadmin"]
        assert "so-web-check" not in [c.label for c in s.commands]  # no SOC user without a finished setup

    def test_nothing_is_applied_when_setup_never_started(self):
        vm = _vm(_accounts=[_acct("tnadmin", "Range-Admin-1", admin=True)])
        out, warnings, s = _setup(vm, stage_code=1)
        assert out["accounts_applied"] == [] and [c.label for c in s.commands] == ["so-setup-start"]
        out, _, _ = _setup(vm, env={}, budget=10)
        assert out["accounts_applied"] == []

    def test_admin_that_is_the_login_gets_its_password_after_the_node_is_healthy(self):
        vm = _vm(_accounts=[_acct("soadmin", "Range-Admin-1", admin=True)])
        out, warnings, s = _setup(vm, codes=[2, 0])
        assert out["status"] == "ok" and out["admin_user"] == "soadmin"
        assert [c.label for c in s.commands] == ["so-setup-start", "so-setup-status", "so-setup-status",
                                                 "so-admin-password"]  # the password change is last
        assert "chpasswd" in s.commands[-1].arguments
        assert out["accounts_applied"] == ["soadmin"]
        out, _, _ = _setup(vm, codes=[0], checks={"so-admin-password": 1})
        assert out["accounts_applied"] == []  # its declared password did not take: not applied

    def test_admin_that_is_the_login_is_left_alone_when_setup_does_not_finish(self):
        vm = _vm(_accounts=[_acct("soadmin", "Range-Admin-1", admin=True)])
        out, warnings, s = _setup(vm, codes=[2] * 100, deadline=120)
        assert out["status"] == "timeout" and "so-admin-password" not in [c.label for c in s.commands]
        assert any("is unchanged" in w for w in warnings)

    def test_without_a_secrets_key_the_generated_password_stays_in_the_guest_only(self, monkeypatch):
        monkeypatch.delenv("TN_SECRETS_KEY", raising=False)
        out, warnings, _ = _setup(codes=[0])
        assert "web_password_sealed" not in out
        assert any(so.CREDENTIALS_FILE in w and "not sealed" in w for w in warnings)

    def test_fewer_nics_on_the_vm_than_planned_is_skipped(self):
        out, warnings, _ = _setup(macs=MACS[:1])
        assert out["status"] == "skipped" and "1 NICs" in out["detail"]


# --------------------------------------------------------------------------- #
# Build wait and the reconcile path (worker/setup_reconcile.py)
# --------------------------------------------------------------------------- #


class TestReconcilePath:
    def test_build_wait_zero_leaves_it_running_with_a_reconcile_block(self):
        out, warnings, _ = _setup(codes=[2] * 100, build_wait=0)
        assert out["status"] == "running"
        rec = out["reconcile"]
        assert rec["kind"] == "securityonion" and rec["status_file"] == so.STATUS_FILE
        assert rec["web_user"] == "analyst@range.local" and rec["timeout"] == so.VSPHERE_SO_SETUP_TIMEOUT
        assert any("reconcile task records the result" in w for w in warnings)

    def test_reconcile_healthy_verifies_accounts(self):
        info = {"kind": "securityonion", "status_file": so.STATUS_FILE, "since": 0.0, "timeout": 7200,
                "web_user": "analyst@range.local", "web_generated": False, "admin_user": "soadmin",
                "node": "seconion01", "install_user": "soadmin", "install_from_env": True}
        s = FakeSession(guest.GuestCredentials("linux", "soadmin", guest.Secret("x")), codes=[0])
        out, verified = so.reconcile(s, info, epoch=lambda: 10.0)
        assert out["status"] == "ok"
        assert verified == {"soadmin", "analyst@range.local"}

    def test_reconcile_failed_and_timeout_and_still_running(self):
        base = {"status_file": so.STATUS_FILE, "since": 0.0, "timeout": 50, "web_user": "a@b.c",
                "web_generated": True, "admin_user": None, "node": "n"}
        c = guest.GuestCredentials("linux", "u", guest.Secret("x"))
        out, _ = so.reconcile(FakeSession(c, codes=[13]), base, epoch=lambda: 10.0)
        assert out["status"] == "failed"
        out, _ = so.reconcile(FakeSession(c, codes=[2]), base, epoch=lambda: 1000.0)
        assert out["status"] == "timeout"
        assert so.reconcile(FakeSession(c, codes=[2]), base, epoch=lambda: 10.0) == (None, set())
