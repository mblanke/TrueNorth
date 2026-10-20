"""TrueNorth Range - deploy-time setup of the Exchange 2019, SharePoint (SE and 2019) and MECM role images.

The role images ``srv2022-exchange2019``, ``srv2022-sharepointse``, ``srv2022-sharepoint2019``
and ``srv2022-mecm`` (SETUP_IMAGES)
(infra/vsphere/packer/roles.pkr.hcl) install prerequisites and STAGE the product media
only: every product setup needs the range's domain, which a template cannot have. Packer's
``role-image.ps1`` writes ``C:\\TrueNorth\\role-image.json`` into each image (``image``,
``staged`` media folder, ``sql``). That file is the staged-image marker: a VM cloned from
one of these images gets the domain-dependent part here, at deploy, and nothing else. No
prerequisite is installed or downloaded again (MECM's setupdl files are the exception
when the image has none: below).

Order (vsphere_api.py ``_setup_products``)
------------------------------------------
After every VM's Windows Server roles (vsphere_roles.py), so the range's forest-root DC
of the VM's domain (``ad_join``, render.py) has been promoted and rebooted. A VM whose DC
did not end ``ad-ds: ok`` is ``skipped``. Per VM:

1. ``prepare``, as the local Administrator (the Sysprep password, ``_guest``): check the
   marker; make ``C:\\TrueNorth\\deploy`` readable by Administrators and SYSTEM only (and
   delete any secret a previous attempt left); deliver the domain password into it by
   VMware guest **file transfer**; set the DNS servers to the DC, wait for its
   ``_ldap._tcp.dc._msdcs`` record, ``Add-Computer`` (the join reads, then deletes, the
   password file); reboot;
2. the VM's own deploy-time software (vsphere_api ``_install_software``, through the
   range's one VSPHERE_CONCURRENCY semaphore), so no Chocolatey install races the product;
3. ``run``, through guest operations authenticated as ``<DOMAIN>\\Administrator``: if no
   status file exists yet, reboot when one is pending, deliver the runner, its parameters
   and (SharePoint) a secrets file by file transfer, and start the runner **detached**
   (``StartProgramInGuest`` returns its PID; the process outlives the call). No scheduled
   task, no stored Windows credential: the password reaches only the vSphere API auth. The
   build waits at most VSPHERE_SETUP_BUILD_WAIT, then records ``running``;
4. ``reconcile`` (worker/setup_reconcile.py, beat) reads the status number later, the same
   way, and records ok / failed / timeout (the timeout is checked first, so an unreachable
   VM still ends).

The domain Administrator is the DC's built-in Administrator after ``Install-ADDSForest``:
the range's declared ``Administrator`` account for the DC (ADR 0012 decision 12, #183), or
the per-range one the credential service generates when a range with these roles declares
none. Its password is the DC's ``_guest`` password; it is never stored in the output or
logged.

To verify on the first real build: the built-in domain Administrator (RID 500) is assumed to
get an unfiltered token under the default UAC policy, so the guest-operations process can run
the product setups; and the worker must reach each ESXi host on 443 for the file transfer
(mblanke/TrueNorth#194).

What each product runs
----------------------
* Exchange 2019: ``Setup.exe /IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF
  /PrepareSchema``, then ``/PrepareAD /OrganizationName:"<range name>"``, then
  ``/Mode:Install /Role:Mailbox``. Run as the forest root's Administrator (Schema and
  Enterprise Admins).
* SharePoint Server Subscription Edition: needs the site's product key,
  TN_SHAREPOINT_PRODUCT_KEY (installer ``vault_sharepoint_product_key``): the key for the
  SharePoint version the site's ranges use (a mismatched one fails setup, status 13).
  Templates carry none (ADR 0012 decision 3). Without it the role is
  ``skipped``, "staged (no product key)": nothing runs, the binaries stay staged. With it:
  ``setup.exe /config <config.xml with PIDKEY>``, then the PowerShell form of psconfig
  (``New-SPConfigurationDatabase -LocalServerRole SingleServerFarm``,
  ``Install-SPHelpCollection``, ``Initialize-SPResourceSecurity``, ``Install-SPService``,
  ``Install-SPFeature``, ``New-SPCentralAdministration``, ``Install-SPApplicationContent``).
  The farm's SQL Server is the VM's own instance: the image's SysPrep-prepared SQL Server
  2022, completed here. A range SQL VM is not used: TrueNorth neither builds
  srv2022-sql2022 nor joins SQL VMs to the domain. Farm account: the domain Administrator
  (a lab range).
* SharePoint Server 2019 (``srv2022-sharepoint2019``, role ``sharepoint-2019``; Large
  Enterprise): exactly as SharePoint SE, with the same site key variable. Its prerequisites
  are in the image, installed one by one from the depot (offline, deterministic; the
  documented alternative is prerequisiteinstaller.exe with local-file switches). Microsoft
  Learn sharepoint/install/hardware-and-software-requirements-2019 lists Windows Server 2022 and
  SQL Server 2022 as supported.
* MECM current branch: SQL Server ``/ACTION=CompleteImage`` (collation
  SQL_Latin1_General_CP1_CI_AS), ``extadsch.exe`` (success read from C:\\ExtADSch.log), the
  System Management container with Full Control for the site server's computer account on
  it and all descendants, then ``setup.exe /SCRIPT <ini> /NOUSERINPUT``: a primary site,
  ``ProductID=EVAL``, site code ``T`` plus two hex digits of the range id, SDK provider,
  management point and distribution point on the server, no service connection point (the
  range is offline). ``PrerequisitePath`` is setupdl.exe output: the image's
  ``C:\\TrueNorth\\media\\mecm-prereqs`` (role-image.ps1 stages it when the depot has
  ``configmgr-prereqs.zip``), else downloaded at deploy from
  ``<TN_DEPOT_URL>:8081/repository/installers/roles/mecm/configmgr-prereqs.zip``.

Sources (Microsoft Learn):
- learn.microsoft.com/exchange/plan-and-deploy/prepare-ad-and-domains?view=exchserver-2019
- learn.microsoft.com/exchange/plan-and-deploy/deploy-new-installations/unattended-installs?view=exchserver-2019
- learn.microsoft.com/sharepoint/install/configxml-reference
- learn.microsoft.com/powershell/module/sharepoint-server/new-spconfigurationdatabase
- learn.microsoft.com/sql/database-engine/install-windows/install-sql-server-using-sysprep
- learn.microsoft.com/sql/database-engine/install-windows/install-sql-server-from-the-command-prompt
- learn.microsoft.com/mem/configmgr/core/servers/deploy/install/command-line-options-for-setup
- learn.microsoft.com/mem/configmgr/core/servers/deploy/install/use-a-command-line-to-install-sites
- learn.microsoft.com/mem/configmgr/core/servers/deploy/install/setup-downloader
- learn.microsoft.com/mem/configmgr/core/plan-design/network/extend-the-active-directory-schema
- learn.microsoft.com/powershell/module/microsoft.powershell.management/add-computer
- developer.broadcom.com (vSphere Web Services API): GuestFileManager.InitiateFileTransferToGuest,
  GuestProcessManager.StartProgramInGuest

Outcomes
--------
``vms[].role_setup`` = {status, detail, image, domain, ...}; the role's entry in
``vms[].roles`` carries the same status. ok | failed | timeout | running | skipped. Like the
Security Onion setup (#184) every outcome but ``ok`` is a provisioner warning, never a
partial range. ``accounts_applied``: the VM's Administrator is verified as for any Windows VM.

Secrets
-------
No secret is ever on a guest command line: the commands' ``-EncodedCommand`` scripts carry
file paths only. The domain password (for the join) and the SharePoint key, passphrase and
farm password reach the guest only by file transfer into the Administrators/SYSTEM-only
folder; the join and the runner read them and delete them in a ``finally``, a failed start
runs the clean-up command, a fresh attempt (INIT) and every terminal reconcile delete any
leftover. Nothing secret is logged, and every recorded error is scrubbed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import windows_roles
from . import vsphere_guest as guest
from . import vsphere_infra as infra
from . import vsphere_roles

logger = logging.getLogger(__name__)

# The role images with a deploy-time setup here, and their role.
SETUP_IMAGES: dict[str, str] = {
    "srv2022-exchange2019": "exchange",
    "srv2022-sharepointse": "sharepoint",
    "srv2022-sharepoint2019": "sharepoint-2019",
    "srv2022-mecm": "mecm",
}
LABELS = {"exchange": "Exchange 2019", "sharepoint": "SharePoint SE", "sharepoint-2019": "SharePoint Server 2019",
          "mecm": "MECM"}
# Both SharePoint products take the site's product key (the one TN_SHAREPOINT_PRODUCT_KEY /
# vault_sharepoint_product_key: a site uses one SharePoint product per key; see the runbook)
# and set up the same way: SQL CompleteImage, setup.exe /config, a single-server farm.
SHAREPOINT_ROLES = frozenset({"sharepoint", "sharepoint-2019"})

# Seconds from the start of a VM's setup (its domain join) until the setup is called a
# ``timeout``. The build never waits this long: the reconcile task (worker/setup_reconcile.py)
# reads the status file later. VSPHERE_SETUP_BUILD_WAIT is how long the build itself waits
# after starting the setup (0: record ``running`` at once and leave it to reconcile).
VSPHERE_ROLE_SETUP_TIMEOUT: int = int(os.environ.get("VSPHERE_ROLE_SETUP_TIMEOUT", "7200"))
VSPHERE_SETUP_BUILD_WAIT: float = float(os.environ.get("VSPHERE_SETUP_BUILD_WAIT", "0"))
# Seconds between status reads (each one a short guest-operations process).
VSPHERE_ROLE_SETUP_POLL: float = float(os.environ.get("VSPHERE_ROLE_SETUP_POLL", "30"))
PRODUCT_KEY_ENV = "TN_SHAREPOINT_PRODUCT_KEY"
MIN_START = 60  # seconds of budget starting the setup needs; less, and it is not started
DC_WAIT = 900  # seconds the join waits for the DC's SRV record (it may still be booting)

ROOT = "C:\\TrueNorth"
MARKER = ROOT + "\\role-image.json"
STATE_DIR = ROOT + "\\deploy"
STATUS_FILE = STATE_DIR + "\\status"
RUNNER_FILE = STATE_DIR + "\\runner.ps1"
RUNNER_LOG = STATE_DIR + "\\runner.log"
PARAMS_FILE = STATE_DIR + "\\params.json"
SECRETS_FILE = STATE_DIR + "\\secrets.json"  # SharePoint key/passphrase; deleted by the runner
JOIN_SECRET = STATE_DIR + "\\join.secret"  # the domain password for Add-Computer; deleted by the join
MECM_PREREQS = ROOT + "\\media\\mecm-prereqs"
POWERSHELL = vsphere_roles.POWERSHELL
_KEY = re.compile(r"^[A-Z0-9]{5}(-[A-Z0-9]{5}){4}$")
_DOMAIN = re.compile(r"^(?=.{3,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

# The runner's status numbers (C:\TrueNorth\deploy\status).
STATUS = {
    0: "set up",
    2: "setup is starting",
    3: "Exchange: preparing the AD schema and the organization",
    4: "Exchange: installing the Mailbox role",
    5: "SharePoint: installing the binaries",
    6: "SharePoint: creating the farm",
    7: "MECM: completing SQL Server",
    8: "MECM: extending the AD schema, System Management container",
    9: "MECM: ConfigMgr setup",
    10: "the staged product media are not where role-image.json says",
    11: f"Exchange /PrepareSchema or /PrepareAD failed; see {RUNNER_LOG} and C:\\ExchangeSetupLogs",
    12: f"Exchange /Mode:Install failed; see {RUNNER_LOG} and C:\\ExchangeSetupLogs",
    13: f"SharePoint setup.exe failed; see {RUNNER_LOG} and the SharePoint Setup log in {STATE_DIR}",
    14: f"SharePoint farm creation failed; see {RUNNER_LOG}",
    15: f"SQL Server CompleteImage failed, or the image has no prepared SQL Server; see {RUNNER_LOG}",
    16: f"extadsch.exe failed; see C:\\ExtADSch.log and {RUNNER_LOG}",
    17: f"could not create the System Management container or grant the site server rights; see {RUNNER_LOG}",
    18: f"no ConfigMgr prerequisite files (setupdl.exe output): not in {MECM_PREREQS} and not downloadable from "
        "the depot; see the runbook",
    19: f"ConfigMgr setup failed; see C:\\ConfigMgrSetup.log and {RUNNER_LOG}",
    20: f"the setup runner stopped unexpectedly; see {RUNNER_LOG}",
}
RUNNING = frozenset(range(2, 10))
MARKER_CODES = {
    20: f"not a staged role image: {MARKER} is missing; nothing was installed",
    21: f"{MARKER} names another image",
    22: f"the staged media folder {MARKER} names is missing",
}
JOIN_CODES = {
    30: f"the domain controller's DNS did not answer for the domain within {DC_WAIT}s",
    31: "Add-Computer failed (credentials, or the domain controller unreachable)",
    33: f"the domain credentials file {JOIN_SECRET} was not delivered to the guest",
}


# --------------------------------------------------------------------------- #
# Which VMs
# --------------------------------------------------------------------------- #


def setup_image(vm_def: dict) -> str | None:
    """The VM's role image when it has a deploy-time setup here (None for a bare OS)."""
    if vm_def.get("role_image_missing") or infra.os_family(vm_def) != "windows":
        return None
    image = windows_roles.role_image(list(vm_def.get("roles") or []), str(vm_def.get("os") or ""))
    return image if image in SETUP_IMAGES else None


