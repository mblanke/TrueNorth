"""TrueNorth Range - is a built vSphere VM's guest network really up?

Observed 2026-10-20: a range was recorded ``ready`` showing all three planned addresses
while one VM had none in VMware Tools (its netplan was keyed to a MAC vCenter then
changed, vsphere_macs.py). The provisioner reported the *planned* address whenever Tools
gave none. Now each VM's planned static addresses are compared with what Tools reports
after power-on (the provisioner polls, bounded: VSPHERE_GUEST_IP_TIMEOUT), the observed
ones are recorded, and a Linux or Windows VM that never shows its addresses is
``not_ready``, which fails the range with the reason (range_alloc.build_outcome).

Router appliances (pfSense, OPNsense, VyOS) are not held to it: their images may not run
VMware Tools at all. Their planned addresses are recorded unverified (``ip_verified``
None) unless Tools runs and says otherwise. Pure functions, no SDK.
"""

from __future__ import annotations

from . import vsphere_infra as infra


def planned_ips(vm_def: dict) -> list[str]:
    """Every static address the build configured in the guest, in NIC order."""
    return [str(n["ip"]) for n in vm_def.get("nics") or [] if n.get("ip")]


def verifiable(vm_def: dict) -> bool:
    """A guest whose network the build configured itself and whose image runs Tools."""
    return infra.os_family(vm_def) in ("linux", "windows")


def assess(vm_def: dict, guest: dict) -> dict:
    """The VM's recorded addresses and readiness from ``guest`` (the provisioner's
    ``_await_guest_network``: tools_ready, seen, timeout, checked).

    ``ip``: the planned primary address when Tools reports it, else the first address
    Tools reports, else (an unverifiable appliance) the planned one, else ''. Never a
    planned address Tools contradicts. ``ip_verified``: True / False, or None when there
    was nothing to compare. ``not_ready``: why the VM fails readiness ('' when it does
    not). ``warning``: worth saying, not failing."""
    nics = vm_def.get("nics") or [{}]
    planned = planned_ips(vm_def)
    primary = str(next((n for n in nics if not n.get("uplink")), nics[0]).get("ip") or "")
    mgmt = vm_def.get("_mgmt_ip") or ""
    seen = [ip for ip in guest.get("seen") or [] if ip]
    tools = bool(guest.get("tools_ready"))
    missing = [ip for ip in planned if ip not in seen]
    verified = (not missing) if planned and (tools or verifiable(vm_def)) else None
    if primary and primary in seen:
        ip = primary
    elif other := [s for s in seen if s != mgmt]:
        ip = other[0]
    elif not tools and not verifiable(vm_def):
        ip = primary  # an appliance without Tools: as planned, ip_verified None
    else:
        ip = ""
    not_ready = warning = ""
    window = f"within {guest.get('timeout', 0)}s of power-on"
    if verified is False and guest.get("checked"):
        if not tools:
            not_ready = (f"VMware Tools did not report running {window}, so its guest network "
                         f"({', '.join(planned)}) could not be verified")
        else:
            not_ready = (f"planned address {', '.join(missing)} not reported by VMware Tools {window} "
                         f"(Tools reports {', '.join(seen) or 'no IPv4 address'}): its guest network "
                         "configuration did not apply")
    elif verified is False and tools:
        warning = (f"planned address {', '.join(missing)} not reported by VMware Tools "
                   f"(Tools reports {', '.join(seen) or 'no IPv4 address'})")
    return {"ip": ip, "planned_ip": primary, "guest_ips": seen, "ip_verified": verified,
            "not_ready": not_ready, "warning": warning}
