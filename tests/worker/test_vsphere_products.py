"""Deploy-time setup of the Exchange, SharePoint SE and MECM role images
(worker/provisioners/vsphere_products.py).

Guest operations are faked at the GuestSession level: held here are the order of the guest
steps (staged-image marker, then the domain join and its reboot, then the detached runner),
the commands the runner carries, the outcomes (ok / failed / timeout / running / skipped),
the no-key SharePoint path, and that no secret reaches a log line or an outcome. The order
against the range's domain controller is in test_vsphere_provision.py, TestProductSetup.
Nothing here proves a product installs: that needs the role images on a real vCenter.
"""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace

import pytest
from worker.provisioners import vsphere_guest as guest
from worker.provisioners import vsphere_products as vp

WS22 = "windows-server-2022"
KEY = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY"
DC_PASSWORD = "Dc-Secret-Pass-1!"
LOCAL_PASSWORD = "Local-Secret-Pass-2!"


def _decode(arguments: str) -> str:
    return base64.b64decode(arguments.rsplit(" ", 1)[1]).decode("utf-16-le")


class FakeSession:
    """GuestSession's surface for the new design. ``codes`` maps a command label to the exit
    codes it returns in turn (the last repeats); unlisted labels exit 0. The status file reads
    as STATUS_MISSING until the detached runner is started, then from ``status`` in turn.
    Uploaded files (never command lines) are kept in ``files``."""

    def __init__(self, codes: dict | None = None, status=None, raise_on: str = "", started: bool = False,
                 clock=None):
        self.vm = SimpleNamespace(name="r-exch", guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning"))
        self.creds = None
        self.codes = {k: list(v) for k, v in (codes or {}).items()}
        self.status = list(status) if status is not None else []
        self.raise_on = raise_on
        self.started = started
        self.ran: list[tuple[str, str]] = []  # (label, script)
        self.files: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.detached: list[tuple[str, str]] = []
        self.waits = 0
        self._poll = 1
        self.t = 0.0
        self._clock = clock or (lambda: self.t)
        self._sleep = self.sleep

    def sleep(self, s):
        self.t += max(s, 1)

    def wait_ready(self, deadline):
        self.waits += 1

    def upload(self, path, data: bytes, deadline, *, overwrite=True):
        if self.raise_on == "upload":
            raise guest.GuestNotReadyError("could not transfer a file to the guest (#194)")
        self.files[path] = data
        self.uploads.append(path)

    def start_detached(self, program, arguments) -> int:
        self.detached.append((program, arguments))
        self.started = True
        return 4242

    def run(self, cmd, deadline):
        self.ran.append((cmd.label, _decode(cmd.arguments)))
        if self.raise_on and self.raise_on == cmd.label:
            raise RuntimeError(f"guest ops refused for {DC_PASSWORD} and {KEY}")
        if cmd.label == "role-setup-status":
            if not self.started:
                return "ok", vp.STATUS_MISSING
            code = (self.status.pop(0) if len(self.status) > 1 else self.status[0]) if self.status else 2
            return "ok", code
        seq = self.codes.get(cmd.label)
        code = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else 0
        return ("ok" if code in cmd.ok_codes else "failed"), code

    def labels(self) -> list[str]:
        return [label for label, _ in self.ran]

    def file(self, name: str) -> str:
        """The content uploaded to STATE_DIR\\name."""
        return self.files[f"{vp.STATE_DIR}\\{name}"].decode("utf-8")


def _session_for(session: FakeSession):
    def make(creds):
        session.creds = creds
        return session
    return make


def _dc(**extra) -> dict:
    return {"name": "r-dc01", "node_id": "dc01", "os": WS22, "roles": ["ad-ds", "dns"],
            "ad_domain": "corp.range.local", "ad_forest_root": True, "nics": [{"ip": "10.60.201.10"}],
            "_guest": guest.windows_credentials(DC_PASSWORD), **extra}


def _member(services_role: str = "exchange", **extra) -> dict:
    return {"name": f"r-{services_role}", "os": WS22, "roles": [services_role], "ad_join": "corp.range.local",
            "_range_id": "ab12cd34-0000-0000-0000-000000000000", "nics": [{"ip": "10.60.201.30"}],
            "_guest": guest.windows_credentials(LOCAL_PASSWORD), **extra}


def _plan(role: str = "exchange", env=None, **extra) -> vp.SetupPlan:
    vm = _member(role, **extra)
    return vp.plan(vm, [_dc(), vm], "Medium Enterprise Network!", "http://10.30.32.10",
                   env if env is not None else {vp.PRODUCT_KEY_ENV: KEY})


# --------------------------------------------------------------------------- #
# Which VMs, and the plan
# --------------------------------------------------------------------------- #


def test_only_the_three_staged_ws2022_images_have_a_setup():
    assert vp.setup_image(_member("exchange")) == "srv2022-exchange2019"
    assert vp.setup_role(_member("sharepoint")) == "sharepoint"
    assert vp.setup_image(_member("mecm")) == "srv2022-mecm"
    assert not vp.needs_setup(_member("exchange", role_image_missing="srv2022-exchange2019"))  # bare OS
    assert not vp.needs_setup({**_member("sharepoint"), "os": "windows-server-2019"})  # 2019 image: none
    assert not vp.needs_setup(_member("sql-server"))
    assert set(vp.SETUP_IMAGES) == {"srv2022-exchange2019", "srv2022-sharepointse", "srv2022-mecm"}


def test_the_domain_admin_is_the_forest_root_dcs_administrator():
    p = _plan()
    assert (p.domain, p.netbios, p.dc_name, p.dc_ip) == ("corp.range.local", "CORP", "r-dc01", "10.60.201.10")
    assert p.domain_user == "CORP\\Administrator" and p.domain_password.reveal() == DC_PASSWORD
    assert p.local.password.reveal() == LOCAL_PASSWORD
    assert p.org == "Medium Enterprise Network" and p.site_code == "TAB"
    assert DC_PASSWORD not in repr(p) and LOCAL_PASSWORD not in repr(p)


def test_no_dc_of_the_domain_or_no_domain_is_skipped_not_failed():
    vm = _member()
    with pytest.raises(vp.PlanError, match="no forest-root domain controller of corp.range.local") as exc:
        vp.plan(vm, [_dc(ad_domain="other.local"), vm], "r")
    assert exc.value.status == "skipped"
    with pytest.raises(vp.PlanError, match="no AD domain"):
        vp.plan(_member(ad_join=""), [_dc()], "r")
    with pytest.raises(vp.PlanError, match="not known to this build"):
        vm = _member()
        vp.plan(vm, [_dc(_guest=None), vm], "r")
    with pytest.raises(vp.PlanError, match="not a DNS name") as exc:
        vm = _member(ad_join="corp'; rm")
        vp.plan(vm, [_dc(ad_domain="corp'; rm"), vm], "r")
    assert exc.value.status == "failed"


def test_sharepoint_without_a_product_key_stays_staged():
    with pytest.raises(vp.PlanError, match=r"staged \(no product key\)") as exc:
        _plan("sharepoint", env={})
    assert exc.value.status == "skipped" and vp.PRODUCT_KEY_ENV in str(exc.value)
    with pytest.raises(vp.PlanError, match="not a 25-character product key") as exc:
        _plan("sharepoint", env={vp.PRODUCT_KEY_ENV: "not-a-key'<x/>"})
    assert "not-a-key" not in str(exc.value)  # never echoed


def test_sharepoint_with_a_key_uses_its_own_sql_and_says_so():
    vm = _member("sharepoint")
    sql = {"name": "r-sql01", "os": WS22, "roles": ["sql-server"]}
    p = vp.plan(vm, [_dc(), vm, sql], "r", "", {vp.PRODUCT_KEY_ENV: KEY.lower()})
    assert p.product_key.reveal() == KEY and p.passphrase is not None
    assert any("r-sql01" in n and "own SQL Server" in n for n in p.notes)
    assert vp.secret_doc(p) == {"product_key": KEY, "passphrase": p.passphrase.reveal(),
                                "farm_password": DC_PASSWORD}
    assert vp.secret_doc(_plan("exchange")) == {}  # Exchange and MECM run with no secret file


def test_names_derived_from_the_range():
    assert vp.org_name("Red vs Blue: ACME (2026)") == "Red vs Blue ACME 2026"
    assert vp.org_name("!!!") == "TrueNorth"
    assert len(vp.org_name("x" * 100)) == 64
    assert vp.site_code("0f9e...") == "T0F" and vp.site_code("") == "T00"
    assert _plan("mecm").prereq_url == "http://10.30.32.10:8081/repository/installers/roles/mecm/configmgr-prereqs.zip"


# --------------------------------------------------------------------------- #
# prepare: marker, then domain join
# --------------------------------------------------------------------------- #


def test_a_clone_without_the_marker_is_skipped_and_never_joined():
    s = FakeSession({"role-staged": [20]})
    out = vp.prepare(_session_for(s), _plan(), 100, lambda *a: pytest.fail("no reboot"))
    assert out["status"] == "skipped" and "role-image.json is missing" in out["detail"]
    assert s.labels() == ["role-staged"] and not s.uploads  # never joined, no secret delivered
    assert "$m.image -ne 'srv2022-exchange2019'" in s.ran[0][1]


@pytest.mark.parametrize("code, text", [(21, "names another image"), (22, "staged media folder")])
def test_a_wrong_marker_fails(code, text):
    out = vp.prepare(_session_for(FakeSession({"role-staged": [code]})), _plan(), 100)
    assert out["status"] == "failed" and text in out["detail"]


def test_staged_image_joins_the_domain_as_local_admin_reading_the_password_from_a_file():
    s = FakeSession({"domain-join": [3010]})
    reboots = []
    assert vp.prepare(_session_for(s), _plan(), 100, lambda session, d: reboots.append(d)) is None
    assert s.labels() == ["role-staged", "role-setup-init", "domain-join", "reboot-pending"]
    assert reboots == [100]  # the join's 3010; reboot-pending returned 0 here
    assert s.creds.username == "Administrator" and s.creds.password.reveal() == LOCAL_PASSWORD
    # The domain password is delivered by file transfer, never on a command line.
    assert s.files[vp.JOIN_SECRET] == DC_PASSWORD.encode()
    join = dict(s.ran)["domain-join"]
    assert DC_PASSWORD not in join and "Get-Content -Raw 'C:\\TrueNorth\\deploy\\join.secret'" in join
    assert "Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses '10.60.201.10'" in join
    assert "PSCredential('CORP\\Administrator'" in join and "Add-Computer -DomainName $domain" in join


def test_a_pending_reboot_after_the_software_installs_is_cleared_before_setup():
    s = FakeSession({"domain-join": [0], "reboot-pending": [3010]})
    reboots = []
    assert vp.prepare(_session_for(s), _plan(), 100, lambda session, d: reboots.append(d)) is None
    assert reboots == [100]  # only the pending-reboot one (join said already a member)
    assert "Component Based Servicing\\RebootPending" in dict(s.ran)["reboot-pending"]


@pytest.mark.parametrize("code, text", [(30, "did not answer"), (31, "Add-Computer failed"),
                                        (33, "credentials file"), (1, "failed (exit 1)")])
def test_a_failed_join_fails_the_setup(code, text):
    out = vp.prepare(_session_for(FakeSession({"domain-join": [code]})), _plan(), 100)
    assert out["status"] == "failed" and text in out["detail"] and out["domain"] == "corp.range.local"


def test_a_file_transfer_that_cannot_reach_the_esxi_host_fails_clearly():
    out = vp.prepare(_session_for(FakeSession(raise_on="upload")), _plan(), 100)
    assert out["status"] == "failed" and "#194" in out["detail"]


def test_an_earlier_builds_setup_is_detected_and_not_rejoined():
    s = FakeSession({"role-setup-init": [5]})
    assert vp.prepare(_session_for(s), _plan(), 100, lambda *a: pytest.fail("no reboot")) is None
    assert s.labels() == ["role-staged", "role-setup-init"] and not s.uploads  # no join, no secret


def test_a_guest_ops_error_is_scrubbed():
    out = vp.prepare(_session_for(FakeSession(raise_on="domain-join")), _plan("sharepoint"), 100)
    assert out["status"] == "failed" and "guest ops refused" in out["detail"]
    assert DC_PASSWORD not in out["detail"] and KEY not in out["detail"]


# --------------------------------------------------------------------------- #
# run: deliver by file transfer, start detached as the domain admin, leave running
# --------------------------------------------------------------------------- #


def test_run_starts_the_setup_detached_as_the_domain_admin_and_leaves_it_running():
    s = FakeSession()
    out = vp.run(_session_for(s), _plan(), 10_000, 10_000, build_wait=0, poll=1)
    assert out["status"] == "running" and out["image"] == "srv2022-exchange2019"
    assert out["reconcile"]["role"] == "exchange" and out["reconcile"]["dc_node"] == "dc01"
    assert out["reconcile"]["netbios"] == "CORP" and out["reconcile"]["status_file"] == vp.STATUS_FILE
    # The product setup runs as the domain Administrator (guest-ops auth only).
    assert s.creds.username == "CORP\\Administrator" and s.creds.password.reveal() == DC_PASSWORD
    # Delivered by file transfer, not a command line; and started detached.
    assert set(s.uploads) == {vp.RUNNER_FILE, vp.PARAMS_FILE}
    assert s.detached and s.detached[0][0] == vp.POWERSHELL and "runner.ps1" in s.detached[0][1]
    runner = s.file("runner.ps1")
    assert runner == vp.RUNNER_SCRIPT and "Unregister-ScheduledTask" not in runner  # no scheduled task
    params = json.loads(s.file("params.json"))
    assert params["org"] == "Medium Enterprise Network" and params["netbios"] == "CORP"
    schema = runner.index("/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareSchema")
    prep_ad = runner.index("/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareAD /OrganizationName:")
    install = runner.index("/Mode:Install /Role:Mailbox /IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF")
    assert schema < prep_ad < install


def test_run_returns_ok_when_the_setup_is_already_healthy_within_the_build_wait():
    s = FakeSession(status=[0])
    out = vp.run(_session_for(s), _plan(), 10_000, 10_000, build_wait=100, poll=1)
    assert out["status"] == "ok" and out["detail"] == "Exchange 2019 set up in corp.range.local"
    assert "role-setup-cleanup" in s.labels()  # the secrets are cleaned up


def test_no_secret_reaches_any_guest_command_argument():
    s = FakeSession(status=[6, 0])
    p = _plan("sharepoint")
    vp.run(_session_for(s), p, 10_000, 10_000, build_wait=100, poll=1)
    for _label, script in s.ran:
        for secret in (DC_PASSWORD, KEY, p.passphrase.reveal()):
            assert secret not in script
    # The SharePoint secrets travel in the uploaded file only.
    secrets_doc = json.loads(s.file("secrets.json"))
    assert secrets_doc["product_key"] == KEY and secrets_doc["farm_password"] == DC_PASSWORD
    assert KEY not in s.file("params.json")


def test_init_locks_the_state_folder_and_deletes_leftover_secrets():
    init = _decode(vp.init_command().arguments)
    assert "*S-1-5-32-544:(OI)(CI)F" in init and "/inheritance:r" in init  # Administrators + SYSTEM only
    assert vp.JOIN_SECRET in init and vp.SECRETS_FILE in init  # leftovers are deleted on a fresh attempt


def test_sharepoint_runner_reads_secrets_from_a_file_it_deletes_and_masks_logs():
    s = FakeSession()
    p = _plan("sharepoint")
    vp.run(_session_for(s), p, 10_000, 10_000, build_wait=0, poll=1)
    assert vp.SECRETS_FILE in s.uploads
    runner = s.file("runner.ps1")
    assert "Remove-Item -Force $sf" in runner and '<PIDKEY Value="$($s.product_key)" />' in runner
    assert "Complete-Sql 'Latin1_General_CI_AS_KS_WS'" in runner
    assert "New-SPConfigurationDatabase" in runner and "-LocalServerRole SingleServerFarm" in runner
    assert "New-SPCentralAdministration" in runner
    assert "SharePoint Setup*.log" in runner and ".Replace($key, '*****')" in runner  # product key masked in logs


def test_mecm_runner_completes_sql_extends_the_schema_then_runs_setup_script():
    s = FakeSession()
    vp.run(_session_for(s), _plan("mecm"), 10_000, 10_000, build_wait=0, poll=1)
    runner = s.file("runner.ps1")
    order = [runner.index(x) for x in ("Complete-Sql 'SQL_Latin1_General_CP1_CI_AS'", "extadsch.exe",
                                       "New-ADObject -Name 'System Management'", "ConfigMgr.Manifest.cab",
                                       '/SCRIPT `"$ini`" /NOUSERINPUT')]
    assert order == sorted(order)
    assert "/ACTION=CompleteImage /INSTANCENAME=MSSQLSERVER /INSTANCEID=MSSQLSERVER" in runner
    assert "ActiveDirectoryAccessRule($sid, 'GenericAll', 'Allow', 'All')" in runner
    for key in ("Action=InstallPrimarySite", "ProductID=EVAL", "SiteCode=$($p.site_code)", "PrerequisiteComp=1",
                "PrerequisitePath=$($manifest.DirectoryName)", "ManagementPoint=$fqdn", "DistributionPoint=$fqdn",
                "SDKServer=$fqdn", "SQLServerName=$fqdn", "CloudConnector=0"):
        assert key in runner, key
    params = json.loads(s.file("params.json"))
    assert params["site_code"] == "TAB" and params["prereq_url"].endswith("/installers/roles/mecm/configmgr-prereqs.zip")


def test_an_earlier_start_is_never_repeated():
    s = FakeSession(status=[0], started=True)  # a status file already there: the runner is up
    out = vp.run(_session_for(s), _plan(), 10_000, 10_000, build_wait=100, poll=1)
    assert out["status"] == "ok"
    assert not s.uploads and not s.detached  # nothing re-staged, nothing re-started


@pytest.mark.parametrize("code, text", [(12, "Exchange /Mode:Install failed"), (18, "ConfigMgr prerequisite"),
                                        (20, "stopped unexpectedly"), (77, "status 77")])
def test_a_runner_failure_is_reported(code, text):
    out = vp.run(_session_for(FakeSession(status=[3, code])), _plan(), 10_000, 10_000, build_wait=100, poll=1)
    assert out["status"] == "failed" and text in out["detail"]
    # It still cleans up the secrets on a terminal state.


def test_no_budget_left_means_not_started():
    s = FakeSession()
    s.t = 1000
    out = vp.run(_session_for(s), _plan(), 1030, 1030, build_wait=0, poll=1)
    assert out["status"] == "skipped" and not s.ran and not s.detached


# --------------------------------------------------------------------------- #
# reconcile: the status file read later by worker/setup_reconcile.py
# --------------------------------------------------------------------------- #


def _info(**extra) -> dict:
    return {"role": "exchange", "domain": "corp.range.local", "netbios": "CORP", "dc_node": "dc01",
            "status_file": vp.STATUS_FILE, "since": 0.0, "timeout": 7200, **extra}


def test_reconcile_finalises_ok_and_cleans_up():
    s = FakeSession(status=[0], started=True)
    out = vp.reconcile(s, _info(), epoch=lambda: 10.0)
    assert out["status"] == "ok" and out["domain"] == "corp.range.local"
    assert "role-setup-cleanup" in s.labels()


@pytest.mark.parametrize("code, text", [(12, "Exchange /Mode:Install failed"), (77, "status 77")])
def test_reconcile_finalises_failure(code, text):
    out = vp.reconcile(FakeSession(status=[code], started=True), _info(), epoch=lambda: 10.0)
    assert out["status"] == "failed" and text in out["detail"]


def test_reconcile_returns_none_while_still_running_then_timeout():
    s = FakeSession(status=[4], started=True)
    assert vp.reconcile(s, _info(), epoch=lambda: 100.0) is None  # within the timeout
    out = vp.reconcile(FakeSession(status=[4], started=True), _info(since=0.0, timeout=50), epoch=lambda: 1000.0)
    assert out["status"] == "timeout"


def test_secrets_never_reach_a_log_line_or_an_outcome(caplog):
    caplog.set_level(logging.DEBUG)
    p = _plan("sharepoint")
    outs = [vp.prepare(_session_for(FakeSession({"domain-join": [3010]})), p, 100, lambda *a: None),
            vp.run(_session_for(FakeSession(status=[6, 0])), p, 10_000, 10_000, build_wait=100, poll=1),
            vp.run(_session_for(FakeSession(raise_on="role-staged")), p, 50, 10_000, build_wait=0, poll=1),
            vp.prepare(_session_for(FakeSession(raise_on="role-staged")), p, 100)]
    text = caplog.text + json.dumps(outs)
    for secret in (KEY, DC_PASSWORD, LOCAL_PASSWORD, p.passphrase.reveal()):
        assert secret not in text


def test_warning_names_the_product():
    out = vp.outcome(_plan(), "running", "x")
    assert vp.warning("r-exch", "exchange", out) == "VM r-exch: Exchange 2019 setup running: x"


def test_render_gives_member_servers_the_domain_to_join():
    from worker import render  # noqa: PLC0415

    def topo(**tpl):
        nodes = [{"id": "exch", "os": WS22, "services": ["exchange"]},
                 {"id": "mecm", "os": WS22, "services": ["sccm"], "ad_domain": "child.corp.local"},
                 {"id": "files", "os": WS22, "services": ["smb"]},
                 {"id": "dc01", "os": WS22, "services": ["active_directory"], "ad_forest": "corp.local"}]
        vms = render.render_topology({"name": "t", "nodes": nodes, **tpl}, "ab12cd34", lambda _: "tmpl")
        return {v["node_id"]: v for v in vms["vm_definitions"]}

    vms = topo()
    assert vms["exch"]["ad_join"] == "corp.local"  # the first forest a DC names, even listed after it
    assert vms["mecm"]["ad_join"] == "child.corp.local"  # the node's own
    assert "ad_join" not in vms["files"] and "ad_join" not in vms["dc01"]
    assert vms["dc01"]["ad_domain"] == "corp.local" and vms["dc01"]["ad_forest_root"] is True
    assert topo(ad_domain="range.test")["exch"]["ad_join"] == "range.test"  # the template's wins
