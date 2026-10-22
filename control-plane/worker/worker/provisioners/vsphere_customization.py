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

Also (review of #264):

* A SUCCEEDED whose ``endTime`` (or CustomizationSucceeded event) predates the power-on
  request by more than VSPHERE_CUSTOMIZATION_SKEW (300 s: clock skew) is left over from the
  template or an earlier boot: it counts as not run yet.
* Sysprep's message is masked (``scrub``: the VM's passwords, the template passwords, and
  anything password-shaped such as ``net user NAME PASSWORD``) before it is stored, on the
  build path and the reconcile path.
* ``running`` is bounded: past its window plus VSPHERE_CUSTOMIZATION_GRACE the reconcile
  task fails it from the record alone (``expired``), even when vCenter cannot be reached.
* No template-login check while it is ``running``; the reconcile makes it once it is ``ok``.

``vsphere_status`` keeps what vSphere said (or ``event:<type>``). No SDK import here: the
reader takes the pyVmomi objects it is given (and builds its event filter only when pyVmomi
is installed); the rest are pure functions.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable

from . import vsphere_credcheck as credcheck
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

# A check left ``running`` fails from its record alone this long after its window ended,
# whatever vCenter says or whether it can be reached at all (worker/setup_reconcile.py,
# every SETUP_RECONCILE_SECONDS: three rounds by default). Never left open-ended.
GRACE_SECONDS = float(os.environ.get("VSPHERE_CUSTOMIZATION_GRACE", "900"))
# How far a Sysprep success may end "before" the power-on request and still be this boot's:
# vCenter's and the worker's clocks can disagree (work site, NTP unsynced for days).
SKEW_SECONDS = float(os.environ.get("VSPHERE_CUSTOMIZATION_SKEW", "300"))

# Sysprep's error text can quote the command that failed. Whatever looks like a password in
# it is masked before it is stored anywhere (the record, warnings, errors, logs).
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bnet(?:\.exe)?\s+user\s+\S+\s+)(?!/)\S+"),  # net user NAME PASSWORD [/add]
    re.compile(r"(?i)(\b(?:password|passwd|pwd)\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(-(?:password|pass)\s+)\S+"),
    re.compile(r"(?i)(<(?:value|password|plaintextpassword)>)[^<]*"),  # unattend XML
)