def setup_role(vm_def: dict) -> str | None:
    image = setup_image(vm_def)
    return SETUP_IMAGES[image] if image else None


def needs_setup(vm_def: dict) -> bool:
    return setup_image(vm_def) is not None


def not_built(vm_def: dict) -> tuple[str, str] | None:
    """(role, image) when the VM's node asked for one of these role images but the VM is
    the plain OS instead: the image is not ``built`` in the golden-image registry (render.py)
    or its template is not on the vCenter (vsphere_api ``_use_role_fallback``)."""
    image = str(vm_def.get("role_image_missing") or "")
    if image in SETUP_IMAGES and infra.os_family(vm_def) == "windows":
        return SETUP_IMAGES[image], image
    return None


def not_built_outcome(vm_def: dict) -> dict:
    """``role_setup`` of such a VM: skipped, nothing installed."""
    role, image = not_built(vm_def) or ("", "")
    using = str(vm_def.get("template_name") or "the plain OS")
    return {"status": "skipped", "detail": f"role image {image} not built — using {using}; {role} not installed",
            "image": image}


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


class PlanError(ValueError):
    """The VM's setup cannot run; ``status`` is ``skipped`` or ``failed``. Never holds a secret."""

    def __init__(self, message: str, status: str = "skipped"):
        super().__init__(message)
        self.status = status


