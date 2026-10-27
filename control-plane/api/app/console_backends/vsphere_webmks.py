"""VMware WebMKS console: a one-time ticket for one VM, from vCenter through pyVmomi.

``VirtualMachine.AcquireTicket("webmks")`` returns the ESXi host, port and a ticket valid
for a single connection that expires within about a minute if unused. The browser opens
``wss://<host>:<port>/ticket/<ticket>`` with VMware's WebMKS SDK, so the student's browser
must be able to reach the ESXi host (and trust its certificate); TrueNorth never relays
the screen.

The vCenter is the range's own (app/hypervisor_binding.py: the connection its build
recorded, with that connection's login only), and the ticket is issued only for a VM
whose range annotation (or pre-tag name) says it is this range's. Until 2026-10-11 it
always logged in with VSPHERE_* and asked for a ticket on the bare recorded ``vm-NN``,
which on a tenant's range named a VM on another vCenter.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, ClassVar
from urllib.parse import urlparse

from .. import hypervisor_binding
from .base import BaseConsoleBackend, ConsoleError, ConsoleReader, ConsoleTarget

try:  # pyVmomi is optional outside vSphere deployments
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim, vmodl
except ImportError:  # pragma: no cover - exercised only without pyvmomi installed
    SmartConnect = Disconnect = vim = vmodl = None

logger = logging.getLogger(__name__)
# A vCenter that does not answer must not hold up the lab sweep (#311 round 3).
CONNECT_TIMEOUT = 10  # seconds, per HTTP connection
VCENTER_SECONDS = float(os.getenv("LAB_CONSOLE_VCENTER_SECONDS", "30"))  # a whole vCenter's answer


class VsphereWebMKSConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "vsphere_api"
    hypervisor_type: ClassVar[str] = "vsphere"  # hypervisor_connections.hypervisor_type

    def console_reader(self, db, targets: list[ConsoleTarget]) -> ConsoleReader:
        """Open WebMKS/VMRC connections (``runtime.numMksConnections``) on every target's VMs:
        one login per vCenter (each range's own, ``hypervisor_binding.resolve``, read here from
        ``db``), one PropertyCollector read of all its VMs, ``CONNECT_TIMEOUT`` per connection
        and ``VCENTER_SECONDS`` per vCenter, vCenters in parallel. A target is None (unknown)
        when its vCenter did not answer in time, a VM was missing, or a VM is not its range's."""
        keys = [t.key for t in targets]
        if SmartConnect is None:
            return lambda: dict.fromkeys(keys)
        groups: dict[tuple, tuple[dict, list[ConsoleTarget]]] = {}
        for t in targets:
            ids = [str(vm.get("vm_id") or "") for vm in t.vms]
            if not ids or not all(ids):
                continue  # unknown
            try:
                login = hypervisor_binding.resolve(db, t.rng, self.hypervisor_type)
            except Exception:  # noqa: BLE001 — unknown, never "no consoles"
                continue
            key = (login["url"], login["username"])
            groups.setdefault(key, (login, []))[1].append(t)

        def read() -> dict:
            out: dict = dict.fromkeys(keys)
            if not groups:
                return out
            pool = ThreadPoolExecutor(max_workers=min(8, len(groups)), thread_name_prefix="tn-console")
            futures = [pool.submit(_one_vcenter, login, ts) for login, ts in groups.values()]
            done, late = wait(futures, timeout=VCENTER_SECONDS)
            for f in done:
                if f.exception() is None:
                    out.update(f.result())
            if late:
                logger.warning("console check: %d vCenter(s) did not answer in %ss", len(late), VCENTER_SECONDS)
            pool.shutdown(wait=False, cancel_futures=True)
            return out

        return read

    def open(self, vm: dict[str, Any], *, db=None, rng=None) -> dict[str, Any]:
        if SmartConnect is None:
            raise ConsoleError("the vSphere console needs pyvmomi on the API")
        vm_id = str(vm.get("vm_id") or "")
        if not vm_id:
            raise ConsoleError("this VM has no hypervisor id yet")
        if db is None or rng is None:
            raise ConsoleError("the console needs the VM's range to find its vCenter")
        try:
            target = hypervisor_binding.resolve(db, rng, self.hypervisor_type)
        except hypervisor_binding.BindingError as exc:
            raise ConsoleError(str(exc)) from exc
        url = urlparse(target["url"])
        if not url.hostname:
            raise ConsoleError("no vCenter is configured for this range")
        try:
            si = SmartConnect(
                host=url.hostname,
                port=url.port or 443,
                user=target["username"],
                pwd=target["password"],
                disableSslCertValidation=not target["verify_ssl"],
            )
        except Exception as exc:  # noqa: BLE001 — any login failure is a console failure
            raise ConsoleError(f"vCenter refused the console request: {exc}") from exc
        label = vm.get("name", vm_id)
        try:
            machine = vim.VirtualMachine(vm_id, si._stub)
            if not hypervisor_binding.owns(str(rng.id), str(vm.get("name") or ""), str(machine.name or ""),
                                           getattr(machine.config, "annotation", None)):
                raise ConsoleError(f"{label} on this vCenter is not this range's VM; no console")
            ticket = machine.AcquireTicket("webmks")
        except ConsoleError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError(f"could not open the console for {label}: {exc}") from exc
        finally:
            Disconnect(si)
        host = ticket.host or url.hostname
        return {"kind": "webmks", "url": f"wss://{host}:{ticket.port or 443}/ticket/{ticket.ticket}", "expires_in": 60}


def _one_vcenter(login: dict, targets: list[ConsoleTarget]) -> dict:
    """One vCenter's answer for its targets: one login, one PropertyCollector read."""
    url = urlparse(login["url"])
    si = SmartConnect(
        host=url.hostname,
        port=url.port or 443,
        user=login["username"],
        pwd=login["password"],
        disableSslCertValidation=not login["verify_ssl"],
        httpConnectionTimeout=CONNECT_TIMEOUT,
    )
    try:
        pc = si.RetrieveContent().propertyCollector
        moids = sorted({str(vm["vm_id"]) for t in targets for vm in t.vms})
        spec = vmodl.query.PropertyCollector.FilterSpec(
            objectSet=[vmodl.query.PropertyCollector.ObjectSpec(obj=vim.VirtualMachine(m, si._stub)) for m in moids],
            propSet=[vmodl.query.PropertyCollector.PropertySpec(
                type=vim.VirtualMachine, pathSet=["name", "config.annotation", "runtime.numMksConnections"])],
            reportMissingObjectsInResults=True,
        )
        props: dict[str, dict] = {}
        result = pc.RetrievePropertiesEx([spec], vmodl.query.PropertyCollector.RetrieveOptions())
        while result is not None:
            for obj in result.objects or []:
                props[str(obj.obj._moId)] = {p.name: p.val for p in obj.propSet or []}
            token = getattr(result, "token", None)
            result = pc.ContinueRetrievePropertiesEx(token) if token else None
    finally:
        Disconnect(si)
    out: dict = {}
    for t in targets:
        total: int | None = 0
        for vm in t.vms:
            p = props.get(str(vm["vm_id"]))
            if p is None or not hypervisor_binding.owns(str(t.rng.id), str(vm.get("name") or ""),
                                                        str(p.get("name") or ""), p.get("config.annotation")):
                total = None  # missing, or not this range's VM: unknown
                break
            total += int(p.get("runtime.numMksConnections") or 0)
        out[t.key] = total
    return out