def known_secrets(env: dict | None = None) -> list[str]:
    """Passwords this worker knows that a Sysprep message must never carry: the golden
    templates' build passwords (vsphere_credcheck)."""
    env = os.environ if env is None else env
    return [str(env.get(k) or "") for k in (credcheck.WINDOWS_PASSWORD_ENV, credcheck.LINUX_PASSWORD_ENV)]


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every one of ``secrets`` and anything password-shaped masked, cut to 300."""
    out = str(text or "")
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        out = out.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(r"\1***", out)
    return out[:300]


def applies(vm_def: dict) -> bool:
    """Windows VMs: the ones the build customizes with Sysprep (``_customize_sync``)."""
    return infra.os_family(vm_def) == "windows"


def _event_name(event) -> str:
    return type(event).__name__.rsplit(".", 1)[-1]


def _epoch(when) -> float | None:
    """A vSphere dateTime as epoch seconds (None when absent or unreadable)."""
    try:
        return float(when.timestamp()) if when is not None else None
    except Exception:  # noqa: BLE001
        return None


def _before(when, since: float | None, skew: float | None = None) -> bool:
    """``when`` (vCenter's clock) is known and more than ``skew`` (VSPHERE_CUSTOMIZATION_SKEW)
    earlier than ``since`` (this power-on, the worker's clock): left over, not this boot's.
    The tolerance absorbs clocks that disagree (an NTP outage); a template's leftover success
    is hours or days old, far past it."""
    t = _epoch(when)
    skew = SKEW_SECONDS if skew is None else skew
    return since is not None and t is not None and t < since - skew


def _from_events(content, vm, since: float | None) -> dict | None:
    """The newest customization-ending event of ``vm`` (None when there is none or the
    events cannot be read). For a vCenter whose VMware Tools reports no customizationInfo.
    A success logged before ``since`` is not this power-on's and is ignored."""
    manager = getattr(content, "eventManager", None)
    if manager is None:
        return None
    kinds = list(EVENTS_SUCCEEDED + EVENTS_FAILED)
    try:
        spec = vim.event.EventFilterSpec(entity=vim.event.EventFilterSpec.ByEntity(entity=vm, recursion="self"),
                                         eventTypeId=kinds)
        events = [e for e in manager.QueryEvents(spec) or [] if _event_name(e) in kinds
                  and not (_event_name(e) in EVENTS_SUCCEEDED and _before(getattr(e, "createdTime", None), since))]
    except Exception:  # noqa: BLE001 — no events read: the status stays unknown, never a pass
        return None
    if not events:
        return None
    last = max(events, key=lambda e: (_epoch(getattr(e, "createdTime", None)) or 0.0, getattr(e, "key", 0)))
    name = _event_name(last)
    status = SUCCEEDED if name in EVENTS_SUCCEEDED else FAILED
    detail = str(getattr(last, "fullFormattedMessage", "") or getattr(last, "reason", "") or "")
    return {"status": status, "error": detail, "source": f"event:{name}"}


def read(content, vm, *, since: float | None = None, secrets: Iterable[str] = ()) -> dict:
    """What vSphere says about ``vm``'s guest customization now: ``{status, error, source}``.
    ``status`` is a TOOLSDEPLOYPKG_* value, or '' when nothing (of this power-on) is
    reported. ``since``: the power-on (epoch); a SUCCEEDED whose ``endTime`` is earlier is
    left over (the template's, a previous boot's) and counts as not run yet (``stale``).
    ``error`` is scrubbed of ``secrets``, the template passwords and anything
    password-shaped (``scrub``). Never raises."""
    info = None
    with contextlib.suppress(Exception):
        info = getattr(getattr(vm, "guest", None), "customizationInfo", None)
    status = str(getattr(info, "customizationStatus", None) or "")
    secrets = [*secrets, *known_secrets()]
    if status == SUCCEEDED and _before(getattr(info, "endTime", None), since):
        return {"status": "", "error": "", "source": "guest", "stale": SUCCEEDED}
    if status:
        return {"status": status, "error": scrub(getattr(info, "errorMsg", None) or "", secrets), "source": "guest"}
    seen = _from_events(content, vm, since) or {"status": "", "error": "", "source": ""}
    return {**seen, "error": scrub(seen["error"], secrets)}


def _failed_reason(seen: dict) -> str:
    said = seen.get("status") if seen.get("source") == "guest" else seen.get("source")
    error = f": {seen['error']}" if seen.get("error") else ""
    return (f"guest customization (Sysprep) failed ({said or FAILED}{error}); the template's build "
            "Administrator password and WinRM settings may still be on it")


def _timeout_reason(seen: dict, window: str) -> str:
    last = seen.get("status") or (f"{seen['stale']} from before this power-on" if seen.get("stale") else "not reported")
    return (f"guest customization (Sysprep) did not finish {window} (last status {last}); the template's "
            "build login may still be on it")


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
        return {"customization": _record(RUNNING, seen, deadline=seen.get("deadline", 0),
                                         powered_on_at=seen.get("since") or 0), "not_ready": "",
                "warning": f"{NOT_FINISHED_YET} (the build's time ran out {window}); checked again until the "
                           "window ends"}
    reason = _timeout_reason(seen, window)
    return {"customization": _record(FAIL, seen, detail=reason), "not_ready": reason, "warning": ""}


def pending(vm: dict) -> bool:
    """A recorded VM whose customization check the build left to the reconcile task."""
    return (vm.get("customization") or {}).get("status") == RUNNING and bool(vm.get("vm_id"))


def since(vm: dict) -> float | None:
    """The power-on a pending check belongs to (epoch), for ``read``."""
    return float((vm.get("customization") or {}).get("powered_on_at") or 0) or None


def expired(vm: dict, now: float, grace: float = GRACE_SECONDS) -> dict | None:
    """From the record alone (no vCenter): a check still ``running`` ``grace`` seconds after
    its window ended fails (the fields to store), so an unreachable vCenter or a VM the
    range-ownership check refuses never leaves it open. None otherwise."""
    check = vm.get("customization") or {}
    if check.get("status") != RUNNING or now <= float(check.get("deadline") or 0) + grace:
        return None
    reason = (f"guest customization (Sysprep) outcome not confirmed within {int(grace)}s after its readiness window "
              "ended (vCenter unreachable, or the VM could not be read); the template's build login may still be on it")
    return {"customization": {**_record(FAIL, {"status": check.get("vsphere_status"), "source": "record"}),
                              "detail": reason}, "not_ready": reason}


def settle(vm: dict, seen: dict, now: float) -> dict | None:
    """The reconcile task's verdict on a ``pending`` VM from ``seen`` (read() now, with
    ``since(vm)``): the fields to store, or None while the window is open and customization
    has not finished."""
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