@dataclass
class SetupPlan:
    name: str
    role: str
    image: str
    domain: str
    netbios: str
    dc_name: str
    dc_node: str
    dc_ip: str
    local: guest.GuestCredentials = field(repr=False)
    domain_password: guest.Secret = field(repr=False)
    org: str = ""
    site_code: str = ""
    product_key: guest.Secret | None = field(default=None, repr=False)
    passphrase: guest.Secret | None = field(default=None, repr=False)
    prereq_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def domain_user(self) -> str:
        return f"{self.netbios}\\Administrator"

    @property
    def domain_login(self) -> guest.GuestCredentials:
        """Guest-operations login as the domain Administrator: the product setup runs as this
        user. The password reaches only the vSphere API auth, never a guest command line."""
        return guest.GuestCredentials("windows", self.domain_user, self.domain_password)

    @property
    def hidden(self) -> tuple:
        return (self.local.password, self.domain_password, self.product_key, self.passphrase)


def org_name(range_name: str) -> str:
    """Exchange organization name: letters, digits, spaces and hyphens, at most 64."""
    clean = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 -]", "", str(range_name or ""))).strip(" -")
    return clean[:64].strip(" -") or "TrueNorth"


def site_code(range_id: str) -> str:
    """A ConfigMgr site code: three characters, letters and digits, none of the reserved names."""
    hexes = "".join(c for c in str(range_id or "").upper() if c in "0123456789ABCDEF")
    return "T" + (hexes + "00")[:2]


def product_key(env: dict | None = None) -> guest.Secret | None:
    env = os.environ if env is None else env
    value = str(env.get(PRODUCT_KEY_ENV) or "").strip().upper()
    return guest.Secret(value) if value else None


def find_dc(vm_def: dict, vm_defs: list[dict]) -> dict:
    domain = str(vm_def.get("ad_join") or "")
    if not domain:
        raise PlanError("no AD domain to join (the range has no domain controller)")
    if not _DOMAIN.match(domain):
        raise PlanError(f"AD domain {domain!r} is not a DNS name", "failed")
    for dc in vm_defs:
        if "ad-ds" in (dc.get("roles") or []) and dc.get("ad_forest_root") and str(dc.get("ad_domain")) == domain:
            return dc
    raise PlanError(f"no forest-root domain controller of {domain} in the range")


def _first_ip(vm_def: dict) -> str:
    return next((str(n["ip"]) for n in vm_def.get("nics") or [] if n.get("ip")), str(vm_def.get("ip") or ""))


def plan(vm_def: dict, vm_defs: list[dict], range_name: str, depot_url: str = "",
         env: dict | None = None) -> SetupPlan:
    """What this VM's setup needs, checked before anything runs (PlanError)."""
    image = setup_image(vm_def)
    if image is None:
        raise PlanError("no staged role image with a deploy-time setup")
    role = SETUP_IMAGES[image]
    key = None
    notes: list[str] = []
    if role in SHAREPOINT_ROLES:
        key = product_key(env)
        if key is None:
            raise PlanError(f"staged (no product key): set {PRODUCT_KEY_ENV} (installer vault_sharepoint_product_key) "
                            f"to install {LABELS[role]}; the binaries stay staged in the image")
        if not _KEY.match(key.reveal()):
            raise PlanError(f"staged (no product key): {PRODUCT_KEY_ENV} is not a 25-character product key "
                            "(XXXXX-XXXXX-XXXXX-XXXXX-XXXXX); the binaries stay staged in the image")
        sql = [v["name"] for v in vm_defs if "sql-server" in (v.get("roles") or [])]
        if sql:
            notes.append(f"SharePoint uses its own SQL Server instance, not {', '.join(sql)} (range SQL VMs are "
                         "not domain members)")
    dc = find_dc(vm_def, vm_defs)
    dc_ip = _first_ip(dc)
    if not dc_ip:
        raise PlanError(f"domain controller {dc['name']} has no static address to use as DNS")
    local = vm_def.get("_guest")
    if local is None:
        raise PlanError("no guest login for this VM")
    dc_login = dc.get("_guest")
    if dc_login is None:
        raise PlanError(f"the Administrator password of domain controller {dc['name']} is not known to this build")
    domain = str(vm_def["ad_join"])
    prereq = f"{depot_url}:8081/repository/installers/roles/mecm/configmgr-prereqs.zip" if depot_url else ""
    return SetupPlan(
        name=str(vm_def.get("name") or ""), role=role, image=image, domain=domain,
        netbios=vsphere_roles.netbios(domain), dc_name=str(dc["name"]),
        dc_node=str(dc.get("node_id") or dc.get("name") or ""), dc_ip=dc_ip, local=local,
        domain_password=dc_login.password, org=org_name(range_name), site_code=site_code(vm_def.get("_range_id", "")),
        product_key=key, passphrase=guest.Secret(secrets.token_urlsafe(18) + "aA1!") if role in SHAREPOINT_ROLES else None,
        prereq_url=prereq if role == "mecm" else "", notes=notes)


