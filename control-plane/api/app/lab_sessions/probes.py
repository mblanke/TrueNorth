"""Readiness probes from a lab profile's ``health_checks``.

A lab is ready when every check passes, not when the provisioner returned VM ids:
  tools   VMware Tools (or the mock) reports the guest running and it has an address
  tcp     a TCP connection to the VM's address and port opens
  ssh     the port answers with an SSH banner
  http    an HTTP GET to the VM's address, port and path returns a status below 500
The API host must be able to reach the lab network for tcp/ssh/http checks; where it
cannot, profiles use ``tools`` checks.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx

TIMEOUT = 3.0


def vm_for(node: str, vms: list[dict[str, Any]], range_id: Any = None) -> dict[str, Any] | None:
    """The provisioned VM for a profile node: render_topology names it ``<range8>-<node>``,
    exactly (a suffix match let node ``dc`` resolve to ``<range8>-victim-dc``)."""
    want = f"{str(range_id)[:8]}-{node}" if range_id is not None else None
    for vm in vms:
        name = str(vm.get("name", ""))
        if name == want or (want is None and name == node):
            return vm
    return None


def run(checks: list[dict[str, Any]], vms: list[dict[str, Any]], range_id: Any = None) -> list[dict[str, Any]]:
    out = []
    for check in checks:
        vm = vm_for(check["node"], vms, range_id)
        result = {"node": check["node"], "kind": check["kind"], "ok": False, "detail": ""}
        if vm is None:
            result["detail"] = "no VM for this node"
        else:
            ok, detail = _probe(check, vm)
            result.update(ok=ok, detail=detail)
        out.append(result)
    return out


def _probe(check: dict[str, Any], vm: dict[str, Any]) -> tuple[bool, str]:
    ip = str(vm.get("ip") or "")
    kind = check["kind"]
    if kind == "tools":
        if not vm.get("tools_ready", vm.get("status") == "running"):
            return False, "guest tools not running yet"
        return (True, ip) if ip else (False, "no guest address yet")
    if not ip:
        return False, "no guest address yet"
    port = int(check.get("port") or 0)
    try:
        if kind == "http":
            resp = httpx.get(f"http://{ip}:{port}{check.get('path') or '/'}", timeout=TIMEOUT)
            return resp.status_code < 500, f"HTTP {resp.status_code}"
        with socket.create_connection((ip, port), timeout=TIMEOUT) as sock:
            if kind == "ssh":
                sock.settimeout(TIMEOUT)
                banner = sock.recv(64)
                return banner.startswith(b"SSH-"), banner[:32].decode("latin-1", "replace").strip()
            return True, f"{ip}:{port} open"
    except (OSError, httpx.HTTPError) as exc:
        return False, str(exc)[:200]
