"""ESXi host CPU, memory, storage and VM count over the vSphere VI/JSON API (vCenter 8.0U1+).

The vCenter REST API discovery uses has no host capacity, so the Infrastructure page
showed hosts with no vCPU or memory (seen on a real vCenter 8.0.3, 2026-10-15). Discovery
now reads them over VI/JSON with plain httpx. These pin:

- the release id comes from the appliance version ("8.0.3.00400" -> "8.0.3.0", then
  "8.0.1.0"); a vCenter without VI/JSON keeps the old result: None, never 0;
- the login sends the row's credentials as JSON, the session key is read from the
  response header and sent on every read, and the session is logged out;
- the column mapping (logical CPUs, percent busy, GiB) and the even split of a shared
  datastore, so summing hosts counts it once;
- one host's failed read leaves that host unreported and the others filled;
- ``vm_count`` counts powered-on VMs only, never a template, never the vCenter appliance.
"""

import json
import socket
import uuid
from types import SimpleNamespace

import httpx
import pytest
import respx
from app.hypervisor_backends import vsphere_vijson
from app.hypervisor_backends.vsphere import _vcenter_addresses, discover
from app.models import HypervisorConnection, HypervisorNode
from app.secretbox import seal

VC = "https://vcsa.range.test"
VC_IP = "10.20.0.5"
GIB = 1024**3
KEY = "vi-session-key"


@pytest.fixture(autouse=True)
def _vcenter_resolves(monkeypatch):
    """vcsa.range.test resolves to VC_IP; no real DNS in unit tests."""

    def fake(host, *a, **k):
        if host == "vcsa.range.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (VC_IP, 0))]
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _conn(db) -> HypervisorConnection:
    conn = HypervisorConnection(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="Range vCenter",
        hypervisor_type="vsphere",
        host="vcsa.range.test",
        port=443,
        username="svc-truenorth@vsphere.local",
        password_encrypted=seal("row-secret"),
    )
    db.add(conn)
    db.flush()
    return conn


def _summary(threads=48, cores=24, mhz=2500, mem_gib=512, cpu_used_mhz=12000, mem_used_mb=131072) -> dict:
    return {
        "_typeName": "HostListSummary",
        "hardware": {"numCpuCores": cores, "numCpuThreads": threads, "cpuMhz": mhz, "memorySize": mem_gib * GIB},
        "quickStats": {"overallCpuUsage": cpu_used_mhz, "overallMemoryUsage": mem_used_mb},
    }


def _ref(mo_type: str, moid: str) -> dict:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _ds(cap_gib: float, free_gib: float) -> dict:
    return {"_typeName": "DatastoreSummary", "capacity": int(cap_gib * GIB), "freeSpace": int(free_gib * GIB)}


def _vm(moid: str, power: str = "POWERED_ON", name: str | None = None) -> dict:
    return {"vm": moid, "name": name or moid, "power_state": power}


def _vm_summary(extension_key: str | None) -> dict:
    config = {"name": "x", "template": False}
    if extension_key:
        config["managedBy"] = {"_typeName": "ManagedByInfo", "extensionKey": extension_key, "type": "cluster-agent"}
    return {"_typeName": "VirtualMachineSummary", "config": config}


