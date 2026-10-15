"""vSphere connection backend — vCenter Automation REST API (the range platform).

Endpoint: ``https://{host}[:{port}]`` from the connection row; the port is left out when
it is 443, 8006 (the API's old Proxmox default) or unset, and a port already in the host
wins. Same rule as the worker's provisioner (see ``base_url``).

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
import socket
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from urllib.parse import urlparse

import httpx

from ..models import HypervisorConnection, HypervisorNode
from ..schemas import HypervisorTestResult
from ..secretbox import unseal
from . import vsphere_vijson
from .base import BaseHypervisorBackend

API = "api"  # /api, vSphere 7.0U2+
REST = "rest"  # legacy /rest only: vCenter before 7.0U2, vcsim
_SESSION_PATH = {API: "/api/session", REST: "/rest/com/vmware/cis/session"}
# The documented version read is /appliance/system/version in both dialects. The /api
# form falls back to /vcenter/system/version on a 404: older builds of tools/vcenter-sim
# serve only that.
_VERSION_PATH = {API: "/appliance/system/version", REST: "/appliance/system/version"}
_API_VERSION_FALLBACK = "/vcenter/system/version"
_VM_HOST_FILTER = {API: "hosts", REST: "filter.hosts"}
_VM_POWER_FILTER = {API: "power_states", REST: "filter.power_states"}


# Ports that mean "not given" on a vSphere row: 443 is the default, and 8006 (Proxmox's
# port) is what the API stored for any connection created without one. No vCenter listens
# on 8006. Same rule as the worker's provisioner (vsphere_api.endpoint_url), so the API and
# the worker reach the same URL for a row.
_NO_PORT = (0, 443, 8006)


def base_url(conn) -> str:
    """``https://host[:port]`` for a vSphere connection row.

    No port for 443, 8006 or unset (see ``_NO_PORT``). A port already in ``host`` wins; a
    host given with a scheme keeps it. A bare IPv6 address is bracketed.
    """
    host = str(conn.host or "").strip().rstrip("/")
    try:
        port = int(getattr(conn, "port", None) or 0)
    except (TypeError, ValueError):
        port = 0
    if "://" not in host:
        bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            if ipaddress.ip_address(bare).version == 6:
                host = f"[{bare}]"
        except ValueError:
            pass
        host = f"https://{host}"
    parsed = urlparse(host)
    if port in _NO_PORT or parsed.port is not None:
        return host
    return parsed._replace(netloc=f"{parsed.netloc}:{port}").geturl()


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

    def version(self):
        """The appliance version info; on /api a 404 retries the older sim-only path."""
        try:
            return self.get(_VERSION_PATH[self.dialect])
        except httpx.HTTPStatusError as exc:
            if self.dialect != API or exc.response.status_code != 404:
                raise
        return self.get(_API_VERSION_FALLBACK)

    def vms_on(self, host_id: str):
        """The host's powered-on VMs. A template cannot be powered on, so none is listed."""
        params = {_VM_HOST_FILTER[self.dialect]: host_id, _VM_POWER_FILTER[self.dialect]: "POWERED_ON"}
        return self.get("/vcenter/vm", params=params)


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
            # A failed logout does not change the result: vCenter expires idle sessions itself.
            with suppress(httpx.HTTPError):
                client.delete(_SESSION_PATH[dialect], headers=s.headers)


class VSphereBackend(BaseHypervisorBackend):
    kind = "vsphere"
    requires_password = True
    provisioner_backends = frozenset({"vsphere_api"})

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
                ver_info = s.version()
                hosts = s.get("/vcenter/host")
            else:
                # The login is the test. The legacy version and host reads are not served
                # everywhere (vcsim has neither), so their absence is reported, not fatal.
                ver_info, hosts, missing = {}, [], []
                try:
                    ver_info = s.version() or {}
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
        # Reported, not deactivated: a failed test must not move ranges off it (#131 review 2).
        db.commit()
        return HypervisorTestResult(success=False, message=f"Connection failed: {exc}", version="n/a", nodes_found=0)


def _host_ip(name: str) -> str | None:
    """An ESXi host's inventory name is its IP when it was added by address, else an FQDN."""
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return None


def _vcenter_addresses(conn) -> tuple[str | None, list[str]]:
    """The vCenter's DNS name (None when the row holds an address) and its IP addresses."""
    host = urlparse(base_url(conn)).hostname or ""
    try:
        return None, [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        ips = sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except (OSError, UnicodeError):
        ips = []  # unresolvable here: matched by its DNS name only
    return host, ips


def _vijson_read(s: _Session, conn, password: str, host_ids: list) -> tuple[dict[str, dict], set[str]]:
    """Over VI/JSON: host CPU/memory/storage columns by host id, and the vCenter's own VM ids.

    Both are empty when VI/JSON is unavailable; then hosts keep their figures as they were
    and VM counts are filtered by power state only. Best effort: only the /api dialect
    (vCenter 7.0U2+) can have VI/JSON, the release id comes from the appliance version,
    and no failure here fails discovery.
    """
    if s.dialect != API:
        return {}, set()
    try:
        ver = s.version()
    except (httpx.HTTPError, ValueError):  # ValueError: a body that is not JSON
        ver = None
    version = ver.get("version") if isinstance(ver, dict) else None
    vi = vsphere_vijson.login(s.client, version, conn.username, password)
    if vi is None:
        return {}, set()
    try:
        name, ips = _vcenter_addresses(conn)
        return vsphere_vijson.host_capacity(vi, host_ids), vsphere_vijson.appliance_vms(vi, name, ips)
    finally:
        vi.logout()


def discover(conn_id, conn: HypervisorConnection, db) -> dict:
    """Discover ESXi hosts via the vCenter Automation REST API.

    That API reports each host's name and connection state, and lists VMs per host, but
    has no host CPU, memory or storage figures. On the /api dialect those are read over
    the VI/JSON API (``vsphere_vijson``, vCenter 8.0U1+). Where that is unavailable, or a
    host's read fails, they are left as they were (None, "not reported" on a new host),
    never 0, which would read as an idle host.

    ``vm_count`` is the host's powered-on VMs: not powered-off ones, not templates (which
    cannot be powered on), and not the vCenter appliance itself when VI/JSON can identify
    it (``vsphere_vijson.appliance_vms``). Without VI/JSON only the power filter applies.
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

            capacity, appliance = _vijson_read(s, conn, password, [h.get("host") for h in hosts])
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
                            # Powered on (asked of vCenter, checked again here in case a
                            # server ignores the filter), and not the vCenter appliance.
                            vm_count = sum(
                                1 for v in vms
                                if v.get("power_state", "POWERED_ON") == "POWERED_ON"
                                and v.get("vm") not in appliance
                            )
                    except httpx.HTTPError:
                        pass  # keep the last known count rather than report an empty host

                figures = capacity.get(h.get("host"), {})
                if existing:
                    existing.status = status
                    existing.vm_count = vm_count
                    existing.ip_address = _host_ip(node_name)
                    existing.last_seen_at = now
                    for column, value in figures.items():
                        setattr(existing, column, value)
                else:
                    db.add(
                        HypervisorNode(
                            connection_id=str(conn_id),
                            node_name=node_name,
                            ip_address=_host_ip(node_name),
                            status=status,
                            vm_count=vm_count,
                            last_seen_at=now,
                            **figures,
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
