"""Deploy-time software: the catalogue, the guest commands, guest-ops sessions, uplink pool.

The end-to-end provision flow (customization -> guest ops -> result) is in
test_vsphere_provision.py; these are the pieces on their own.
"""

from __future__ import annotations

import datetime as dt
import pickle
from types import SimpleNamespace

import pytest
from pyVmomi import vim
from worker import software_catalogue as cat
from worker import uplink_pool
from worker.provisioners import vsphere_guest as guest
from worker.provisioners import vsphere_infra as infra

# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def catalogue():
    return cat.load()  # the real content/catalogue/software_catalogue.yaml


class TestCatalogue:
    def test_the_shipped_file_loads_and_covers_the_build_sheet(self, catalogue):
        for name in ("7zip", "googlechrome", "firefoxesr", "notepadplusplus", "vscode", "git", "python",
                     "wireshark", "sysinternals", "putty", "winscp", "adobereader", "vlc", "nginx", "apache2",
                     "mariadb-server", "postgresql", "redis", "bind9", "postfix", "tcpdump"):
            assert name in catalogue.entries, name
        assert {"dns", "iis", "dhcp"} <= catalogue.roles

    def test_windows_maps_to_chocolatey(self, catalogue):
        specs, warnings = cat.resolve(["7zip", "Chrome", "notepad++"], "windows", "windows-server-2022", catalogue)
        assert [(s.name, s.manager, s.packages) for s in specs] == [
            ("7zip", "choco", ("7zip",)), ("googlechrome", "choco", ("googlechrome",)),
            ("notepadplusplus", "choco", ("notepadplusplus",))]
        assert warnings == []

    def test_linux_apt_or_dnf_by_distro(self, catalogue):
        apt, _ = cat.resolve(["apache", "bind9"], "linux", "ubuntu-2404", catalogue)
        assert [(s.manager, s.packages) for s in apt] == [("apt", ("apache2",)), ("apt", ("bind9", "bind9-utils"))]
        dnf, _ = cat.resolve(["apache", "bind9"], "linux", "rocky-9", catalogue)
        assert [(s.manager, s.packages) for s in dnf] == [("dnf", ("httpd",)), ("dnf", ("bind", "bind-utils"))]

    def test_unknown_warns_roles_are_silent_and_duplicates_collapse(self, catalogue):
        specs, warnings = cat.resolve(["dns", "IIS", "frobnicator", "git", "Git"], "windows", "win11", catalogue)
        assert [s.name for s in specs] == ["git"]
        assert warnings == ["software 'frobnicator' is not in the software catalogue; skipped"]

    def test_known_but_not_for_this_os_family_warns(self, catalogue):
        specs, warnings = cat.resolve(["sysinternals"], "linux", "ubuntu-2404", catalogue)
        assert specs == [] and "no apt install" in warnings[0]
        specs, warnings = cat.resolve(["nginx"], "windows", "srv2022", catalogue)
        assert specs == [] and "no windows install" in warnings[0]

    def test_pinned_version(self):
        c = cat.parse({"software": {"python": {"windows": {"choco": "python", "version": "3.12.7"}}}})
        (spec,), _ = cat.resolve(["python"], "windows", "win", c)
        assert spec.version == "3.12.7"
        assert guest.windows_commands([spec], "http://d/")[0].arguments.endswith("--version 3.12.7")

    def test_bad_or_missing_file(self, tmp_path):
        with pytest.raises(cat.CatalogueError):
            cat.load(tmp_path / "nope.yaml")
        (tmp_path / "bad.yaml").write_text("roles: []\n")
        with pytest.raises(cat.CatalogueError):
            cat.load(tmp_path / "bad.yaml")

    def test_env_overrides_the_path(self, tmp_path, monkeypatch):
        f = tmp_path / "c.yaml"
        f.write_text("software:\n  foo:\n    linux: {apt: [foo]}\n")
        monkeypatch.setenv("TN_SOFTWARE_CATALOGUE", str(f))
        assert set(cat.load().entries) == {"foo"}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