# --------------------------------------------------------------------------- #
# Guest scripts (PowerShell, as -EncodedCommand)
# --------------------------------------------------------------------------- #


def _fill(script: str, **values: str) -> str:
    for k, v in values.items():
        script = script.replace(f"__{k.upper()}__", v)
    return script


def _q(value: str) -> str:
    return vsphere_roles.ps_quote(str(value))


MARKER_SCRIPT = """$ErrorActionPreference = 'Stop'
if (-not (Test-Path '__MARKER__')) { exit 20 }
$m = Get-Content -Raw '__MARKER__' | ConvertFrom-Json
if ($m.image -ne '__IMAGE__') { exit 21 }
if (-not $m.staged -or -not (Test-Path ([string]$m.staged))) { exit 22 }
exit 0
"""

# The domain password is read from the delivered file (JOIN_SECRET), never a command line.
JOIN_SCRIPT = """$ErrorActionPreference = 'Stop'
$domain = '__DOMAIN__'
$cs = Get-CimInstance Win32_ComputerSystem
if ($cs.PartOfDomain -and $cs.Domain -ieq $domain) { Remove-Item -Force '__SECRET__' -ErrorAction SilentlyContinue; exit 0 }
if (-not (Test-Path '__SECRET__')) { exit 33 }
try {
  Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object {
    Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses '__DC_IP__' }
  $until = (Get-Date).AddSeconds(__WAIT__)
  while ($true) {
    try { Resolve-DnsName -Type SRV "_ldap._tcp.dc._msdcs.$domain" -Server '__DC_IP__' -DnsOnly -ErrorAction Stop | Out-Null; break }
    catch { if ((Get-Date) -gt $until) { exit 30 }; Start-Sleep -Seconds 15 }
  }
  $pw = (Get-Content -Raw '__SECRET__').Trim()
  $cred = New-Object System.Management.Automation.PSCredential('__USER__', (ConvertTo-SecureString $pw -AsPlainText -Force))
  $pw = $null
  $tries = 0
  while ($true) {
    try { Add-Computer -DomainName $domain -Credential $cred -Force -ErrorAction Stop; break }
    catch { $tries++; if ($tries -ge 10) { exit 31 }; Start-Sleep -Seconds 30 }
  }
} finally { Remove-Item -Force '__SECRET__' -ErrorAction SilentlyContinue }
exit 3010
"""

# Create the state folder, readable only by Administrators and SYSTEM, and delete any
# secret a previous attempt may have left (a crash, a reboot, a terminated guest-ops call).
INIT_SCRIPT = """$ErrorActionPreference = 'Stop'
$dir = '__DIR__'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
& icacls.exe $dir /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { exit 34 }  # never write a secret into a folder that is not locked down
Remove-Item -Force -ErrorAction SilentlyContinue '__SECRET__', '__JOINSECRET__', (Join-Path $dir 'config.xml')
if (Test-Path '__STATUS__') { exit 5 }
exit 0
"""

# Delete every secret the setup may have left: the reconcile task runs it on a terminal
# state, and a fresh attempt runs INIT_SCRIPT, so nothing lingers past the setup.
CLEANUP_SCRIPT = """Remove-Item -Force -ErrorAction SilentlyContinue '__SECRET__', '__JOINSECRET__'
exit 0
"""

# A reboot pending after the Chocolatey software installs would make the product setup fail;
# reboot first. Checks the keys Microsoft documents for Component-Based Servicing, Windows
# Update and a pending file-rename or computer-rename.
REBOOT_PENDING_SCRIPT = r"""$ErrorActionPreference = 'SilentlyContinue'
$pending = $false
if (Get-Item 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pending = $true }
if (Get-Item 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pending = $true }
$pfro = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue).PendingFileRenameOperations
if ($pfro) { $pending = $true }
$active = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName').ComputerName
$want = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName').ComputerName
if ($active -ne $want) { $pending = $true }
if ($pending) { exit 3010 }
exit 0
"""

# Exit codes: the status number; 200 (STATUS_MISSING) when there is no status file yet, so
# nothing was started; 201 (STATUS_UNREADABLE) when it exists but reading it is denied (the
# login's token cannot read the Administrators-only folder: likely a UAC-filtered token);
# 2 when it exists but is not a number yet or is being written (a sharing violation).
POLL_SCRIPT = """if (-not (Test-Path -LiteralPath '__STATUS__')) { exit 200 }
try { $c = Get-Content -Raw -LiteralPath '__STATUS__' -ErrorAction Stop }
catch [System.UnauthorizedAccessException] { exit 201 }
catch { exit 2 }
if ($c -match '^\\s*(\\d+)') { exit [int]$Matches[1] }
exit 2
"""

