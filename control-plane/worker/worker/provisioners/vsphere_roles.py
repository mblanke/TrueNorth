"""TrueNorth Range - Windows Server roles inside range VMs, over VMware guest operations.

render.py gives a Windows Server node whose ``services`` name roles (worker/windows_roles.py)
``roles``, ``role_features`` and, for a domain controller, ``ad_domain`` /
``ad_forest_root``. After the VM is built and customized, the provisioner runs here, through
the same guest-operations session software installs use (vsphere_guest.py), as the built-in
Administrator with the random per-VM password Sysprep set:

1. feature roles: grow C: into any space the role's disk floor added, then one
   ``Install-WindowsFeature`` for every feature of the VM's roles; reboot on 3010;
2. the forest-root DC of each AD domain: ``Install-ADDSForest`` (random DSRM password,
   never stored), then reboot. A second DC of the same domain is reported ``skipped``:
   promoting an extra DC is not automated.

Image roles (Exchange, SQL Server, ...) were built into the role image the VM was cloned
from; they need no guest work, unless that image is not registered yet (render.py then
built the bare OS and set ``role_image_missing``), which is reported ``skipped``.

Every role ends ``ok``, ``failed`` or ``skipped`` with a detail; anything but ``ok`` is an
error on the range (``partial``), never a quiet ``ready``. Blocking pyVmomi: call through a
thread.
"""

from __future__ import annotations

import base64
import logging
import secrets

from .. import windows_roles
from . import vsphere_guest as guest

logger = logging.getLogger(__name__)

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
EXIT_REBOOT = 3010  # Windows: success, restart required
REBOOT_GRACE = 120  # seconds to see VMware Tools drop after a reboot request

# Grows C: into any space a larger virtual disk added, then installs the features.
FEATURE_SCRIPT = """$ErrorActionPreference = 'Stop'
$max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
if ((Get-Partition -DriveLetter C).Size -lt $max) {{ Resize-Partition -DriveLetter C -Size $max }}
$r = Install-WindowsFeature -Name {features} -IncludeManagementTools
if (-not $r.Success) {{ exit 1 }}
if ($r.RestartNeeded -eq 'Yes') {{ exit 3010 }}
exit 0
"""

FOREST_SCRIPT = """$ErrorActionPreference = 'Stop'
Import-Module ADDSDeployment
$dsrm = ConvertTo-SecureString '{dsrm}' -AsPlainText -Force
Install-ADDSForest -DomainName '{domain}' -DomainNetbiosName '{netbios}' -SafeModeAdministratorPassword $dsrm `
  -InstallDns -Force -NoRebootOnCompletion | Out-Null
exit 3010
"""


def encoded(script: str) -> str:
    """PowerShell ``-EncodedCommand`` form: no quoting surprises in the guest command line."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def ps_quote(value: str) -> str:
    return value.replace("'", "''")


def netbios(domain: str) -> str:
    label = "".join(c for c in domain.split(".")[0].upper() if c.isalnum())
    return (label or "RANGE")[:15]


def powershell(label: str, script: str, ok_codes: frozenset) -> guest.GuestCommand:
    args = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded(script)}"
    return guest.GuestCommand(label, POWERSHELL, args, ok_codes)


def feature_roles(vm_def: dict) -> list[str]:
    return [r for r in vm_def.get("roles") or [] if windows_roles.BY_ID[r]["install"]["method"] == "feature"]


def needs_guest(vm_def: dict) -> bool:
    """True when the VM has guest work to do (feature roles), so it needs a known login."""
    return bool(feature_roles(vm_def) and vm_def.get("role_features"))


def initial_status(vm_def: dict) -> dict[str, dict]:
    """Every role's outcome before any guest work: image roles are done (or skipped)."""
    missing = vm_def.get("role_image_missing")
    out: dict[str, dict] = {}
    for rid in vm_def.get("roles") or []:
        if windows_roles.BY_ID[rid]["install"]["method"] == "image":
            out[rid] = ({"status": "skipped", "detail": f"role image {missing} is not in the golden-image registry; "
                                                       "built on the bare OS"} if missing
                        else {"status": "ok", "detail": "in role image"})
        else:
            out[rid] = {"status": "skipped", "detail": "not run"}
    return out


def reboot(session: guest.GuestSession, deadline: float) -> None:
    """Restart the guest OS and wait until guest operations work again."""
    vm = session.vm
    vm.RebootGuest()
    grace = min(deadline, session._clock() + REBOOT_GRACE)
    while session._clock() < grace:  # Tools must drop first, or wait_ready returns at once
        if str(getattr(vm.guest, "toolsRunningStatus", "")) != "guestToolsRunning":
            break
        session._sleep(session._poll)
    session.wait_ready(deadline)


def install(session: guest.GuestSession, vm_def: dict, deadline: float, reboot_fn=reboot) -> dict[str, dict]:
    """Run the VM's role installs. Returns ``{role: {"status", "detail"}}`` for every role."""
    status = initial_status(vm_def)
    feats = feature_roles(vm_def)
    if not feats:
        return status
    creds = session.creds

    def mark(state: str, detail: str, only: list[str] | None = None) -> dict[str, dict]:
        for rid in only or feats:
            status[rid] = {"status": state, "detail": guest.redact(detail, creds)}
        return status

    step = feats  # the roles a failure right now belongs to
    try:
        session.wait_ready(deadline)
        features = ",".join(vm_def.get("role_features") or [])
        result, code = session.run(powershell("roles", FEATURE_SCRIPT.format(features=features),
                                              frozenset({0, EXIT_REBOOT})), deadline)
        if result != "ok":
            return mark("failed", f"Install-WindowsFeature {result} (exit {code})")
        if code == EXIT_REBOOT:
            reboot_fn(session, deadline)
        mark("ok", "installed")
        if "ad-ds" in feats:
            domain = str(vm_def.get("ad_domain") or "range.local")
            if not vm_def.get("ad_forest_root"):
                return mark("skipped", f"AD DS installed; promotion as an extra DC of {domain} is not automated",
                            ["ad-ds"])
            step = ["ad-ds"]
            script = FOREST_SCRIPT.format(dsrm=ps_quote(secrets.token_urlsafe(18) + "aA1!"),
                                          domain=ps_quote(domain), netbios=ps_quote(netbios(domain)))
            result, code = session.run(powershell("ad-forest", script, frozenset({EXIT_REBOOT})), deadline)
            if result != "ok":
                return mark("failed", f"Install-ADDSForest {result} (exit {code})", ["ad-ds"])
            reboot_fn(session, deadline)
            mark("ok", f"forest {domain} created", ["ad-ds"])
    except Exception as exc:  # noqa: BLE001 — reported per role, never raised past the VM
        logger.warning("role install on %s failed: %s", getattr(session.vm, "name", ""), guest.redact(str(exc), creds))
        return mark("failed", (getattr(exc, "msg", None) or str(exc) or type(exc).__name__)[:300], step)
    return status


def errors(name: str, status: dict[str, dict]) -> list[str]:
    """The range errors for a VM's role outcomes: every role that did not end ``ok``."""
    return [f"VM {name}: role {rid} {v['status']} ({v['detail']})" for rid, v in status.items() if v["status"] != "ok"]
