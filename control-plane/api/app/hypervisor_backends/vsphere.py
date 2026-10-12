"""vSphere connection backend — vCenter Automation REST API (the range platform).

Endpoint: ``https://{host}[:{port}]`` from the connection row; the port is left out when
it is 443 or unset, and a host that already carries a scheme or port is used as given.

REST dialect, detected at login as the worker's provisioner does
(``worker/provisioners/vsphere_api.py``): ``POST /api/session`` (vSphere 7.0U2+) first; a
404 there means the server has no ``/api`` at all (older vCenters, govmomi's vcsim), so
log in through the legacy ``/rest/com/vmware/cis/session`` (token wrapped as
``{"value": ...}``) and read the ``/rest/vcenter/...`` forms instead. The session is
deleted on the way out, whichever dialect, success or failure.

Credentials are the connection row's only; there is no environment fallback here.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import httpx

from ..models import HypervisorConnection, HypervisorNode
from ..schemas import HypervisorTestResult
from ..secretbox import unseal
from .base import BaseHypervisorBackend

API = "api"  # /api, vSphere 7.0U2+
REST = "rest"  # legacy /rest only: vCenter before 7.0U2, vcsim
_SESSION_PATH = {API: "/api/session", REST: "/rest/com/vmware/cis/session"}
_VERSION_PATH = {API: "/vcenter/system/version", REST: "/appliance/system/version"}
_VM_HOST_FILTER = {API: "hosts", REST: "filter.hosts"}


def base_url(conn) -> str:
    """``https://host[:port]`` for a connection row; no port for 443 or unset."""
    host = str(conn.host or "").strip().rstrip("/")
    if "://" in host:
        return host  # an explicit URL is taken as written
    port = getattr(conn, "port", None)
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        if ipaddress.ip_address(bare).version == 6:
            host = f"[{bare}]"
    except ValueError:
        if ":" in host:
            return f"https://{host}"  # host:port already
    if not port or int(port) == 443:
        return f"https://{host}"
    return f"https://{host}:{int(port)}"


class _Session:
    """A logged-in vCenter session: its dialect and auth header."""

    def __init__(self, client: httpx.Client, dialect: str, token: str) -> None:
        self.client = client
        self.dialect = dialect
        self.headers = {"vmware-api-session-id": token}

    def get(self, path: str, params: dict | None = None):
        """GET ``/api{path}`` or ``/rest{path}``; the legacy body's ``value`` is unwrapped."""
        r = self.client.get(f"/{self.dialect}{path}", params=params, headers=self.headers)
        r.raise_for_status()
        body = r.json()
        return body.get("value") if self.dialect == REST and isinstance(body, dict) else body

    def vms_on(self, host_id: str):
        return self.get("/vcenter/vm", params={_VM_HOST_FILTER[self.dialect]: host_id})


def _login(client: httpx.Client, username: str, password: str) -> tuple[str, str]:
    """Create a session; return ``(dialect, token)``. 401 and other errors raise."""
    r = client.post(_SESSION_PATH[API], auth=(username, password))
    if r.status_code != 404:
        r.raise_for_status()
        return API, r.json()  # a JSON string
    legacy = client.post(_SESSION_PATH[REST], auth=(username, password))
    legacy.raise_for_status()
    body = legacy.json()
    token = body.get("value") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"vCenter legacy session endpoint returned no token: {body!r}")
    return REST, token


@contextmanager
def _session(conn: HypervisorConnection, password: str, timeout: float) -> Iterator[_Session]:
    """Log in with the row's credentials; always delete the session afterwards."""
    with httpx.Client(base_url=base_url(conn), verify=conn.verify_ssl, timeout=timeout) as client:
        dialect, token = _login(client, conn.username, password)
        s = _Session(client, dialect, token)
        try:
            yield s
        finally:
            try:
                client.delete(_SESSION_PATH[dialect], headers=s.headers)
            except httpx.HTTPError:
                pass  # vCenter expires an idle session itself; the result stands


class VSphereBackend(BaseHypervisorBackend):
    kind = "vsphere"

    def check_connection(self, conn, db):
        return check_connection(conn, db)

    def discover(self, conn_id, conn, db):
        return discover(conn_id, conn, db)