RUNNER_SCRIPT = r"""# TrueNorth Range: deploy-time setup of a staged role image.
# Written by the TrueNorth worker (worker/provisioners/vsphere_products.py); started detached
# through VMware guest operations as the range's domain Administrator (no scheduled task, no
# stored credential: the password reaches only the vSphere API auth of the guest-ops call).
# Progress for the worker: a number in C:\TrueNorth\deploy\status (vsphere_products.STATUS).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$dir = 'C:\TrueNorth\deploy'
$log = Join-Path $dir 'runner.log'
function Log([string]$m) { Add-Content -Path $log -Value ('{0:s} {1}' -f (Get-Date), $m) }
function Status([int]$n) { Set-Content -Path (Join-Path $dir 'status') -Value $n -Encoding ascii }
function Finish([int]$n) {
  Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $dir 'secrets.json'), (Join-Path $dir 'config.xml'), (Join-Path $dir 'join.secret')
  Status $n
  exit $n
}
function Fail([int]$n, [string]$m) { Log "FAILED ($n): $m"; Finish $n }
Status 2
# Arguments are logged: never pass a secret on a command line here.
function Run([string]$file, [string]$arguments, [int[]]$ok, [int]$code, [string]$what) {
  Log "${what}: $file $arguments"
  if ($arguments) { $proc = Start-Process -FilePath $file -ArgumentList $arguments -Wait -PassThru -NoNewWindow }
  else { $proc = Start-Process -FilePath $file -Wait -PassThru -NoNewWindow }
  Log "${what}: exit $($proc.ExitCode)"
  if ($proc.ExitCode -notin $ok) { Fail $code "$what exited $($proc.ExitCode)" }
}
trap { Log "runner stopped: $($_.Exception.Message)"; Finish 20 }

$p = Get-Content -Raw (Join-Path $dir 'params.json') | ConvertFrom-Json
$s = $null
$sf = Join-Path $dir 'secrets.json'
if (Test-Path $sf) { $s = Get-Content -Raw $sf | ConvertFrom-Json; Remove-Item -Force $sf }
$m = Get-Content -Raw 'C:\TrueNorth\role-image.json' | ConvertFrom-Json
$staged = [string]$m.staged
$fqdn = ("$env:COMPUTERNAME.$($p.domain)").ToLower()
Log "start: $($p.role) on $fqdn"

# SQL Server 2022 prepared by role-image.ps1 (/ACTION=PrepareImage), completed here.
function Complete-Sql([string]$collation) {
  if (Get-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue) { Log 'SQL Server: already completed'; return }
  $boot = Get-ChildItem -Path 'C:\Program Files\Microsoft SQL Server\*\Setup Bootstrap\SQL*\setup.exe' `
    -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
  if (-not $boot) { Fail 15 'no prepared SQL Server in the image (Setup Bootstrap\SQL*\setup.exe)' }
  Run $boot.FullName ("/Q /ACTION=CompleteImage /INSTANCENAME=MSSQLSERVER /INSTANCEID=MSSQLSERVER " +
    "/SQLSVCACCOUNT=`"NT Service\MSSQLSERVER`" /SQLSYSADMINACCOUNTS=`"$($p.netbios)\Domain Admins`" " +
    "`"BUILTIN\Administrators`" /SQLCOLLATION=$collation /TCPENABLED=1 " +
    "/IACCEPTSQLSERVERLICENSETERMS /SUPPRESSPRIVACYSTATEMENTNOTICE") @(0, 3010) 15 'SQL Server CompleteImage'
  Start-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue
}

switch ($p.role) {
  'exchange' {
    $setup = Join-Path $staged 'Setup.exe'
    if (-not (Test-Path $setup)) { Fail 10 "Exchange Setup.exe is not in $staged" }
    if (Get-Service -Name MSExchangeIS -ErrorAction SilentlyContinue) { Log 'Exchange: already installed' }
    else {
      Status 3
      Run $setup '/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareSchema' @(0) 11 'Exchange PrepareSchema'
      Run $setup ("/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareAD /OrganizationName:`"" +
        $p.org + "`"") @(0) 11 'Exchange PrepareAD'
      Status 4
      Run $setup '/Mode:Install /Role:Mailbox /IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF' @(0) 12 `
        'Exchange Mailbox install'
    }
  }
  { $_ -in @('sharepoint', 'sharepoint-2019') } {
    if (-not $s -or -not $s.product_key) { Fail 13 'no product key reached the runner' }
    if (-not (Test-Path (Join-Path $staged 'setup.exe'))) { Fail 10 "SharePoint setup.exe is not in $staged" }
    Complete-Sql 'Latin1_General_CI_AS_KS_WS'
    $psconfig = 'C:\Program Files\Common Files\microsoft shared\Web Server Extensions\16\BIN\psconfig.exe'
    if (-not (Test-Path $psconfig)) {
      Status 5
      $cfg = Join-Path $dir 'config.xml'
      Set-Content -Path $cfg -Encoding UTF8 -Value @"
<Configuration>
  <Package Id="sts"><Setting Id="LAUNCHEDFROMSETUPSTS" Value="Yes" /></Package>
  <Package Id="spswfe"><Setting Id="SETUPCALLED" Value="1" /></Package>
  <Logging Type="verbose" Path="$dir" Template="SharePoint Setup(*).log" />
  <PIDKEY Value="$($s.product_key)" />
  <Display Level="none" CompletionNotice="no" AcceptEula="yes" />
  <Setting Id="USINGUIINSTALLMODE" Value="0" />
  <Setting Id="SETUP_REBOOT" Value="Never" />
  <Setting Id="SETUPTYPE" Value="CLEAN_INSTALL" />
</Configuration>
"@
      Run (Join-Path $staged 'setup.exe') "/config `"$cfg`"" @(0, 3010) 13 'SharePoint setup'
      Remove-Item -Force $cfg
    }
    # Mask the product key from any setup log that may have recorded it (config.xml is gone;
    # SharePoint's own logs are not documented to hold PIDKEY, so scrub to be sure).
    $key = $s.product_key
    foreach ($d in @($dir, $env:TEMP)) {
      Get-ChildItem -Path $d -Filter 'SharePoint Setup*.log' -ErrorAction SilentlyContinue | ForEach-Object {
        (Get-Content -Raw $_.FullName).Replace($key, '*****') | Set-Content -Encoding UTF8 $_.FullName }
    }
    $key = $null
    Status 6
    try {
      Add-PSSnapin Microsoft.SharePoint.PowerShell -ErrorAction SilentlyContinue
      if (-not (Get-Command New-SPConfigurationDatabase -ErrorAction SilentlyContinue)) { Import-Module SharePointServer }
      $farm = $true
      try { $null = Get-SPFarm -ErrorAction Stop } catch { $farm = $false }
      if ($farm) { Log 'SharePoint: farm already exists' }
      else {
        $cred = New-Object System.Management.Automation.PSCredential("$($p.netbios)\Administrator",
          (ConvertTo-SecureString $s.farm_password -AsPlainText -Force))
        Log "SharePoint: New-SPConfigurationDatabase on $env:COMPUTERNAME"
        New-SPConfigurationDatabase -DatabaseName 'TN_SharePoint_Config' -DatabaseServer $env:COMPUTERNAME `
          -AdministrationContentDatabaseName 'TN_SharePoint_AdminContent' -FarmCredentials $cred `
          -Passphrase (ConvertTo-SecureString $s.passphrase -AsPlainText -Force) -LocalServerRole SingleServerFarm
        Install-SPHelpCollection -All
        Initialize-SPResourceSecurity
        Install-SPService
        Install-SPFeature -AllExistingFeatures -Force | Out-Null
        New-SPCentralAdministration -Port 9999 -WindowsAuthProvider NTLM
        Install-SPApplicationContent
      }
    } catch { Fail 14 "SharePoint farm: $($_.Exception.Message)" }
  }
  'mecm' {
    $bin = Join-Path $staged 'SMSSETUP\BIN\X64'
    if (-not (Test-Path (Join-Path $bin 'setup.exe'))) { Fail 10 "ConfigMgr setup.exe is not in $bin" }
    Status 7
    Complete-Sql 'SQL_Latin1_General_CP1_CI_AS'
    Status 8
    Run (Join-Path $bin 'extadsch.exe') '' @(0) 16 'extadsch'
    if (-not (Select-String -Path 'C:\ExtADSch.log' -Pattern 'Successfully extended the Active Directory schema' `
        -Quiet -ErrorAction SilentlyContinue)) { Fail 16 'C:\ExtADSch.log does not report success' }
    try {
      Install-WindowsFeature RSAT-AD-PowerShell | Out-Null
      Import-Module ActiveDirectory
      $system = "CN=System,$((Get-ADDomain).DistinguishedName)"
      $container = "CN=System Management,$system"
      if (-not (Get-ADObject -Filter "distinguishedName -eq '$container'")) {
        New-ADObject -Name 'System Management' -Type container -Path $system }
      $sid = (Get-ADComputer $env:COMPUTERNAME).SID
      $acl = Get-Acl "AD:$container"
      $acl.AddAccessRule((New-Object System.DirectoryServices.ActiveDirectoryAccessRule($sid, 'GenericAll', 'Allow', 'All')))
      Set-Acl -Path "AD:$container" -AclObject $acl
      Log "System Management container: Full Control for $env:COMPUTERNAME$ (this object and all descendants)"
    } catch { Fail 17 "System Management container: $($_.Exception.Message)" }
    $pre = 'C:\TrueNorth\media\mecm-prereqs'
    $manifest = Get-ChildItem -Path $pre -Recurse -Filter 'ConfigMgr.Manifest.cab' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $manifest -and $p.prereq_url) {
      try {
        Log "ConfigMgr prerequisites: $($p.prereq_url)"
        $zip = Join-Path $dir 'configmgr-prereqs.zip'
        Invoke-WebRequest -Uri $p.prereq_url -OutFile $zip -UseBasicParsing -TimeoutSec 3600
        Expand-Archive -Path $zip -DestinationPath $pre -Force
        Remove-Item -Force $zip
      } catch { Log "ConfigMgr prerequisites: $($_.Exception.Message)" }
      $manifest = Get-ChildItem -Path $pre -Recurse -Filter 'ConfigMgr.Manifest.cab' -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if (-not $manifest) { Fail 18 "no ConfigMgr.Manifest.cab under $pre" }
    Status 9
    $ini = Join-Path $dir 'configmgr.ini'
    Set-Content -Path $ini -Encoding ascii -Value @"
[Identification]
Action=InstallPrimarySite

[Options]
ProductID=EVAL
SiteCode=$($p.site_code)
SiteName=$($p.site_name)
SMSInstallDir=C:\Program Files\Microsoft Configuration Manager
SDKServer=$fqdn
PrerequisiteComp=1
PrerequisitePath=$($manifest.DirectoryName)
AdminConsole=1
JoinCEIP=0
MobileDeviceLanguage=0
RoleCommunicationProtocol=HTTPorHTTPS
ClientsUsePKICertificate=0
ManagementPoint=$fqdn
ManagementPointProtocol=HTTP
DistributionPoint=$fqdn
DistributionPointProtocol=HTTP
DistributionPointInstallIIS=0

[SQLConfigOptions]
SQLServerName=$fqdn
DatabaseName=CM_$($p.site_code)
SQLSSBPort=4022

[CloudConnectorOptions]
CloudConnector=0
CloudConnectorServer=$fqdn
UseProxy=0

[SABranchOptions]
SAActive=0
CurrentBranch=1
"@
    Run (Join-Path $bin 'setup.exe') "/SCRIPT `"$ini`" /NOUSERINPUT" @(0) 19 'ConfigMgr setup'
  }
  default { Fail 20 "unknown role $($p.role)" }
}
Log 'done'
Finish 0
"""