def _mock(router: respx.MockRouter, *, version: str = "8.0.3.00400", release: str = "8.0.3.0",
          host10_summary: httpx.Response | None = None, vcenter_vm: str | None = "vm-1",
          vm_summaries: dict[str, httpx.Response] | None = None) -> dict:
    """vCenter 8.0.3 with two hosts: each has a local datastore and both mount a shared one.

    host-10 runs the vCenter appliance (``vcenter_vm``) and two range VMs; host-11 one.
    vCenter is asked for powered-on VMs only, so a powered-off VM or template is not listed.
    """
    sdk = f"{VC}/sdk/vim25/{release}"
    router.post(f"{VC}/api/session").mock(return_value=httpx.Response(201, json="rest-tok"))
    router.delete(f"{VC}/api/session").mock(return_value=httpx.Response(204))
    router.get(f"{VC}/api/appliance/system/version").mock(return_value=httpx.Response(200, json={"version": version}))
    router.get(f"{VC}/api/vcenter/host").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"host": "host-10", "name": "10.20.0.11", "connection_state": "CONNECTED"},
                {"host": "host-11", "name": "10.20.0.12", "connection_state": "CONNECTED"},
            ],
        )
    )
    routes = {
        "vms10": router.get(f"{VC}/api/vcenter/vm", params={"hosts": "host-10", "power_states": "POWERED_ON"}).mock(
            return_value=httpx.Response(200, json=[_vm("vm-1"), _vm("vm-2"), _vm("vm-3")])
        ),
        "vms11": router.get(f"{VC}/api/vcenter/vm", params={"hosts": "host-11", "power_states": "POWERED_ON"}).mock(
            return_value=httpx.Response(200, json=[_vm("vm-4")])
        ),
        "content": router.get(f"{sdk}/ServiceInstance/ServiceInstance/content").mock(
            return_value=httpx.Response(
                200,
                json={
                    "sessionManager": _ref("SessionManager", "SessionManager"),
                    "searchIndex": _ref("SearchIndex", "SearchIndex"),
                },
            )
        ),
        "login": router.post(f"{sdk}/SessionManager/SessionManager/Login").mock(
            return_value=httpx.Response(200, json={"key": "ignored"}, headers={"vmware-api-session-id": KEY})
        ),
        "logout": router.post(f"{sdk}/SessionManager/SessionManager/Logout").mock(return_value=httpx.Response(204)),
        "by_dns": router.post(f"{sdk}/SearchIndex/SearchIndex/FindByDnsName").mock(
            return_value=httpx.Response(200, json=_ref("VirtualMachine", vcenter_vm)) if vcenter_vm
            else httpx.Response(200)
        ),
        "by_ip": router.post(f"{sdk}/SearchIndex/SearchIndex/FindByIp").mock(
            return_value=httpx.Response(200, json=_ref("VirtualMachine", vcenter_vm)) if vcenter_vm
            else httpx.Response(200)
        ),
        "host10": router.get(f"{sdk}/HostSystem/host-10/summary").mock(
            return_value=host10_summary or httpx.Response(200, json=_summary())
        ),
        "host11": router.get(f"{sdk}/HostSystem/host-11/summary").mock(
            return_value=httpx.Response(
                200, json=_summary(threads=32, cores=16, mhz=2000, mem_gib=256, cpu_used_mhz=0, mem_used_mb=0)
            )
        ),
        "shared": router.get(f"{sdk}/Datastore/datastore-99/summary").mock(
            return_value=httpx.Response(200, json=_ds(10000, 6000))
        ),
    }
    router.get(f"{sdk}/HostSystem/host-10/datastore").mock(
        return_value=httpx.Response(200, json=[_ref("Datastore", "datastore-1"), _ref("Datastore", "datastore-99")])
    )
    router.get(f"{sdk}/HostSystem/host-11/datastore").mock(
        return_value=httpx.Response(200, json=[_ref("Datastore", "datastore-2"), _ref("Datastore", "datastore-99")])
    )
    router.get(f"{sdk}/Datastore/datastore-1/summary").mock(return_value=httpx.Response(200, json=_ds(1000, 400)))
    router.get(f"{sdk}/Datastore/datastore-2/summary").mock(return_value=httpx.Response(200, json=_ds(500, 500)))
    for vm, response in (vm_summaries or {}).items():
        routes[f"summary:{vm}"] = router.get(f"{sdk}/VirtualMachine/{vm}/summary").mock(return_value=response)
    routes["vm_reads"] = router.get(url__regex=rf"^{VC}/sdk/vim25/[^/]+/VirtualMachine/").mock(
        return_value=httpx.Response(404)
    )
    # Any other VI/JSON read: not served (routes match in order, so this is the last resort).
    routes["other_sdk"] = router.route(url__startswith=f"{VC}/sdk/").mock(return_value=httpx.Response(404))
    return routes


