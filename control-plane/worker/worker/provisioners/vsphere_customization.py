"""TrueNorth Range - did a Windows VM's guest customization (Sysprep) succeed? (#255)

Every Windows range VM is cloned from a golden template that still has its build
Administrator password and Basic-auth WinRM; vSphere guest customization (``CustomizeVM_Task``,
vsphere_api._customize_sync) runs Sysprep at first boot to replace them. ``CustomizeVM_Task``
only *applies* the spec: Sysprep runs, and can fail, after power-on. Before #255 only a VM
with software to install (``_guest`` credentials) ever looked at the outcome
(vsphere_guest.GuestSession.wait_ready); any other Windows VM whose Sysprep failed could be
marked ready with the template's login on it.

Now every Windows VM's outcome is read after power-on and recorded on the VM as
``customization``:

==============  =========================================================================
``ok``          ``guest.customizationInfo.customizationStatus`` TOOLSDEPLOYPKG_SUCCEEDED
                (or, on a vCenter that does not report it, a CustomizationSucceeded event).
``failed``      TOOLSDEPLOYPKG_FAILED / a CustomizationFailed event, or not finished when
                the readiness window (VSPHERE_GUEST_IP_TIMEOUT from power-on) ended: the VM
                is ``not_ready`` and the range fails (range_alloc.build_outcome, #232).
``running``     the build's own budget (``finish_by``), not the window, ended the wait: not
                failed, left with the window's end (``deadline``, epoch) for the reconcile
                task to ``settle``, as vsphere_readiness does for the guest network.
``unverified``  VSPHERE_GUEST_IP_CHECK=off (a simulator): one look, no wait, recorded.
                An observed failure still fails the VM.
==============  =========================================================================

``vsphere_status`` keeps what vSphere said (or ``event:<type>``). No SDK import here: the
reader takes the pyVmomi objects it is given (and builds its event filter only when pyVmomi
is installed); the rest are pure functions.
"""

from __future__ import annotations

import contextlib

from . import vsphere_infra as infra

try:  # the worker image has pyVmomi; the events fallback needs its filter types
    from pyVmomi import vim
except ImportError:  # pragma: no cover - the provisioner refuses to build without it
    vim = None

SUCCEEDED, FAILED = "TOOLSDEPLOYPKG_SUCCEEDED", "TOOLSDEPLOYPKG_FAILED"
FINISHED = (SUCCEEDED, FAILED)
OK, FAIL, RUNNING, UNVERIFIED = "ok", "failed", "running", "unverified"

# Event types (vim.event.*) that end a customization. CustomizationFailed's subclasses are
# listed: an event filter matches a type id exactly.
EVENTS_SUCCEEDED = ("CustomizationSucceeded",)
EVENTS_FAILED = ("CustomizationFailed", "CustomizationSysprepFailed", "CustomizationNetworkSetupFailed",
                 "CustomizationUnknownFailure", "CustomizationLinuxIdentityFailed")

NOT_FINISHED_YET = "guest customization (Sysprep) not finished yet"


def applies(vm_def: dict) -> bool:
    """Windows VMs: the ones the build customizes with Sysprep (``_customize_sync``)."""
    return infra.os_family(vm_def) == "windows"


def _event_name(event) -> str:
    return type(event).__name__.rsplit(".", 1)[-1]


def _from_events(content, vm) -> dict | None:
    """The newest customization-ending event of ``vm`` (None when there is none or the
    events cannot be read). For a vCenter whose VMware Tools reports no customizationInfo."""
    manager = getattr(content, "eventManager", None)
    if manager is None:
        return None
    kinds = list(EVENTS_SUCCEEDED + EVENTS_FAILED)
    try:
        spec = vim.event.EventFilterSpec(entity=vim.event.EventFilterSpec.ByEntity(entity=vm, recursion="self"),
                                         eventTypeId=kinds)
        events = [e for e in manager.QueryEvents(spec) or [] if _event_name(e) in kinds]
    except Exception:  # noqa: BLE001 — no events read: the status stays unknown, never a pass
        return None
    if not events:
        return None
    last = max(events, key=lambda e: (getattr(e, "createdTime", None) is not None, getattr(e, "createdTime", None),
                                      getattr(e, "key", 0)))
    name = _event_name(last)
    status = SUCCEEDED if name in EVENTS_SUCCEEDED else FAILED
    detail = str(getattr(last, "fullFormattedMessage", "") or getattr(last, "reason", "") or "")
    return {"status": status, "error": detail[:300], "source": f"event:{name}"}


