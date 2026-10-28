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
import queue
import threading
import time
from typing import Any, ClassVar
from urllib.parse import urlparse

from .. import hypervisor_binding
from .base import BaseConsoleBackend, ConsoleError, ConsoleReader, ConsoleTarget

try:  # pyVmomi is optional outside vSphere deployments
    from pyVim.connect import Connect, Disconnect, SmartConnect
    from pyVmomi import vim, vmodl
except ImportError:  # pragma: no cover - exercised only without pyvmomi installed
    Connect = SmartConnect = Disconnect = vim = vmodl = None

logger = logging.getLogger(__name__)
# A vCenter that does not answer must not hold up the lab sweep (#311 rounds 3 and 4).
CONNECT_TIMEOUT = 10  # seconds, per HTTP connection
# The console check logs in with this API version: no version probe, which has no timeout.
API_VERSION = os.getenv("LAB_CONSOLE_VSPHERE_VERSION", "vim.version.v7_0")
VCENTER_SECONDS: float | None = None  # a test's override of vcenter_seconds()
# vCenters a console check is talking to now, process-wide: a check never starts a second
# thread on a vCenter whose last one has not come back (its labs are unknown meanwhile).
_IN_FLIGHT: set[tuple[str, str]] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def vcenter_seconds() -> float:
    """How long one vCenter's answer may take: ``LAB_CONSOLE_VCENTER_SECONDS``, else two
    thirds of the sweep's console batch (``LAB_CONSOLE_BATCH_SECONDS``, 30), so the answers of
    healthy vCenters come back before the batch gives up on a slow one."""
    if VCENTER_SECONDS is not None:
        return VCENTER_SECONDS
    try:
        if os.getenv("LAB_CONSOLE_VCENTER_SECONDS"):
            return float(os.environ["LAB_CONSOLE_VCENTER_SECONDS"])
        return max(1.0, float(os.getenv("LAB_CONSOLE_BATCH_SECONDS", "30")) * 2 / 3)
    except ValueError:
        return 20.0


class VsphereWebMKSConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "vsphere_api"
    hypervisor_type: ClassVar[str] = "vsphere"  # hypervisor_connections.hypervisor_type

    def console_reader(self, db, targets: list[ConsoleTarget]) -> ConsoleReader:
        """Open WebMKS/VMRC connections (``runtime.numMksConnections``) on every target's VMs:
        one login per vCenter (each range's own, ``hypervisor_binding.resolve``, read here, on
        the caller's thread, from ``db``), one PropertyCollector read of all its VMs,
        ``CONNECT_TIMEOUT`` per connection and ``vcenter_seconds()`` per vCenter, vCenters in
        parallel on daemon threads. The threads get plain data only (logins, range ids, VM ids
        and names), never an ORM object or the session. A vCenter whose previous check is still
        out is skipped. A target is None (unknown) when its vCenter was skipped or did not
        answer in time, a VM was missing, or a VM is not its range's."""
        keys = [t.key for t in targets]
        if Connect is None:
            return lambda: dict.fromkeys(keys)
        groups: dict[tuple[str, str], tuple[dict, list[tuple]]] = {}
        for t in targets:
            vms = [(str(vm.get("vm_id") or ""), str(vm.get("name") or "")) for vm in t.vms]
            if not vms or not all(v for v, _ in vms):
                continue  # unknown
            try:
                login = dict(hypervisor_binding.resolve(db, t.rng, self.hypervisor_type))
            except Exception:  # noqa: BLE001 — unknown, never "no consoles"
                continue
            endpoint = (str(login["url"]), str(login["username"]))
            groups.setdefault(endpoint, (login, []))[1].append((t.key, str(t.range_id), vms))

        def read() -> dict:
            out: dict = dict.fromkeys(keys)
            answers: queue.Queue = queue.Queue()
            started = 0
            for endpoint, (login, jobs) in groups.items():
                with _IN_FLIGHT_LOCK:
                    if endpoint in _IN_FLIGHT:
                        logger.warning("console check: %s is still answering the last check; its labs are kept",
                                       endpoint[0])
                        continue
                    _IN_FLIGHT.add(endpoint)
                threading.Thread(target=_ask_vcenter, args=(endpoint, login, jobs, answers),
                                 name="tn-console", daemon=True).start()
                started += 1
            deadline = time.monotonic() + vcenter_seconds()
            for _ in range(started):
                try:
                    answer = answers.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    logger.warning("console check: a vCenter did not answer in %ss; its labs are kept",
                                   vcenter_seconds())
                    break
                out.update(answer)
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




def _ask_vcenter(endpoint: tuple[str, str], login: dict, jobs: list[tuple], answers: queue.Queue) -> None:
    """One vCenter's answer, on its own daemon thread; lets the vCenter go when it is back."""
    try:
        answers.put(_one_vcenter(login, jobs))
    except Exception as exc:  # noqa: BLE001 — unknown for its labs
        logger.warning("console check: %s: %s", endpoint[0], exc)
        answers.put({})
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(endpoint)


def _one_vcenter(login: dict, jobs: list[tuple]) -> dict:
    """``jobs``: (key, range id, [(vm id, recorded name)]) for one vCenter. One login with an
    explicit API version (``Connect``: no version probe, which has no timeout), one
    PropertyCollector read."""
    url = urlparse(login["url"])
    si = Connect(
        host=url.hostname,
        port=url.port or 443,
        user=login["username"],
        pwd=login["password"],
        version=API_VERSION,
        disableSslCertValidation=not login["verify_ssl"],
        httpConnectionTimeout=CONNECT_TIMEOUT,
    )
    try:
        pc = si.RetrieveContent().propertyCollector
        moids = sorted({vm_id for _, _, vms in jobs for vm_id, _ in vms})
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
    for key, range_id, vms in jobs:
        total: int | None = 0
        for vm_id, recorded_name in vms:
            p = props.get(vm_id)
            if p is None or not hypervisor_binding.owns(range_id, recorded_name, str(p.get("name") or ""),
                                                        p.get("config.annotation")):
                total = None  # missing, or not this range's VM: unknown
                break
            total += int(p.get("runtime.numMksConnections") or 0)
        out[key] = total
    return out