def _nodes(db, conn) -> dict[str, HypervisorNode]:
    rows = db.query(HypervisorNode).filter(HypervisorNode.connection_id == conn.id).all()
    return {n.node_name: n for n in rows}


class TestReleaseId:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("8.0.3.00400", ["8.0.3.0", "8.0.1.0"]),
            ("8.0.3", ["8.0.3.0", "8.0.1.0"]),
            ("8.0.1.00100", ["8.0.1.0"]),
            ("9.0.0.0", ["9.0.0.0", "8.0.1.0"]),
            (None, ["8.0.1.0"]),
            ("7.0.3.01400", []),  # no VI/JSON before 8.0: nothing is tried
            ("6.7.0", []),
        ],
    )
    def test_release_candidates(self, version, expected):
        assert vsphere_vijson.release_candidates(version) == expected


class TestHostCapacity:
    def test_login_sends_row_credentials_and_reads_with_the_header_key(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            result = discover(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 2, result["message"]
        login = routes["login"].calls.last.request
        assert json.loads(login.content) == {"userName": "svc-truenorth@vsphere.local", "password": "row-secret"}
        assert "vmware-api-session-id" not in login.headers  # its own session, not the REST one
        assert routes["host10"].calls.last.request.headers["vmware-api-session-id"] == KEY
        assert routes["logout"].call_count == 1
        assert routes["logout"].calls.last.request.headers["vmware-api-session-id"] == KEY

    def test_cpu_memory_and_storage_mapping(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            discover(conn.id, conn, db_session)
        nodes = _nodes(db_session, conn)
        n = nodes["10.20.0.11"]

        assert n.cpu_total == 48  # numCpuThreads: logical CPUs
        assert n.cpu_used == 20.0  # 12000 MHz of 24 cores x 2500 MHz, as percent busy
        assert (n.memory_total_gb, n.memory_used_gb) == (512.0, 128.0)
        # local 1000 GiB (600 used) + half of the shared 10000 GiB (4000 used)
        assert (n.storage_total_gb, n.storage_used_gb) == (6000.0, 2600.0)
        assert routes["shared"].call_count == 1  # each datastore read once

        idle = nodes["10.20.0.12"]
        assert (idle.cpu_total, idle.cpu_used, idle.memory_used_gb) == (32, 0.0, 0.0)  # reported idle is 0

    def test_summing_hosts_counts_a_shared_datastore_once(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            _mock(router)
            discover(conn.id, conn, db_session)
        nodes = _nodes(db_session, conn).values()
        # 1000 + 500 local + 10000 shared, not 1000 + 500 + 2 x 10000
        assert sum(n.storage_total_gb for n in nodes) == 11500.0
        assert sum(n.cpu_total for n in nodes) == 80
        assert sum(n.memory_total_gb for n in nodes) == 768.0

    def test_an_unknown_derived_release_falls_back_to_8_0_1(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            unknown = router.get(f"{VC}/sdk/vim25/8.0.2.0/ServiceInstance/ServiceInstance/content").mock(
                return_value=httpx.Response(500, json={"message": "Unknown release ID"})
            )
            _mock(router, version="8.0.2.00100", release="8.0.1.0")
            discover(conn.id, conn, db_session)
        assert unknown.called
        assert _nodes(db_session, conn)["10.20.0.11"].cpu_total == 48

    @pytest.mark.parametrize(
        "content", [httpx.Response(404), httpx.Response(500, json={"message": "Unknown release ID"})]
    )
    def test_no_vi_json_leaves_capacity_unreported_not_zero(self, db_session, content):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            routes["content"].mock(return_value=content)  # 8.0.3.0; 8.0.1.0 is not served either
            result = discover(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 2, result["message"]
        for n in _nodes(db_session, conn).values():
            assert n.cpu_total is None and n.cpu_used is None
            assert n.memory_total_gb is None and n.memory_used_gb is None
            assert n.storage_total_gb is None and n.storage_used_gb is None
        assert not routes["login"].called

    def test_a_refused_vi_json_login_does_not_fail_discovery(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            routes["login"].mock(return_value=httpx.Response(500, json={"faultstring": "InvalidLogin"}))
            result = discover(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 2, result["message"]
        assert _nodes(db_session, conn)["10.20.0.11"].cpu_total is None
        assert not routes["host10"].called and not routes["logout"].called  # no session, nothing to end

    def test_a_host_whose_summary_fails_is_unreported_and_the_others_filled(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router, host10_summary=httpx.Response(500))
            discover(conn.id, conn, db_session)
        nodes = _nodes(db_session, conn)

        failed, ok = nodes["10.20.0.11"], nodes["10.20.0.12"]
        assert failed.cpu_total is None and failed.cpu_used is None and failed.memory_total_gb is None
        assert failed.storage_total_gb == 6000.0  # its datastore reads still worked
        assert (ok.cpu_total, ok.memory_total_gb, ok.storage_total_gb) == (32, 256.0, 5500.0)
        assert routes["logout"].called

    def test_a_failed_read_keeps_the_last_known_figures(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            _mock(router)
            discover(conn.id, conn, db_session)
        with respx.mock(assert_all_called=False) as router:
            _mock(router, host10_summary=httpx.Response(503))
            discover(conn.id, conn, db_session)
        assert _nodes(db_session, conn)["10.20.0.11"].cpu_total == 48

    def test_an_unreadable_datastore_leaves_storage_unreported_not_partial(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            routes["shared"].mock(return_value=httpx.Response(500))
            discover(conn.id, conn, db_session)
        nodes = _nodes(db_session, conn)
        assert all(n.storage_total_gb is None for n in nodes.values())
        assert nodes["10.20.0.11"].cpu_total == 48

    def test_the_password_and_session_key_stay_out_of_the_result(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            _mock(router, host10_summary=httpx.Response(500))
            result = discover(conn.id, conn, db_session)
        assert "row-secret" not in result["message"] and KEY not in result["message"]


class TestVmCount:
    def test_only_powered_on_vms_are_asked_for(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            discover(conn.id, conn, db_session)
        assert routes["vms10"].calls.last.request.url.params["power_states"] == "POWERED_ON"

    def test_powered_off_vms_and_templates_are_not_counted_even_if_listed(self, db_session):
        """A server that ignores the power filter still gives a powered-on count."""
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router, vcenter_vm=None)
            routes["vms11"].mock(
                return_value=httpx.Response(
                    200, json=[_vm("vm-4"), _vm("vm-5", "POWERED_OFF"), _vm("vm-6", "SUSPENDED"),
                               _vm("vm-7", "POWERED_OFF")]  # vm-7: a template is never powered on
                )
            )
            discover(conn.id, conn, db_session)
        assert _nodes(db_session, conn)["10.20.0.12"].vm_count == 1

    def test_the_vcenter_appliance_is_not_counted(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router, vcenter_vm="vm-1")
            discover(conn.id, conn, db_session)
        nodes = _nodes(db_session, conn)
        assert nodes["10.20.0.11"].vm_count == 2  # vm-2, vm-3; vm-1 is the vCenter
        assert nodes["10.20.0.12"].vm_count == 1  # others counted
        assert json.loads(routes["by_dns"].calls.last.request.content) == {
            "dnsName": "vcsa.range.test", "vmSearch": True,
        }
        assert json.loads(routes["by_ip"].calls.last.request.content) == {"ip": VC_IP, "vmSearch": True}

    def test_the_vcenter_appliance_is_matched_by_ip_when_its_guest_name_differs(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            routes["by_dns"].mock(return_value=httpx.Response(200))  # not found by name
            discover(conn.id, conn, db_session)
        assert _nodes(db_session, conn)["10.20.0.11"].vm_count == 2

    @pytest.mark.parametrize(
        ("host", "port", "expected"),
        [
            ("vcsa.range.test", 443, ("vcsa.range.test", [VC_IP])),
            (VC_IP, 443, (None, [VC_IP])),  # a row holding an address is searched by it alone
            ("fd00::10", 8443, (None, ["fd00::10"])),
            ("vc.unresolvable.test", 443, ("vc.unresolvable.test", [])),  # by name only
        ],
    )
    def test_vcenter_addresses(self, host, port, expected):
        assert _vcenter_addresses(SimpleNamespace(host=host, port=port)) == expected

    def test_without_vi_json_vm_counts_are_power_filtered_only(self, db_session):
        conn = _conn(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router)
            routes["content"].mock(return_value=httpx.Response(404))
            discover(conn.id, conn, db_session)
        assert _nodes(db_session, conn)["10.20.0.11"].vm_count == 3  # the vCenter cannot be told apart
        assert not routes["by_ip"].called


EAM = "com.vmware.vim.eam"
HOST10_VMS = [
    _vm("vm-1"),  # the vCenter appliance
    _vm("vm-2"),
    _vm("vm-3"),
    _vm("vm-50", name="vCLS-ffb74000-1111-2222-3333-444455556666"),
    _vm("vm-60", name="WIN11-TMPL"),  # a running VM named like a template: a VM, counted
]
HOST11_VMS = [_vm("vm-4"), _vm("vm-51", name="vcls-0a1b2c3d"), _vm("vm-52", name="vCLS-not-an-agent")]


class TestClusterServiceAgents:
    """vSphere Cluster Services (vCLS) agent VMs, one per host, are not range VMs."""

    def _discover(self, db_session, *, summaries=None, vi_json=True):
        conn = _conn(db_session)
        summaries = summaries or {
            "vm-50": httpx.Response(200, json=_vm_summary(EAM)),
            "vm-51": httpx.Response(200, json=_vm_summary(EAM)),
            "vm-52": httpx.Response(200, json=_vm_summary(None)),  # someone's VM, not EAM's
        }
        with respx.mock(assert_all_called=False) as router:
            routes = _mock(router, vm_summaries=summaries)
            routes["vms10"].mock(return_value=httpx.Response(200, json=HOST10_VMS))
            routes["vms11"].mock(return_value=httpx.Response(200, json=HOST11_VMS))
            if not vi_json:
                routes["content"].mock(return_value=httpx.Response(404))
            discover(conn.id, conn, db_session)
        return _nodes(db_session, conn), routes

    def test_eam_agents_are_not_counted_and_other_running_vms_are(self, db_session):
        nodes, routes = self._discover(db_session)
        assert nodes["10.20.0.11"].vm_count == 3  # vm-2, vm-3, WIN11-TMPL
        assert nodes["10.20.0.12"].vm_count == 2  # vm-4 and vCLS-not-an-agent (not managed by EAM)
        assert all(routes[f"summary:{vm}"].called for vm in ("vm-50", "vm-51", "vm-52"))
        assert not routes["vm_reads"].called  # only vCLS-named VMs are read

    def test_an_unreadable_agent_summary_falls_back_to_the_name(self, db_session):
        nodes, _ = self._discover(db_session, summaries={"vm-50": httpx.Response(500)})
        assert nodes["10.20.0.11"].vm_count == 3

    def test_without_vi_json_vcls_names_are_not_counted(self, db_session):
        nodes, routes = self._discover(db_session, vi_json=False)
        assert nodes["10.20.0.11"].vm_count == 4  # vm-1 (vCenter, not identifiable), vm-2, vm-3, WIN11-TMPL
        assert nodes["10.20.0.12"].vm_count == 1  # by name only
        assert not routes["summary:vm-52"].called