class TestCommands:
    def test_choco_from_the_depot_feed_only(self):
        specs = [cat.InstallSpec("7zip", "choco", ("7zip",))]
        (cmd,) = guest.windows_commands(specs, "http://10.30.32.10:8081/repository/chocolatey/")
        assert cmd.program == r"C:\ProgramData\chocolatey\bin\choco.exe"
        assert cmd.arguments == 'install 7zip -y --no-progress --source "http://10.30.32.10:8081/repository/chocolatey/"'
        assert 3010 in cmd.ok_codes and 1 not in cmd.ok_codes

    def test_apt_through_the_depot_proxy(self):
        cmds = guest.linux_commands([cat.InstallSpec("nginx", "apt", ("nginx",))], "http://10.30.32.10")
        assert [c.label for c in cmds] == ["cloud-init", "apt-update", "nginx"]
        assert [c.step for c in cmds] == [True, True, False]
        assert all(c.program == "/usr/bin/sudo" and c.arguments.startswith("-n /bin/sh -c ") for c in cmds)
        assert "Acquire::http::Proxy=http://10.30.32.10" in cmds[2].arguments
        assert "Acquire::https::Proxy=http://10.30.32.10" in cmds[2].arguments
        assert ">>/var/log/tn-software-install.log" in cmds[2].arguments

    def test_dnf_through_the_depot_proxy(self):
        cmds = guest.linux_commands([cat.InstallSpec("nginx", "dnf", ("nginx",))], "http://10.30.32.10")
        assert [c.label for c in cmds] == ["cloud-init", "nginx"]  # no apt update
        assert "dnf -y -q --setopt=proxy=http://10.30.32.10 install nginx" in cmds[1].arguments

    def test_cleanup_locks_and_deletes_the_user(self):
        cmd = guest.linux_cleanup_command()
        for part in ("passwd -l", "usermod --expiredate 1", "pkill -KILL -u", "userdel -r", "nohup", "sleep 15"):
            assert part in cmd.arguments

    def test_cloud_init_user(self):
        creds = guest.linux_credentials()
        user = guest.cloud_init_user(creds, today=dt.date(2026, 10, 3))
        assert user["name"] == "tn-install" and user["lock_passwd"] is False
        assert user["expiredate"] == "2026-10-05"
        hashed = user.get("hashed_passwd")
        assert hashed.startswith("$6$") if hashed else user["plain_text_passwd"] == creds.password.reveal()


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


class TestSecret:
    def test_never_prints_or_pickles(self):
        creds = guest.windows_credentials()
        pw = creds.password.reveal()
        assert pw not in repr(creds) and pw not in str(creds.password) and pw not in f"{creds}"
        with pytest.raises(TypeError):
            pickle.dumps(creds)
        assert guest.redact(f"bad {pw} here", creds, None) == "bad *** here"

    def test_windows_password_meets_complexity_and_is_used_by_sysprep(self):
        creds = guest.windows_credentials()
        pw = creds.password.reveal()
        assert any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw) and "!" in pw
        vm_def = {"name": "r-dc01", "node_id": "dc01", "nics": [{"ip": "10.0.0.5", "gateway": "10.0.0.1"}]}
        spec = infra.windows_customization(vm_def, pw)
        assert spec.identity.guiUnattended.password.value == pw
        other = infra.windows_customization(vm_def)
        assert other.identity.guiUnattended.password.value not in ("", pw)


# --------------------------------------------------------------------------- #
# Guest-ops session
# --------------------------------------------------------------------------- #


class FakeOps:
    def __init__(self, ready_after=0, codes=None, hang=False):
        self.processManager = self.authManager = self
        self.ready_after = ready_after
        self.codes = list(codes or [])
        self.hang = hang
        self.validations = 0
        self.terminated = []

    def ValidateCredentialsInGuest(self, vm, auth):  # noqa: N802
        self.validations += 1
        if self.validations <= self.ready_after:
            raise vim.fault.InvalidGuestLogin()

    def StartProgramInGuest(self, vm, auth, spec):  # noqa: N802
        return 7

    def ListProcessesInGuest(self, vm, auth, pids):  # noqa: N802
        if self.hang:
            return [SimpleNamespace(endTime=None, exitCode=None)]
        return [SimpleNamespace(endTime="t", exitCode=self.codes.pop(0) if self.codes else 0)]

    def TerminateProcessInGuest(self, vm, auth, pid):  # noqa: N802
        self.terminated.append(pid)


def _session(ops, family="linux", status="TOOLSDEPLOYPKG_SUCCEEDED"):
    clock = [0.0]
    vm = SimpleNamespace(name="vm", guest=SimpleNamespace(
        guestOperationsReady=True, customizationInfo=SimpleNamespace(customizationStatus=status)))
    creds = guest.windows_credentials() if family == "windows" else guest.linux_credentials()
    s = guest.GuestSession(SimpleNamespace(guestOperationsManager=ops), vm, creds, poll=10,
                           clock=lambda: clock[0], sleep=lambda d: clock.__setitem__(0, clock[0] + d))
    return s, clock


