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
from worker.provisioners import vsphere_roles as roles

WS22 = "windows-server-2022"
KEY = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY"
DC_PASSWORD = "Dc-Secret-Pass-1!"
LOCAL_PASSWORD = "Local-Secret-Pass-2!"


def _decode(arguments: str) -> str:
    return base64.b64decode(arguments.rsplit(" ", 1)[1]).decode("utf-16-le")


class FakeSession:
    """GuestSession's surface. ``codes`` maps a command label to the exit codes it returns, in
    turn (the last one repeats); unlisted labels exit 0."""

    def __init__(self, codes: dict | None = None, raise_on: str = "", clock=None):
        self.vm = SimpleNamespace(name="r-exch", guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning"))
        self.creds = None
        self.codes = {k: list(v) for k, v in (codes or {}).items()}
        self.raise_on = raise_on
        self.ran: list[tuple[str, str]] = []  # (label, script)
        self.waits = 0
        self._poll = 1
        self.t = 0.0
        self._clock = clock or (lambda: self.t)
        self._sleep = self.sleep

    def sleep(self, s):
        self.t += max(s, 1)

    def wait_ready(self, deadline):
        self.waits += 1

    def run(self, cmd, deadline):
        script = _decode(cmd.arguments)
        self.ran.append((cmd.label, script))
        if self.raise_on and self.raise_on == cmd.label:
            raise RuntimeError(f"guest ops refused for {DC_PASSWORD} and {KEY}")
        seq = self.codes.get(cmd.label)
        code = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else 0
        return ("ok" if code in cmd.ok_codes else "failed"), code

    def labels(self) -> list[str]:
        return [label for label, _ in self.ran]

    def file(self, name: str) -> str:
        """Reassemble a file the put commands wrote."""
        data = ""
        for label, script in self.ran:
            if label == f"put:{name}":
                data += script.split("FromBase64String('", 1)[1].split("'", 1)[0]
        return base64.b64decode(data).decode("utf-8")


def _session_for(session: FakeSession):
    def make(creds):
        session.creds = creds
        return session
    return make


def _dc(**extra) -> dict:
    return {"name": "r-dc01", "os": WS22, "roles": ["ad-ds", "dns"], "ad_domain": "corp.range.local",
            "ad_forest_root": True, "nics": [{"ip": "10.60.201.10"}],
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
    assert s.labels() == ["role-staged"]
    assert "$m.image -ne 'srv2022-exchange2019'" in s.ran[0][1]


@pytest.mark.parametrize("code, text", [(21, "names another image"), (22, "staged media folder")])
def test_a_wrong_marker_fails(code, text):
    out = vp.prepare(_session_for(FakeSession({"role-staged": [code]})), _plan(), 100)
    assert out["status"] == "failed" and text in out["detail"]


def test_staged_image_joins_the_domain_as_local_admin_then_reboots():
    s = FakeSession({"domain-join": [3010]})
    reboots = []
    assert vp.prepare(_session_for(s), _plan(), 100, lambda session, d: reboots.append(d)) is None
    assert s.labels() == ["role-staged", "domain-join"] and reboots == [100]
    assert s.creds.username == "Administrator" and s.creds.password.reveal() == LOCAL_PASSWORD
    join = s.ran[1][1]
    assert "Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses '10.60.201.10'" in join
    assert '_ldap._tcp.dc._msdcs.$domain' in join and "$domain = 'corp.range.local'" in join
    assert "PSCredential('CORP\\Administrator'" in join and "Add-Computer -DomainName $domain" in join


def test_already_a_member_needs_no_reboot():
    s = FakeSession({"domain-join": [0]})
    assert vp.prepare(_session_for(s), _plan(), 100, lambda *a: pytest.fail("no reboot")) is None


@pytest.mark.parametrize("code, text", [(30, "did not answer"), (31, "Add-Computer failed"), (1, "failed (exit 1)")])
def test_a_failed_join_fails_the_setup(code, text):
    out = vp.prepare(_session_for(FakeSession({"domain-join": [code]})), _plan(), 100)
    assert out["status"] == "failed" and text in out["detail"] and out["domain"] == "corp.range.local"


def test_a_guest_ops_error_is_scrubbed():
    out = vp.prepare(_session_for(FakeSession(raise_on="domain-join")), _plan("sharepoint"), 100)
    assert out["status"] == "failed" and "guest ops refused" in out["detail"]
    assert DC_PASSWORD not in out["detail"] and KEY not in out["detail"]


# --------------------------------------------------------------------------- #
# run: stage, start, poll
# --------------------------------------------------------------------------- #


def test_exchange_runner_prepares_ad_then_installs_the_mailbox_role():
    s = FakeSession({"role-setup-status": [3, 4, 0]})
    out = vp.run(_session_for(s), _plan(), 10_000, 10_000, poll=1)
    assert out == {"status": "ok", "detail": "Exchange 2019 set up in corp.range.local",
                   "image": "srv2022-exchange2019", "domain": "corp.range.local",
                   "organization": "Medium Enterprise Network"}
    labels = s.labels()
    assert labels[0] == "role-setup-init" and labels.index("role-setup-start") < labels.index("role-setup-status")
    assert "put:secrets.json" not in labels
    runner = s.file("runner.ps1")
    assert runner == vp.RUNNER_SCRIPT
    params = json.loads(s.file("params.json"))
    assert params["org"] == "Medium Enterprise Network" and params["netbios"] == "CORP" and params["role"] == "exchange"
    schema = runner.index("/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareSchema")
    prep_ad = runner.index("/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareAD /OrganizationName:")
    install = runner.index("/Mode:Install /Role:Mailbox /IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF")
    assert schema < prep_ad < install
    start = dict(s.ran)["role-setup-start"]
    assert "-User 'CORP\\Administrator'" in start and "-RunLevel Highest" in start
    assert "Unregister-ScheduledTask -TaskName 'TrueNorth role setup'" in runner  # its stored password goes


def test_every_guest_command_stays_under_the_windows_command_line_limit():
    p = _plan("sharepoint")
    cmds = [vp.marker_command(p.image), vp.join_command(p), vp.init_command(), *vp.stage_commands(p),
            vp.start_command(p), vp.poll_command()]
    assert len([c for c in cmds if c.label == "put:runner.ps1"]) > 1  # the runner goes in chunks
    for cmd in cmds:
        assert len(roles.POWERSHELL) + 1 + len(cmd.arguments) < 32_767, cmd.label


def test_sharepoint_runner_gets_its_secrets_in_a_file_it_deletes():
    s = FakeSession()
    p = _plan("sharepoint")
    assert vp.run(_session_for(s), p, 10_000, 10_000, poll=1)["status"] == "ok"
    secrets_doc = json.loads(s.file("secrets.json"))
    assert secrets_doc["product_key"] == KEY and secrets_doc["farm_password"] == DC_PASSWORD
    assert "secrets.json" not in json.dumps(vp.params(p)) and KEY not in s.file("params.json")
    runner = s.file("runner.ps1")
    assert "Remove-Item -Force $sf" in runner and '<PIDKEY Value="$($s.product_key)" />' in runner
    assert "Complete-Sql 'Latin1_General_CI_AS_KS_WS'" in runner
    assert "New-SPConfigurationDatabase" in runner and "-LocalServerRole SingleServerFarm" in runner
    assert "New-SPCentralAdministration" in runner
    init = dict(s.ran)["role-setup-init"]
    assert "*S-1-5-32-544:(OI)(CI)F" in init and "/inheritance:r" in init  # Administrators + SYSTEM only


def test_mecm_runner_completes_sql_extends_the_schema_then_runs_setup_script():
    s = FakeSession()
    vp.run(_session_for(s), _plan("mecm"), 10_000, 10_000, poll=1)
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
    s = FakeSession({"role-setup-init": [5], "role-setup-status": [0]})
    assert vp.run(_session_for(s), _plan(), 10_000, 10_000, poll=1)["status"] == "ok"
    assert s.labels() == ["role-setup-init", "role-setup-status"]


@pytest.mark.parametrize("code, text", [(12, "Exchange /Mode:Install failed"), (18, "ConfigMgr prerequisite"),
                                        (20, "stopped unexpectedly"), (77, "status 77")])
def test_a_runner_failure_is_reported(code, text):
    out = vp.run(_session_for(FakeSession({"role-setup-status": [3, code]})), _plan(), 10_000, 10_000, poll=1)
    assert out["status"] == "failed" and text in out["detail"]


def test_a_task_that_cannot_register_fails_without_its_password():
    out = vp.run(_session_for(FakeSession({"role-setup-start": [32]})), _plan(), 10_000, 10_000, poll=1)
    assert out["status"] == "failed" and "domain Administrator" in out["detail"]
    assert DC_PASSWORD not in out["detail"]


def test_timeout_and_budget_end_are_told_apart():
    s = FakeSession({"role-setup-status": [4]})
    out = vp.run(_session_for(s), _plan(), 300, 10_000, poll=30)  # its own timeout first
    assert out["status"] == "timeout" and "Mailbox" in out["detail"] and "carries on" in out["detail"]
    s = FakeSession({"role-setup-status": [9]})
    out = vp.run(_session_for(s), _plan("mecm"), 10_000, 300, poll=30)  # the provision budget first
    assert out["status"] == "running" and "ConfigMgr setup" in out["detail"]


def test_no_budget_left_means_not_started():
    s = FakeSession()
    s.t = 1000
    out = vp.run(_session_for(s), _plan(), 1030, 1030, poll=1)
    assert out["status"] == "skipped" and not s.ran


def test_secrets_never_reach_a_log_line_or_an_outcome(caplog):
    caplog.set_level(logging.DEBUG)
    p = _plan("sharepoint")
    outs = [vp.prepare(_session_for(FakeSession({"domain-join": [3010]})), p, 100, lambda *a: None),
            vp.run(_session_for(FakeSession({"role-setup-status": [5, 6, 0]})), p, 10_000, 10_000, poll=1),
            vp.run(_session_for(FakeSession(raise_on="role-setup-status")), p, 50, 10_000, poll=1),
            vp.prepare(_session_for(FakeSession(raise_on="role-staged")), p, 100)]
    text = caplog.text + json.dumps(outs)
    for secret in (KEY, DC_PASSWORD, LOCAL_PASSWORD, p.passphrase.reveal()):
        assert secret not in text
    assert "***" in json.dumps(outs)  # the raised message was scrubbed, not dropped


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