STATUS_MISSING = 200  # the status file is not there yet (the runner has not written it)
STATUS_UNREADABLE = 201  # it is there but reading it is denied (see POLL_SCRIPT)
# Every exit code the status read can legitimately give; anything else (1, an NTSTATUS such
# as 0xC000013A, a guest-ops error) is "unknown": retried, never a result.
KNOWN_STATUS = frozenset({*STATUS, STATUS_MISSING, STATUS_UNREADABLE})
UNREADABLE_DETAIL = (f"the status file {STATUS_FILE} exists but the domain Administrator cannot read it (access "
                     "denied): most likely a UAC-filtered token for guest operations (runbook 7.3, verify on the "
                     "first real build)")
JOIN_CODES[34] = f"could not lock {STATE_DIR} down to Administrators and SYSTEM (icacls); nothing was delivered"


def command(label: str, script: str, ok_codes) -> guest.GuestCommand:
    return vsphere_roles.powershell(label, script, frozenset(ok_codes))


def marker_command(image: str) -> guest.GuestCommand:
    return command("role-staged", _fill(MARKER_SCRIPT, marker=MARKER, image=image), {0, *MARKER_CODES})


def init_command() -> guest.GuestCommand:
    return command("role-setup-init", _fill(INIT_SCRIPT, dir=STATE_DIR, status=STATUS_FILE,
                                            secret=SECRETS_FILE, joinsecret=JOIN_SECRET), {0, 5})


def join_command(p: SetupPlan) -> guest.GuestCommand:
    """Join the domain as the VM's local Administrator, reading the domain password from the
    delivered file (JOIN_SECRET). No secret is on the command line."""
    script = _fill(JOIN_SCRIPT, domain=_q(p.domain), dc_ip=_q(p.dc_ip), wait=str(DC_WAIT),
                   user=_q(p.domain_user), secret=JOIN_SECRET)
    return command("domain-join", script, {0, vsphere_roles.EXIT_REBOOT})


def reboot_pending_command() -> guest.GuestCommand:
    return command("reboot-pending", REBOOT_PENDING_SCRIPT, {0, vsphere_roles.EXIT_REBOOT})


def poll_command() -> guest.GuestCommand:
    # The status number is the process exit code; only KNOWN_STATUS codes are a result.
    return command("role-setup-status", _fill(POLL_SCRIPT, status=STATUS_FILE), KNOWN_STATUS)


