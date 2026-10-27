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

from typing import Any, ClassVar
from urllib.parse import urlparse

from .. import hypervisor_binding
from .base import BaseConsoleBackend, ConsoleError

try:  # pyVmomi is optional outside vSphere deployments
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim
except ImportError:  # pragma: no cover - exercised only without pyvmomi installed
    SmartConnect = Disconnect = vim = None


class VsphereWebMKSConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "vsphere_api"
    hypervisor_type: ClassVar[str] = "vsphere"  # hypervisor_connections.hypervisor_type

    def open_connections(self, vms: list[dict[str, Any]], *, db=None, rng=None) -> int | None:
        """Sum of ``VirtualMachine.runtime.numMksConnections`` over the range's own VMs, on the
        range's own vCenter. None on any failure, or when a VM cannot be read (fail safe)."""
        if SmartConnect is None or db is None or rng is None:
            return None
        ids = [str(vm.get("vm_id") or "") for vm in vms]
        if not ids or not all(ids):
            return None
        try:
            target = hypervisor_binding.resolve(db, rng, self.hypervisor_type)
            url = urlparse(target["url"])
            si = SmartConnect(
                host=url.hostname,
                port=url.port or 443,
                user=target["username"],
                pwd=target["password"],
                disableSslCertValidation=not target["verify_ssl"],
            )
        except Exception:  # noqa: BLE001 — unknown, never "no consoles"
            return None
        try:
            total = 0
            for vm, vm_id in zip(vms, ids, strict=True):
                machine = vim.VirtualMachine(vm_id, si._stub)
                if not hypervisor_binding.owns(str(rng.id), str(vm.get("name") or ""), str(machine.name or ""),
                                               getattr(machine.config, "annotation", None)):
                    return None
                total += int(getattr(machine.runtime, "numMksConnections", 0) or 0)
            return total
        except Exception:  # noqa: BLE001
            return None
        finally:
            Disconnect(si)

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
