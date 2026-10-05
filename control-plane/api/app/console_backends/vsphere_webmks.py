"""VMware WebMKS console: a one-time ticket for one VM, from vCenter through pyVmomi.

``VirtualMachine.AcquireTicket("webmks")`` returns the ESXi host, port and a ticket valid
for a single connection that expires within about a minute if unused. The browser opens
``wss://<host>:<port>/ticket/<ticket>`` with VMware's WebMKS SDK, so the student's browser
must be able to reach the ESXi host (and trust its certificate); TrueNorth never relays
the screen. Connection settings are the provisioner's (VSPHERE_URL, VSPHERE_USERNAME,
VSPHERE_PASSWORD, VSPHERE_VERIFY_SSL).
"""

from __future__ import annotations

import os
from typing import Any, ClassVar
from urllib.parse import urlparse

from .base import BaseConsoleBackend, ConsoleError

try:  # pyVmomi is optional outside vSphere deployments
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim
except ImportError:  # pragma: no cover - exercised only without pyvmomi installed
    SmartConnect = Disconnect = vim = None


class VsphereWebMKSConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "vsphere_api"

    def open(self, vm: dict[str, Any]) -> dict[str, Any]:
        if SmartConnect is None:
            raise ConsoleError("the vSphere console needs pyvmomi on the API")
        vm_id = str(vm.get("vm_id") or "")
        if not vm_id:
            raise ConsoleError("this VM has no hypervisor id yet")
        url = urlparse(os.getenv("VSPHERE_URL", ""))
        if not url.hostname:
            raise ConsoleError("VSPHERE_URL is not configured")
        verify = os.getenv("VSPHERE_VERIFY_SSL", "false").lower() == "true"
        try:
            si = SmartConnect(
                host=url.hostname,
                port=url.port or 443,
                user=os.getenv("VSPHERE_USERNAME", ""),
                pwd=os.getenv("VSPHERE_PASSWORD", ""),
                disableSslCertValidation=not verify,
            )
        except Exception as exc:  # noqa: BLE001 — any login failure is a console failure
            raise ConsoleError(f"vCenter refused the console request: {exc}") from exc
        try:
            machine = vim.VirtualMachine(vm_id, si._stub)
            ticket = machine.AcquireTicket("webmks")
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError(f"could not open the console for {vm.get('name', vm_id)}: {exc}") from exc
        finally:
            Disconnect(si)
        host = ticket.host or url.hostname
        return {"kind": "webmks", "url": f"wss://{host}:{ticket.port or 443}/ticket/{ticket.ticket}", "expires_in": 60}