def cleanup_command() -> guest.GuestCommand:
    return command("role-setup-cleanup", _fill(CLEANUP_SCRIPT, secret=SECRETS_FILE, joinsecret=JOIN_SECRET), {0})


def _runner_args() -> str:
    """The powershell arguments that start the runner file. No secret: only the file path."""
    return f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{RUNNER_FILE}"'


def params(p: SetupPlan) -> dict:
    """What the runner reads from params.json. Nothing secret."""
    return {"role": p.role, "image": p.image, "domain": p.domain, "netbios": p.netbios, "org": p.org,
            "site_code": p.site_code, "site_name": f"TrueNorth {p.org}"[:50].strip(), "prereq_url": p.prereq_url}


def secret_doc(p: SetupPlan) -> dict:
    """What the runner reads from secrets.json and deletes: SharePoint only."""
    if p.role not in SHAREPOINT_ROLES or p.product_key is None or p.passphrase is None:
        return {}
    return {"product_key": p.product_key.reveal(), "passphrase": p.passphrase.reveal(),
            "farm_password": p.domain_password.reveal()}


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def scrub(text: str, *values) -> str:
    out = str(text)
    for v in values:
        if v is not None and v.reveal():
            out = out.replace(v.reveal(), "***")
    return out


def outcome(p: SetupPlan | None, status: str, detail: str, image: str = "") -> dict:
    out = {"status": status, "detail": scrub(detail, *(p.hidden if p else ()))[:400],
           "image": p.image if p else image}
    if p is not None:
        out["domain"] = p.domain
        if p.role == "exchange":
            out["organization"] = p.org
        if p.role == "mecm":
            out["site_code"] = p.site_code
    return out


def running_outcome(p: SetupPlan, since: float, last: int) -> dict:
    """The ``running`` record the build stores; the reconcile task (worker/setup_reconcile.py)
    reads ``reconcile`` back to finish it. No secret: the domain password is re-derived there
    from the range's DC Administrator account."""
    out = outcome(p, "running", f"{LABELS[p.role]} {STATUS.get(last, last)}; continues in the guest, the reconcile "
                                f"task records the result (check {STATUS_FILE})")
    out["reconcile"] = {"role": p.role, "domain": p.domain, "netbios": p.netbios, "dc_node": p.dc_node,
                        "status_file": STATUS_FILE, "since": since, "timeout": VSPHERE_ROLE_SETUP_TIMEOUT}
    return out


def warning(name: str, role: str, out: dict) -> str:
    return f"VM {name}: {LABELS.get(role, role)} setup {out['status']}: {out['detail']}"


def _err(exc: Exception) -> str:
    return str(getattr(exc, "msg", None) or exc or type(exc).__name__)


def prepare(session_for: Callable[[guest.GuestCredentials], object], p: SetupPlan, deadline: float,
            reboot_fn=vsphere_roles.reboot) -> dict | None:
    """As the local Administrator: check the staged-image marker, make the state folder,
    deliver the domain password by file transfer, join the domain (the join reads and deletes
    that file) and reboot. None: ready for the VM's software and then ``run``; else the VM's
    final outcome. No secret touches a command line."""
    session = session_for(p.local)
    uploaded = False
    try:
        session.wait_ready(deadline)
        result, code = session.run(marker_command(p.image), deadline)
        if result != "ok":
            return outcome(p, "failed", f"staged-image check {result} (exit {code})")
        if code in MARKER_CODES:
            return outcome(p, "skipped" if code == 20 else "failed", MARKER_CODES[code])
        result, code = session.run(init_command(), deadline)
        if result != "ok":
            why = JOIN_CODES.get(code, f"could not create {STATE_DIR} ({result}, exit {code})")
            return outcome(p, "failed", why)
        if code == 5:  # a status file already there: an earlier build started the setup
            logger.info("role setup on %s: already started by an earlier build", p.name)
            return None
        logger.info("role setup on %s: staged %s; joining %s", p.name, p.image, p.domain)
        uploaded = True
        session.upload(JOIN_SECRET, p.domain_password.reveal().encode("utf-8"), deadline)
        result, code = session.run(join_command(p), deadline)
        if result != "ok":
            # A timed-out join was killed (TerminateProcessInGuest): its finally never ran.
            _cleanup(session, deadline)
            return outcome(p, "failed", f"domain join: {JOIN_CODES.get(code, f'{result} (exit {code})')}")
        if code == vsphere_roles.EXIT_REBOOT:
            reboot_fn(session, deadline)
    except Exception as exc:  # noqa: BLE001 — reported (scrubbed) per VM, never raised past it
        if uploaded:  # the join may not have run: the file must not stay
            _cleanup(session, deadline)
        return outcome(p, "failed", f"guest operations: {_err(exc)}")
    return None


CLEANUP_GRACE = 60  # seconds a clean-up gets even when the step before it used the deadline


def _cleanup(session, deadline: float) -> bool:
    """Best effort, bounded: delete every secret file in the guest. True when it ran ok."""
    try:
        result, _ = session.run(cleanup_command(), max(deadline, session._clock() + CLEANUP_GRACE))
    except Exception:  # noqa: BLE001 — INIT and the next reconcile try again
        return False
    return result == "ok"


def cleanup(session, deadline: float) -> bool:
    """For a terminal reconcile (timeout) with no other guest step: log in, then clean up. Never raises."""
    try:
        session.wait_ready(deadline)
    except Exception:  # noqa: BLE001 — unreachable: nothing to clean from here
        return False
    return _cleanup(session, deadline)


def _read_status(session, deadline: float) -> int | None:
    """The runner's status number (the status-read process's exit code), STATUS_MISSING if the
    file is not there, STATUS_UNREADABLE if reading it is denied; None for anything else (a
    guest-ops error, a timeout, an exit code the script never gives: 1, 0xC000013A, ...), which
    callers retry and never take for a result or for "already in progress"."""
    try:
        result, code = session.run(poll_command(), deadline)
    except Exception:  # noqa: BLE001 — the deadline bounds the caller's loop
        return None
    return code if result == "ok" and code in KNOWN_STATUS else None