def check_connection(conn: HypervisorConnection, db) -> HypervisorTestResult:
    """Test a vSphere connection via the vCenter Automation REST API."""
    # Outside the try: a missing or wrong TN_SECRETS_KEY is our fault, not the vCenter's,
    # and must not mark the connection inactive (it surfaces as a 503 naming the key).
    password = unseal(conn.password_encrypted) or ""
    try:
        note = ""
        with _session(conn, password, timeout=10) as s:
            if s.dialect == API:
                ver_info = s.get(_VERSION_PATH[API])
                hosts = s.get("/vcenter/host")
            else:
                # The login is the test. The legacy version and host reads are not served
                # everywhere (vcsim has neither), so their absence is reported, not fatal.
                ver_info, hosts, missing = {}, [], []
                try:
                    ver_info = s.get(_VERSION_PATH[REST]) or {}
                except httpx.HTTPStatusError:
                    missing.append("version")
                try:
                    hosts = s.get("/vcenter/host")
                except httpx.HTTPStatusError:
                    missing.append("hosts")
                note = " (legacy /rest API" + (f"; {' and '.join(missing)} not reported" if missing else "") + ")"

        version = (ver_info.get("version") if isinstance(ver_info, dict) else None) or "unknown"
        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return HypervisorTestResult(
            success=True,
            message=f"Connected to vCenter {version}{note}",
            version=version,
            nodes_found=len(hosts) if isinstance(hosts, list) else 0,
        )
    except Exception as exc:
        conn.is_active = False
        db.commit()
        return HypervisorTestResult(success=False, message=f"Connection failed: {exc}", version="n/a", nodes_found=0)


def _host_ip(name: str) -> str | None:
    """An ESXi host's inventory name is its IP when it was added by address, else an FQDN."""
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return None


def discover(conn_id, conn: HypervisorConnection, db) -> dict:
    """Discover ESXi hosts via the vCenter Automation REST API.

    That API reports each host's name and connection state, and lists VMs per host, but
    has no host CPU or memory figures. Those are left empty (None, "not reported"), never
    0, which would read as an idle host. Host capacity needs pyVmomi or the VI/JSON API.
    """
    password = unseal(conn.password_encrypted) or ""  # outside the try, as in check_connection
    try:
        with _session(conn, password, timeout=15) as s:
            try:
                hosts = s.get("/vcenter/host")
            except httpx.HTTPStatusError as exc:
                if s.dialect == REST and exc.response.status_code == 404:
                    raise RuntimeError("this vCenter's legacy /rest API does not list hosts") from exc
                raise
            if not isinstance(hosts, list):
                raise RuntimeError(f"unexpected host list from vCenter: {hosts!r}")

            now = datetime.utcnow()
            discovered = 0
            for h in hosts:
                node_name = h.get("name", h.get("host", "unknown"))
                # Scoped to this vCenter: two vCenters can each have a host called esxi01.
                existing = (
                    db.query(HypervisorNode)
                    .filter(HypervisorNode.connection_id == str(conn_id), HypervisorNode.node_name == node_name)
                    .first()
                )
                status = "online" if h.get("connection_state") == "CONNECTED" else "offline"

                vm_count = existing.vm_count if existing else 0
                if h.get("host"):
                    try:
                        vms = s.vms_on(h["host"])
                        if isinstance(vms, list):
                            vm_count = len(vms)
                    except httpx.HTTPError:
                        pass  # keep the last known count rather than report an empty host

                if existing:
                    existing.status = status
                    existing.vm_count = vm_count
                    existing.ip_address = _host_ip(node_name)
                    existing.last_seen_at = now
                else:
                    db.add(
                        HypervisorNode(
                            connection_id=str(conn_id),
                            node_name=node_name,
                            ip_address=_host_ip(node_name),
                            status=status,
                            vm_count=vm_count,
                            last_seen_at=now,
                        )
                    )
                    discovered += 1

        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return {
            "message": f"Discovered {discovered} new hosts ({len(hosts)} total)",
            "nodes_discovered": discovered,
        }
    except Exception as exc:
        return {"message": f"vSphere discovery failed: {exc}", "nodes_discovered": 0}
