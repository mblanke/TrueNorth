"""TrueNorth Range - is a built vSphere VM's guest network really up?

Observed 2026-10-20: a range was recorded ``ready`` showing all three planned addresses
while one VM had none in VMware Tools (its netplan was keyed to a MAC vCenter then
changed, vsphere_macs.py). The provisioner reported the *planned* address whenever Tools
gave none. Now each VM's planned static addresses are compared with what Tools reports
after power-on (the provisioner polls, bounded: VSPHERE_GUEST_IP_TIMEOUT), the observed
ones are recorded, and a Linux or Windows VM that never shows its addresses is
``not_ready``, which fails the range with the reason (range_alloc.build_outcome).

When the build's own budget (``finish_by``, worker/fencing.py), not
VSPHERE_GUEST_IP_TIMEOUT, ended the wait, the VM is not failed: its check is left
``running`` in ``ip_check`` with the window's end, like a long in-guest setup, and the
setup-reconcile task settles it (``settle``; worker/setup_reconcile.py).

Not held to it: router appliances (pfSense, OPNsense, VyOS), whose images may not run
VMware Tools at all, and any node a template marks ``verify_ip: false`` (a deliberately
Tools-less vulnerable image). Their planned addresses are recorded unverified
(``ip_verified`` None) unless Tools runs and says otherwise. Pure functions, no SDK.

A Windows VM is also held to its guest customization (Sysprep) outcome, in the same window
and with the same budget rule: vsphere_customization.py (#255).
"""

from __future__ import annotations

from . import vsphere_infra as infra


def planned_ips(vm_def: dict) -> list[str]:
    """Every static address the build configured in the guest, in NIC order."""
    return [str(n["ip"]) for n in vm_def.get("nics") or [] if n.get("ip")]


def verifiable(vm_def: dict) -> bool:
    """A guest whose network the build configured itself, whose image runs Tools, and
    whose template did not opt it out (``verify_ip: false``, render.py)."""
    return infra.os_family(vm_def) in ("linux", "windows") and vm_def.get("verify_ip") is not False


def _ip(primary: str, mgmt: str, seen: list[str], fallback: str) -> str:
    """The planned primary when Tools reports it, else the first other address Tools
    reports, else ``fallback``. Never a planned address Tools contradicts."""
    if primary and primary in seen:
        return primary
    other = [s for s in seen if s != mgmt]
    return other[0] if other else fallback


def _missing_reason(missing: list[str], seen: list[str], window: str) -> str:
    return (f"planned address {', '.join(missing)} not reported by VMware Tools {window} "
            f"(Tools reports {', '.join(seen) or 'no IPv4 address'}): its guest network configuration did not apply")


def assess(vm_def: dict, guest: dict) -> dict:
    """The VM's recorded addresses and readiness from ``guest`` (the provisioner's
    ``_await_guest_network``: tools_ready, seen, timeout, checked, cut, deadline).

    ``ip``: see ``_ip``; an unverifiable VM without Tools keeps the planned one.
    ``ip_verified``: True / False, or None when there was nothing to compare (or the
    check continues after the build). ``not_ready``: why the VM fails readiness ('' when
    it does not). ``ip_check``: the check the reconcile task finishes ({} when none).
    ``warning``: worth saying, not failing."""
    nics = vm_def.get("nics") or [{}]
    planned = planned_ips(vm_def)
    primary = str(next((n for n in nics if not n.get("uplink")), nics[0]).get("ip") or "")
    seen = [ip for ip in guest.get("seen") or [] if ip]
    tools = bool(guest.get("tools_ready"))
    missing = [ip for ip in planned if ip not in seen]
    verified = (not missing) if planned and (tools or verifiable(vm_def)) else None
    unchecked = not tools and not verifiable(vm_def)
    out = {"ip": _ip(primary, vm_def.get("_mgmt_ip") or "", seen, primary if unchecked else ""),
           "planned_ip": primary, "guest_ips": seen, "ip_verified": verified, "not_ready": "", "warning": "",
           "ip_check": {}}
    window = f"within {guest.get('timeout', 0)}s of power-on"
    if verified is False and guest.get("checked") and guest.get("cut"):
        # The build's budget ended the wait, not the readiness window: not a failure yet.
        out["ip_verified"] = None
        out["ip_check"] = {"status": "running", "planned": planned, "deadline": guest.get("deadline", 0)}
        out["warning"] = (f"guest network not verified yet (the build's time ran out {window}); "
                          f"planned {', '.join(planned)} is checked again until the window ends")
    elif verified is False and guest.get("checked"):
        out["not_ready"] = (f"VMware Tools did not report running {window}, so its guest network "
                            f"({', '.join(planned)}) could not be verified") if not tools \
            else _missing_reason(missing, seen, window)
    elif verified is False and tools:
        out["warning"] = (f"planned address {', '.join(missing)} not reported by VMware Tools "
                          f"(Tools reports {', '.join(seen) or 'no IPv4 address'})")
    return out


def pending(vm: dict) -> bool:
    """A recorded VM whose readiness check the build left to the reconcile task."""
    return (vm.get("ip_check") or {}).get("status") == "running" and bool(vm.get("vm_id"))


def settle(vm: dict, seen: list[str] | None, now: float) -> dict | None:
    """The reconcile task's verdict on a ``pending`` VM, from what Tools reports now
    (``seen``; None: could not be read). Returns the fields to store, or None while the
    window is still open and the addresses are not all there yet. A VM whose window has
    ended without them gets ``not_ready`` (the range then fails, setup_reconcile)."""
    check = vm.get("ip_check") or {}
    planned = [str(p) for p in check.get("planned") or []]
    seen = [s for s in seen or [] if s]
    missing = [ip for ip in planned if ip not in seen]
    base = {"guest_ips": seen, "ip": _ip(str(vm.get("planned_ip") or ""), str(vm.get("mgmt_ip") or ""), seen, "")}
    if not missing:
        return {**base, "ip_verified": True, "ip_check": {"status": "ok"}}
    if now < float(check.get("deadline") or 0):
        return None
    reason = _missing_reason(missing, seen, "before its readiness window ended")
    return {**base, "ip_verified": False, "ip_check": {"status": "failed", "detail": reason}, "not_ready": reason}
