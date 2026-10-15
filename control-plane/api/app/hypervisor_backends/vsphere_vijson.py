"""ESXi host capacity over the vSphere VI/JSON API (vCenter 8.0U1+), plain httpx.

The vCenter Automation REST API that ``vsphere.py`` discovers hosts with has no host CPU,
memory or storage figures. VI/JSON exposes the same managed objects as the SOAP API
(and pyVmomi) as JSON over HTTPS, so the API can read them without a vendor SDK (ADR 0001
keeps pyVmomi in the worker's provisioners).

    GET  /sdk/vim25/{release}/ServiceInstance/ServiceInstance/content   (no session needed)
    POST /sdk/vim25/{release}/SessionManager/{moid}/Login   {"userName", "password"}
         -> session key in the ``vmware-api-session-id`` response header
    GET  /sdk/vim25/{release}/HostSystem/{moid}/summary      hardware + quickStats
    GET  /sdk/vim25/{release}/HostSystem/{moid}/datastore    Datastore references
    GET  /sdk/vim25/{release}/Datastore/{moid}/summary       capacity, freeSpace (bytes)
    POST /sdk/vim25/{release}/SearchIndex/{moid}/FindByDnsName | FindByIp   the vCenter's VM
    POST /sdk/vim25/{release}/SessionManager/{moid}/Logout

``release`` must be a release vCenter knows ("release" or a made-up id is a 500 "Unknown
release ID"), so it is derived from the appliance version: "8.0.3.00400" -> "8.0.3.0",
then "8.0.1.0" (the first release with VI/JSON). Before 8.0 there is no VI/JSON and
nothing is tried.

Everything here is best effort. When VI/JSON is unavailable (older vCenter, vcsim,
tools/vcenter-sim) or a read fails, the figure is left out, never reported as 0, which
would read as an idle or empty host.

Mapping (column semantics match the Proxmox backend and the Infrastructure page):

- ``cpu_total`` = ``hardware.numCpuThreads``: logical CPUs, what a vCPU is scheduled on
  and what Proxmox's ``maxcpu`` reports. The capacity service multiplies it by
  ``CAPACITY_VCPU_RATIO``.
- ``cpu_used`` = percent busy (0-100): ``quickStats.overallCpuUsage`` (MHz) over
  ``hardware.cpuMhz * hardware.numCpuCores`` (the host's physical MHz).
- ``memory_total_gb`` / ``memory_used_gb``: ``hardware.memorySize`` (bytes) and
  ``quickStats.overallMemoryUsage`` (MB), in GiB.
- ``storage_total_gb`` / ``storage_used_gb``: the host's datastores, each shared one split
  evenly across the discovered hosts that mount it. Summing hosts (``/hypervisors/summary``,
  the capacity service) then counts a shared datastore once, and every host that mounts
  storage still reports a non-zero figure. A host whose datastore list or any of whose
  datastore summaries cannot be read reports no storage figure at all (a partial sum would
  understate it).

The vCenter appliance is not a range VM, so discovery leaves it out of a host's
``vm_count``. It is the VM whose guest (VMware Tools) reports the connection's DNS name or
one of the addresses that name resolves to: ``SearchIndex.FindByDnsName`` /
``FindByIp`` with ``vmSearch``, which match the same ``guest.hostName`` and guest IP
addresses that ``summary.guest`` reports, in one call each instead of a summary read per
VM (a range cluster runs thousands).

The password and session key are never logged or put in an error message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import suppress
from urllib.parse import quote

import httpx

SESSION_HEADER = "vmware-api-session-id"
FIRST_RELEASE = "8.0.1.0"  # VI/JSON shipped with vCenter 8.0 Update 1
GIB = 1024**3

# What can go wrong reading VI/JSON: HTTP and transport errors, a non-JSON body (ValueError)
# or a body of an unexpected shape. None of them may fail host discovery.
_READ_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, ZeroDivisionError)

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


def release_candidates(version: str | None) -> list[str]:
    """VI/JSON release ids to try for an appliance version, best first; [] before 8.0."""
    m = _VERSION_RE.match(version or "")
    if not m:
        return [FIRST_RELEASE]  # version not reported: try the one every 8.0U1+ serves
    major, minor, update = (int(x) for x in m.groups())
    if major < 8:
        return []
    return list(dict.fromkeys([f"{major}.{minor}.{update}.0", FIRST_RELEASE]))


class ViJson:
    """A logged-in VI/JSON session on an existing httpx client (same base URL, TLS, timeout)."""

    def __init__(
        self, client: httpx.Client, release: str, session_manager: str, key: str, search_index: str | None = None
    ) -> None:
        self.client = client
        self.prefix = f"/sdk/vim25/{release}"
        self.session_manager = session_manager
        self.search_index = search_index
        self._headers = {SESSION_HEADER: key}

    def get(self, mo_type: str, moid: str, prop: str):
        r = self.client.get(f"{self.prefix}/{mo_type}/{quote(moid, safe='')}/{prop}", headers=self._headers)
        r.raise_for_status()
        return r.json()

    def call(self, mo_type: str, moid: str, method: str, body: dict):
        """Invoke a method; its return value, or None for an empty (unset) result."""
        r = self.client.post(
            f"{self.prefix}/{mo_type}/{quote(moid, safe='')}/{method}", json=body, headers=self._headers
        )
        r.raise_for_status()
        return r.json() if r.content else None

    def logout(self) -> None:
        # A failed logout does not change the result: vCenter expires idle sessions itself.
        with suppress(httpx.HTTPError):
            self.client.post(
                f"{self.prefix}/SessionManager/{quote(self.session_manager, safe='')}/Logout",
                headers=self._headers,
            )


def login(client: httpx.Client, version: str | None, username: str, password: str) -> ViJson | None:
    """Find a release this vCenter serves and log in; None when VI/JSON is unavailable."""
    for release in release_candidates(version):
        prefix = f"/sdk/vim25/{release}"
        try:
            r = client.get(f"{prefix}/ServiceInstance/ServiceInstance/content")
            if r.status_code != 200:
                continue  # 404: no VI/JSON; 500 "Unknown release ID": try the next release
            content = r.json()
            sm = content["sessionManager"]["value"]
            r = client.post(
                f"{prefix}/SessionManager/{quote(sm, safe='')}/Login",
                json={"userName": username, "password": password},
            )
            if r.status_code != 200:
                return None  # the release is right; a refused login is not retried
            key = r.headers.get(SESSION_HEADER)
            if not key:
                return None
            search_index = (content.get("searchIndex") or {}).get("value")
            return ViJson(client, release, sm, key, search_index)
        except _READ_ERRORS:
            continue
    return None


def _num(d: dict | None, key: str) -> float | None:
    v = (d or {}).get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def host_figures(summary: dict) -> dict:
    """CPU and memory columns from a HostListSummary; a field vCenter leaves out is None."""
    hw = summary.get("hardware") or {}
    qs = summary.get("quickStats") or {}
    threads, cores, mhz = _num(hw, "numCpuThreads"), _num(hw, "numCpuCores"), _num(hw, "cpuMhz")
    used_mhz, mem_bytes, used_mb = _num(qs, "overallCpuUsage"), _num(hw, "memorySize"), _num(qs, "overallMemoryUsage")
    cpu_pct = None
    if used_mhz is not None and mhz and cores:
        cpu_pct = round(min(used_mhz / (mhz * cores) * 100, 100.0), 1)
    return {
        "cpu_total": int(threads) if threads else None,
        "cpu_used": cpu_pct,
        "memory_total_gb": round(mem_bytes / GIB, 1) if mem_bytes else None,
        "memory_used_gb": round(used_mb / 1024, 1) if used_mb is not None else None,
    }


def _refs(body) -> list[str]:
    """Managed object ids from a VI/JSON reference list (``[{"type", "value"}, ...]``)."""
    if not isinstance(body, list):
        raise TypeError("expected a list of managed object references")
    return [ref["value"] for ref in body]


def host_capacity(vi: ViJson, host_ids: Iterable[str]) -> dict[str, dict]:
    """``{host moid: {column: value}}`` for what could be read; a host or column missing
    from the result was not reported (leave the row's value as it is)."""
    host_ids = list(dict.fromkeys(h for h in host_ids if h))
    out: dict[str, dict] = {h: {} for h in host_ids}

    mounts: dict[str, list[str]] = {}
    for h in host_ids:
        with suppress(*_READ_ERRORS):
            out[h].update(host_figures(vi.get("HostSystem", h, "summary")))
        with suppress(*_READ_ERRORS):
            mounts[h] = _refs(vi.get("HostSystem", h, "datastore"))

    # Each datastore is read once and split across the hosts (that we could list) mounting it.
    sharers: dict[str, int] = {}
    for ds_ids in mounts.values():
        for ds in set(ds_ids):
            sharers[ds] = sharers.get(ds, 0) + 1
    sizes: dict[str, tuple[float, float] | None] = {}
    for ds in sharers:
        try:
            s = vi.get("Datastore", ds, "summary")
            cap, free = _num(s, "capacity"), _num(s, "freeSpace")
            sizes[ds] = (cap, cap - free) if cap is not None and free is not None else None
        except _READ_ERRORS:
            sizes[ds] = None

    for h, ds_ids in mounts.items():
        mine = {ds: sizes[ds] for ds in set(ds_ids)}
        if any(v is None for v in mine.values()):
            continue
        total = sum(cap / sharers[ds] for ds, (cap, _) in mine.items())
        used = sum(u / sharers[ds] for ds, (_, u) in mine.items())
        out[h]["storage_total_gb"] = round(total / GIB, 1)
        out[h]["storage_used_gb"] = round(used / GIB, 1)
    return {h: v for h, v in out.items() if v}


def appliance_vms(vi: ViJson, dns_name: str | None, ips: Iterable[str]) -> set[str]:
    """Ids of the VM(s) whose guest reports the vCenter's DNS name or one of its addresses."""
    if not vi.search_index:
        return set()
    lookups = [("FindByIp", {"ip": ip, "vmSearch": True}) for ip in ips]
    if dns_name:
        lookups.insert(0, ("FindByDnsName", {"dnsName": dns_name, "vmSearch": True}))
    found: set[str] = set()
    for method, body in lookups:
        with suppress(*_READ_ERRORS):
            ref = vi.call("SearchIndex", vi.search_index, method, body)
            if isinstance(ref, dict) and ref.get("type", "VirtualMachine") == "VirtualMachine" and ref.get("value"):
                found.add(ref["value"])
    return found