SPECS = [cat.InstallSpec("nginx", "apt", ("nginx",)), cat.InstallSpec("redis", "apt", ("redis-server",))]


class TestGuestInstall:
    def test_waits_for_the_login_then_runs_everything(self):
        ops = FakeOps(ready_after=3)
        s, _ = _session(ops)
        out = guest.install(s, guest.linux_commands(SPECS, "http://d"), 1000, guest.linux_cleanup_command())
        assert out == {"status": "ok", "packages": [
            {"name": "nginx", "status": "ok", "exit_code": 0}, {"name": "redis", "status": "ok", "exit_code": 0}]}
        assert ops.validations == 4

    def test_failed_package_is_partial(self):
        ops = FakeOps(codes=[0, 0, 100, 0, 0])  # cloud-init, update, nginx=100, redis, cleanup
        s, _ = _session(ops)
        out = guest.install(s, guest.linux_commands(SPECS, "http://d"), 1000, guest.linux_cleanup_command())
        assert out["status"] == "partial"
        assert out["packages"][0] == {"name": "nginx", "status": "failed", "exit_code": 100}

    def test_never_ready_means_nothing_ran(self):
        ops = FakeOps(ready_after=10**6)
        s, _ = _session(ops)
        out = guest.install(s, guest.linux_commands(SPECS, "http://d"), 100, guest.linux_cleanup_command())
        assert out["status"] == "failed" and "not ready" in out["error"]
        assert [p["status"] for p in out["packages"]] == ["not_run", "not_run"]

    def test_windows_waits_while_sysprep_runs(self):
        ops = FakeOps()
        s, _ = _session(ops, family="windows", status="TOOLSDEPLOYPKG_RUNNING")
        with pytest.raises(guest.GuestNotReadyError, match="TOOLSDEPLOYPKG_RUNNING"):
            s.wait_ready(50)
        assert ops.validations == 0
        s, _ = _session(FakeOps(), family="windows", status="TOOLSDEPLOYPKG_FAILED")
        with pytest.raises(guest.GuestNotReadyError, match="Sysprep"):
            s.wait_ready(50)

    def test_timeout_terminates_and_skips_the_rest(self):
        ops = FakeOps(hang=True)
        s, clock = _session(ops, family="windows")
        cmds = guest.windows_commands([cat.InstallSpec("a", "choco", ("a",)), cat.InstallSpec("b", "choco", ("b",))],
                                      "http://d/")
        out = guest.install(s, cmds, 100)
        assert out["packages"] == [{"name": "a", "status": "timeout", "exit_code": None},
                                   {"name": "b", "status": "timeout", "exit_code": None}]
        assert out["status"] == "failed" and ops.terminated == [7]


# --------------------------------------------------------------------------- #
# Uplink pool and the edge firewall
# --------------------------------------------------------------------------- #


class TestUplinkPool:
    def test_parse(self):
        assert uplink_pool.parse_ip_pool("10.30.32.100-10.30.32.102") == ["10.30.32.100", "10.30.32.101",
                                                                          "10.30.32.102"]
        assert uplink_pool.parse_ip_pool("10.30.32.100-101, 10.30.32.150") == ["10.30.32.100", "10.30.32.101",
                                                                              "10.30.32.150"]
        with pytest.raises(ValueError):
            uplink_pool.parse_ip_pool("")
        with pytest.raises(ValueError):
            uplink_pool.parse_ip_pool("10.0.0.9-10.0.0.1")

    def test_allocate_skips_used(self):
        pool = uplink_pool.parse_ip_pool("10.30.32.100-102")
        assert uplink_pool.allocate_ip({"10.30.32.100"}, pool) == "10.30.32.101"
        with pytest.raises(uplink_pool.UplinkPoolExhaustedError):
            uplink_pool.allocate_ip(set(pool), pool)


class TestPickEdge:
    def test_order(self):
        vyos = {"name": "r", "os": "vyos", "role": "router"}
        fw = {"name": "f", "os": "pfsense", "role": "firewall"}
        flagged = {"name": "g", "os": "opnsense", "role": "gateway", "edge": True}
        web = {"name": "w", "os": "ubuntu-2404", "role": "server"}
        assert infra.pick_edge([web, vyos, fw]) is fw
        assert infra.pick_edge([web, vyos, fw, flagged]) is flagged
        assert infra.pick_edge([web, vyos]) is vyos
        assert infra.pick_edge([web]) is None