def read(content, vm) -> dict:
    """What vSphere says about ``vm``'s guest customization now: ``{status, error, source}``.
    ``status`` is a TOOLSDEPLOYPKG_* value, or '' when nothing is reported. Never raises."""
    info = None
    with contextlib.suppress(Exception):
        info = getattr(getattr(vm, "guest", None), "customizationInfo", None)
    status = str(getattr(info, "customizationStatus", None) or "")
    if status:
        return {"status": status, "error": str(getattr(info, "errorMsg", None) or "")[:300], "source": "guest"}
    return _from_events(content, vm) or {"status": "", "error": "", "source": ""}


def _failed_reason(seen: dict) -> str:
    said = seen.get("status") if seen.get("source") == "guest" else seen.get("source")
    error = f": {seen['error']}" if seen.get("error") else ""
    return (f"guest customization (Sysprep) failed ({said or FAILED}{error}); the template's build "
            "Administrator password and WinRM settings may still be on it")


def _timeout_reason(seen: dict, window: str) -> str:
    return (f"guest customization (Sysprep) did not finish {window} (last status "
            f"{seen.get('status') or 'not reported'}); the template's build login may still be on it")


def _record(status: str, seen: dict, **more) -> dict:
    return {"status": status, "vsphere_status": seen.get("status") or "", "source": seen.get("source") or "",
            **more}


def assess(seen: dict) -> dict:
    """The VM's ``customization`` record, ``not_ready`` reason and ``warning`` from the
    provisioner's wait (``seen``: read() plus checked, cut, timeout, deadline)."""
    window = f"within {seen.get('timeout', 0)}s of power-on"
    status = seen.get("status")
    if status == SUCCEEDED:
        return {"customization": _record(OK, seen), "not_ready": "", "warning": ""}
    if status == FAILED:
        reason = _failed_reason(seen)
        return {"customization": _record(FAIL, seen, detail=reason), "not_ready": reason, "warning": ""}
    if not seen.get("checked"):
        text = "guest customization (Sysprep) outcome not verified (VSPHERE_GUEST_IP_CHECK=off)"
        return {"customization": _record(UNVERIFIED, seen, detail=text), "not_ready": "", "warning": text}
    if seen.get("cut"):
        return {"customization": _record(RUNNING, seen, deadline=seen.get("deadline", 0)), "not_ready": "",
                "warning": f"{NOT_FINISHED_YET} (the build's time ran out {window}); checked again until the "
                           "window ends"}
    reason = _timeout_reason(seen, window)
    return {"customization": _record(FAIL, seen, detail=reason), "not_ready": reason, "warning": ""}


def pending(vm: dict) -> bool:
    """A recorded VM whose customization check the build left to the reconcile task."""
    return (vm.get("customization") or {}).get("status") == RUNNING and bool(vm.get("vm_id"))


def settle(vm: dict, seen: dict, now: float) -> dict | None:
    """The reconcile task's verdict on a ``pending`` VM from ``seen`` (read() now): the
    fields to store, or None while the window is open and customization has not finished."""
    check = vm.get("customization") or {}
    if seen.get("status") == SUCCEEDED:
        return {"customization": _record(OK, seen)}
    if seen.get("status") == FAILED:
        reason = _failed_reason(seen)
        return {"customization": _record(FAIL, seen, detail=reason), "not_ready": reason}
    if now < float(check.get("deadline") or 0):
        return None
    reason = _timeout_reason(seen, "before its readiness window ended")
    return {"customization": _record(FAIL, seen, detail=reason), "not_ready": reason}