def run(session_for: Callable[[guest.GuestCredentials], object], p: SetupPlan, deadline: float,
        budget_end: float, *, build_wait: float | None = None, poll: float | None = None,
        clock: Callable[[], float] | None = None, epoch: Callable[[], float] | None = None,
        reboot_fn=vsphere_roles.reboot) -> dict:
    """As the domain Administrator (guest-ops auth; the password never reaches a command line
    or disk): deliver the runner, parameters and any SharePoint secrets by file transfer, start
    the setup detached, and wait at most VSPHERE_SETUP_BUILD_WAIT for it. Usually returns
    ``running`` with the reconcile record; the reconcile task finishes it. An earlier build's
    setup is never restarted."""
    session = session_for(p.domain_login)
    now = clock or session._clock
    at = epoch or _time.time
    wait = VSPHERE_ROLE_SETUP_POLL if poll is None else poll
    bw = VSPHERE_SETUP_BUILD_WAIT if build_wait is None else build_wait
    stop = min(deadline, budget_end)
    if stop - now() < MIN_START:
        return outcome(p, "skipped", "the provision time budget (VSPHERE_PROVISION_BUDGET) is spent; joined to "
                                     f"{p.domain}, setup not started")
    since = at()
    staged = False
    try:
        session.wait_ready(stop)
        # A guest-ops hiccup (None) is retried, never taken for "already in progress".
        code = _read_status(session, min(stop, now() + 120))
        while code is None and now() < stop:
            with contextlib.suppress(Exception):
                session._sleep(max(0.0, min(wait, stop - now())))
            code = _read_status(session, min(stop, now() + 120))
        if code is None:
            return outcome(p, "skipped", "could not read the setup status file (guest operations); not started")
        if code == STATUS_UNREADABLE:
            return outcome(p, "failed", UNREADABLE_DETAIL)
        if code == STATUS_MISSING:
            # Right before the product setup: a reboot the software installs left pending
            # would fail it. Reboot first and wait for VMware Tools (reboot_fn does).
            result, rc = session.run(reboot_pending_command(), stop)
            if result == "ok" and rc == vsphere_roles.EXIT_REBOOT:
                logger.info("role setup on %s: reboot pending after the software installs; rebooting", p.name)
                reboot_fn(session, stop)
            staged = True
            for name, content in ((RUNNER_FILE, RUNNER_SCRIPT), (PARAMS_FILE, json.dumps(params(p))),
                                  *([(SECRETS_FILE, json.dumps(secret_doc(p)))] if secret_doc(p) else [])):
                session.upload(name, content.encode("utf-8"), stop)
            session.start_detached(POWERSHELL, _runner_args())
            logger.info("role setup on %s: %s started", p.name, p.role)
        else:
            logger.info("role setup on %s: already in progress (status %s)", p.name, code)
    except Exception as exc:  # noqa: BLE001
        if staged:  # a secrets file may be on the disk with no runner to delete it
            with contextlib.suppress(Exception):
                session.run(cleanup_command(), max(stop, now() + 120))
        return outcome(p, "failed", f"could not start: {_err(exc)}")
    last = 2
    build_stop = min(stop, now() + bw)
    while True:
        code = _read_status(session, min(stop, now() + 120))
        if code == 0:
            with contextlib.suppress(Exception):
                session.run(cleanup_command(), min(stop, now() + 120))
            logger.info("role setup on %s: %s set up", p.name, p.role)
            return outcome(p, "ok", f"{LABELS[p.role]} {STATUS[0]} in {p.domain}")
        if code == STATUS_UNREADABLE:
            return outcome(p, "failed", UNREADABLE_DETAIL)
        if code is not None and code not in RUNNING and code != STATUS_MISSING:
            with contextlib.suppress(Exception):
                session.run(cleanup_command(), min(stop, now() + 120))
            return outcome(p, "failed", STATUS.get(code, f"setup runner status {code}"))
        if code in RUNNING:
            last = code
        if now() >= build_stop:
            break
        with contextlib.suppress(Exception):
            session._sleep(max(0.0, min(wait, build_stop - now())))
    return running_outcome(p, since, last)


def _reconcile_base(info: dict) -> dict:
    role = str(info.get("role") or "")
    image = next((i for i, r in SETUP_IMAGES.items() if r == role), "")
    return {"status": "", "detail": "", "image": image, "domain": info.get("domain", ""), "reconcile": info}


def timed_out(info: dict, *, epoch: Callable[[], float] | None = None) -> dict | None:
    """The ``timeout`` outcome of a ``running`` setup past VSPHERE_ROLE_SETUP_TIMEOUT from its
    start, else None. No guest operation: a powered-off, unreachable or no longer
    authenticatable VM still ends."""
    at = epoch or _time.time
    timeout = float(info.get("timeout") or VSPHERE_ROLE_SETUP_TIMEOUT)
    if at() - float(info.get("since") or 0) <= timeout:
        return None
    label = LABELS.get(str(info.get("role") or ""), str(info.get("role") or ""))
    return {**_reconcile_base(info), "status": "timeout",
            "detail": f"{label} not finished within VSPHERE_ROLE_SETUP_TIMEOUT ({int(timeout)}s); see "
                      f"{STATUS_FILE} and {RUNNER_LOG} in the guest"}


def reconcile(session, info: dict, *, clock: Callable[[], float] | None = None,
              epoch: Callable[[], float] | None = None, deadline: float | None = None) -> dict | None:
    """Read a ``running`` product setup's status file (worker/setup_reconcile.py). Returns the
    final outcome (ok / failed / timeout) and runs the secrets clean-up on a terminal state, or
    None while it is still running or could not be read this round (leave it ``running``).

    The timeout is checked first, before any guest operation: a VM that is powered off,
    unreachable or no longer accepts the login still ends ``timeout`` instead of staying
    ``running`` for ever."""
    now = clock or session._clock
    stop = deadline if deadline is not None else now() + 120
    role = str(info.get("role") or "")
    label = LABELS.get(role, role)
    base = _reconcile_base(info)
    if (expired := timed_out(info, epoch=epoch)) is not None:
        return expired
    try:
        session.wait_ready(stop)
    except Exception as exc:  # noqa: BLE001
        logger.info("reconcile %s: not reachable this round: %s", info.get("dc_node"), _err(exc))
        return None
    code = _read_status(session, min(stop, now() + 120))
    if code is None:
        return None
    if code == STATUS_UNREADABLE:
        return {**base, "status": "failed", "detail": UNREADABLE_DETAIL}
    if code == 0:
        with contextlib.suppress(Exception):
            session.run(cleanup_command(), min(stop, now() + 120))
        return {**base, "status": "ok", "detail": f"{label} {STATUS[0]} in {info.get('domain', '')}"}
    if code != STATUS_MISSING and code not in RUNNING:
        with contextlib.suppress(Exception):
            session.run(cleanup_command(), min(stop, now() + 120))
        return {**base, "status": "failed", "detail": STATUS.get(code, f"setup runner status {code}")}
    return None
