"""vSphere is the range platform. These cover what the dashboard relies on.

- Discovery against a (mocked) vCenter records each ESXi host with its real VM count,
  keeps the vCenter's address out of the host's IP, and leaves CPU/memory empty —
  the vCenter REST API does not report them, and 0 would read as an idle host.
- ``GET /hypervisors/nodes`` is readable by instructors (INFRA_READ) for the dashboard,
  while every connection route — hosts, usernames, stored credentials — stays INFRA_WRITE.
"""

import base64
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
import respx
from app.auth import CurrentUser, get_current_user
from app.hypervisor_backends.vsphere import base_url as vsphere_base_url
from app.hypervisor_backends.vsphere import check_connection as check_vsphere
from app.hypervisor_backends.vsphere import discover as _discover_vsphere
from app.main import app as fastapi_app
from app.models import HypervisorConnection, HypervisorNode, UserRole
from app.secretbox import seal

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
VCENTER = "https://vcsa.range.test"


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=DEV_TENANT,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _vcenter(db, tenant: str = DEV_TENANT) -> HypervisorConnection:
    conn = HypervisorConnection(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant),
        name="Range vCenter",
        hypervisor_type="vsphere",
        host="vcsa.range.test",
        port=443,
        username="svc-truenorth@vsphere.local",
        password_encrypted="x",
    )
    db.add(conn)
    db.flush()
    return conn


def _mock_vcenter(router: respx.MockRouter, *, vms_for_host2: httpx.Response | None = None) -> None:
    router.post(f"{VCENTER}/api/session").mock(return_value=httpx.Response(201, json="tok"))
    router.delete(f"{VCENTER}/api/session").mock(return_value=httpx.Response(204))
    router.get(f"{VCENTER}/api/vcenter/host").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"host": "host-1", "name": "10.20.0.11", "connection_state": "CONNECTED", "power_state": "POWERED_ON"},
                {"host": "host-2", "name": "esxi02.range.test", "connection_state": "DISCONNECTED"},
            ],
        )
    )
    router.get(f"{VCENTER}/api/vcenter/vm", params={"hosts": "host-1"}).mock(
        return_value=httpx.Response(200, json=[{"vm": f"vm-{i}"} for i in range(3)])
    )
    router.get(f"{VCENTER}/api/vcenter/vm", params={"hosts": "host-2"}).mock(
        return_value=vms_for_host2 or httpx.Response(200, json=[{"vm": "vm-9"}])
    )


def _nodes(db, conn) -> dict[str, HypervisorNode]:
    rows = db.query(HypervisorNode).filter(HypervisorNode.connection_id == conn.id).all()
    return {n.node_name: n for n in rows}


class TestVsphereDiscovery:
    def test_records_hosts_with_vm_counts(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock(assert_all_called=True) as router:
            _mock_vcenter(router)
            result = _discover_vsphere(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 2
        nodes = _nodes(db_session, conn)
        assert nodes["10.20.0.11"].vm_count == 3
        assert nodes["10.20.0.11"].status == "online"
        assert nodes["esxi02.range.test"].vm_count == 1
        assert nodes["esxi02.range.test"].status == "offline"
        assert nodes["10.20.0.11"].last_seen_at is not None

    def test_host_ip_is_never_the_vcenter_address(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock() as router:
            _mock_vcenter(router)
            _discover_vsphere(conn.id, conn, db_session)
        nodes = _nodes(db_session, conn)
        assert nodes["10.20.0.11"].ip_address == "10.20.0.11"  # added to vCenter by address
        assert nodes["esxi02.range.test"].ip_address is None  # added by name: unknown, not vCenter's
        assert all(n.ip_address != conn.host for n in nodes.values())

    def test_cpu_and_memory_are_not_reported_rather_than_zero(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock() as router:
            _mock_vcenter(router)
            _discover_vsphere(conn.id, conn, db_session)
        for n in _nodes(db_session, conn).values():
            assert n.cpu_total is None and n.cpu_used is None
            assert n.memory_total_gb is None and n.memory_used_gb is None

    def test_rediscovery_updates_in_place_and_keeps_count_on_a_failed_vm_query(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock() as router:
            _mock_vcenter(router)
            _discover_vsphere(conn.id, conn, db_session)
        with respx.mock() as router:
            _mock_vcenter(router, vms_for_host2=httpx.Response(503))
            result = _discover_vsphere(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 0
        nodes = _nodes(db_session, conn)
        assert len(nodes) == 2
        assert nodes["esxi02.range.test"].vm_count == 1  # last known, not 0

    def test_same_host_name_under_another_vcenter_is_not_overwritten(self, db_session):
        other = _vcenter(db_session)
        db_session.add(HypervisorNode(connection_id=other.id, node_name="10.20.0.11", status="online", vm_count=42))
        db_session.flush()

        conn = _vcenter(db_session)
        with respx.mock() as router:
            _mock_vcenter(router)
            _discover_vsphere(conn.id, conn, db_session)

        assert _nodes(db_session, other)["10.20.0.11"].vm_count == 42
        assert _nodes(db_session, conn)["10.20.0.11"].vm_count == 3


LEGACY = "https://vcsa-old.range.test:8443"
HOSTS = [
    {"host": "host-1", "name": "10.20.0.11", "connection_state": "CONNECTED"},
    {"host": "host-2", "name": "esxi02.range.test", "connection_state": "DISCONNECTED"},
]


def _legacy_vcenter(db, *, password: str = "row-secret") -> HypervisorConnection:
    conn = _vcenter(db)
    conn.host, conn.port = "vcsa-old.range.test", 8443
    conn.username, conn.password_encrypted = "row-user@vsphere.local", seal(password)
    db.flush()
    return conn


def _mock_legacy(router: respx.MockRouter, *, hosts: httpx.Response | None = None) -> dict:
    """A vCenter without /api (pre-7.0U2, vcsim): every read is /rest, wrapped in {"value": ...}."""
    routes = {
        "api_login": router.post(f"{LEGACY}/api/session").mock(return_value=httpx.Response(404)),
        "login": router.post(f"{LEGACY}/rest/com/vmware/cis/session").mock(
            return_value=httpx.Response(200, json={"value": "legacy-tok"})
        ),
        "logout": router.delete(f"{LEGACY}/rest/com/vmware/cis/session").mock(return_value=httpx.Response(200)),
        "version": router.get(f"{LEGACY}/rest/appliance/system/version").mock(
            return_value=httpx.Response(200, json={"value": {"version": "6.7.0"}})
        ),
        "hosts": router.get(f"{LEGACY}/rest/vcenter/host").mock(
            return_value=hosts or httpx.Response(200, json={"value": HOSTS})
        ),
    }
    router.get(f"{LEGACY}/rest/vcenter/vm", params={"filter.hosts": "host-1"}).mock(
        return_value=httpx.Response(200, json={"value": [{"vm": f"vm-{i}"} for i in range(3)]})
    )
    router.get(f"{LEGACY}/rest/vcenter/vm", params={"filter.hosts": "host-2"}).mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    return routes


class TestVsphereEndpointAndDialect:
    """The connection's port is honoured; /api first, legacy /rest on a 404; always log out."""

    @pytest.mark.parametrize(
        ("host", "port", "url"),
        [
            ("vcsa.range.test", 443, "https://vcsa.range.test"),
            ("vcsa.range.test", None, "https://vcsa.range.test"),
            ("vcsa.range.test", 8443, "https://vcsa.range.test:8443"),
            # 8006 is the API's old default (Proxmox's port) on rows created without one
            ("vcsa.range.test", 8006, "https://vcsa.range.test"),
            ("vcsa.range.test", "8006", "https://vcsa.range.test"),
            ("vcsa.range.test", "not-a-port", "https://vcsa.range.test"),
            ("127.0.0.1:8989", None, "https://127.0.0.1:8989"),  # host already carries a port
            ("127.0.0.1:8989", 8443, "https://127.0.0.1:8989"),  # ...and it wins
            ("https://vcsa.range.test:9443/", 443, "https://vcsa.range.test:9443"),
            ("https://vcsa.range.test", 8443, "https://vcsa.range.test:8443"),
            ("fd00::10", 8443, "https://[fd00::10]:8443"),
            ("fd00::10", 8006, "https://[fd00::10]"),
        ],
    )
    def test_base_url(self, host, port, url):
        assert vsphere_base_url(SimpleNamespace(host=host, port=port)) == url

    def test_a_row_stored_with_8006_connects_on_443(self, db_session):
        conn = _vcenter(db_session)
        conn.port = 8006  # what the API stored when a vSphere connection was created without a port
        with respx.mock(assert_all_called=True) as router:
            _mock_vcenter(router)
            result = _discover_vsphere(conn.id, conn, db_session)
        assert result["nodes_discovered"] == 2, result["message"]

    def test_same_url_as_the_worker_for_a_row(self):
        """The worker's provisioner builds its endpoint with the same rule, when it has one."""
        worker = pytest.importorskip("worker.provisioners.vsphere_api")
        endpoint_url = getattr(worker, "endpoint_url", None)
        if endpoint_url is None:
            pytest.skip("worker has no endpoint_url on this branch (claude/vsphere-tenant-creds-hardening)")
        for host, port in [("vcsa.range.test", 443), ("vcsa.range.test", None), ("vcsa.range.test", 8006),
                           ("vcsa.range.test", 8443), ("10.0.0.5:9443", 8443), ("https://vc.test", 8443)]:
            assert vsphere_base_url(SimpleNamespace(host=host, port=port)) == endpoint_url(host, port), (host, port)

    def test_api_dialect_uses_the_port_and_logs_out(self, db_session):
        conn = _vcenter(db_session)
        conn.port = 8443
        base = "https://vcsa.range.test:8443"
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{base}/api/session").mock(return_value=httpx.Response(201, json="tok"))
            router.get(f"{base}/api/appliance/system/version").mock(
                return_value=httpx.Response(200, json={"version": "8.0.3"})
            )
            router.get(f"{base}/api/vcenter/host").mock(return_value=httpx.Response(200, json=HOSTS))
            logout = router.delete(f"{base}/api/session").mock(return_value=httpx.Response(204))
            result = check_vsphere(conn, db_session)

        assert result.success, result.message
        assert (result.version, result.nodes_found) == ("8.0.3", 2)
        assert logout.calls.last.request.headers["vmware-api-session-id"] == "tok"
        assert conn.is_active is True

    def test_api_version_falls_back_to_the_old_sim_path_on_404(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{VCENTER}/api/session").mock(return_value=httpx.Response(201, json="tok"))
            documented = router.get(f"{VCENTER}/api/appliance/system/version").mock(
                return_value=httpx.Response(404)
            )
            old = router.get(f"{VCENTER}/api/vcenter/system/version").mock(
                return_value=httpx.Response(200, json={"version": "8.0.3"})
            )
            router.get(f"{VCENTER}/api/vcenter/host").mock(return_value=httpx.Response(200, json=HOSTS))
            router.delete(f"{VCENTER}/api/session").mock(return_value=httpx.Response(204))
            result = check_vsphere(conn, db_session)

        assert result.success, result.message
        assert result.version == "8.0.3"
        assert documented.call_count == 1 and old.call_count == 1  # documented path asked first

    def test_api_version_error_other_than_404_does_not_fall_back(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{VCENTER}/api/session").mock(return_value=httpx.Response(201, json="tok"))
            router.get(f"{VCENTER}/api/appliance/system/version").mock(return_value=httpx.Response(403))
            old = router.get(f"{VCENTER}/api/vcenter/system/version").mock(
                return_value=httpx.Response(200, json={"version": "8.0.3"})
            )
            router.delete(f"{VCENTER}/api/session").mock(return_value=httpx.Response(204))
            result = check_vsphere(conn, db_session)

        assert not result.success and "403" in result.message
        assert not old.called

    def test_api_404_falls_back_to_rest_with_the_rows_credentials(self, db_session):
        conn = _legacy_vcenter(db_session)
        with respx.mock(assert_all_called=False) as router:  # the per-host VM reads are discovery's
            routes = _mock_legacy(router)
            result = check_vsphere(conn, db_session)

        assert result.success, result.message
        assert (result.version, result.nodes_found) == ("6.7.0", 2)
        assert "legacy /rest" in result.message
        login = routes["login"].calls.last.request
        assert login.headers["authorization"] == "Basic " + base64.b64encode(b"row-user@vsphere.local:row-secret").decode()
        assert routes["hosts"].calls.last.request.headers["vmware-api-session-id"] == "legacy-tok"
        assert routes["logout"].calls.last.request.headers["vmware-api-session-id"] == "legacy-tok"

    def test_rest_without_version_or_host_reads_still_connects_and_says_so(self, db_session):
        conn = _legacy_vcenter(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock_legacy(router, hosts=httpx.Response(404))
            routes["version"].mock(return_value=httpx.Response(404))
            result = check_vsphere(conn, db_session)

        assert result.success, result.message
        assert result.version == "unknown" and result.nodes_found == 0
        assert "version and hosts not reported" in result.message
        assert routes["logout"].called

    def test_bad_credentials_fail_and_do_not_try_the_legacy_login(self, db_session):
        conn = _legacy_vcenter(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock_legacy(router)
            routes["api_login"].mock(return_value=httpx.Response(401))
            result = check_vsphere(conn, db_session)

        assert not result.success and "401" in result.message
        assert not routes["login"].called
        assert conn.is_active is False

    def test_session_is_deleted_when_a_read_fails(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock() as router:
            router.post(f"{VCENTER}/api/session").mock(return_value=httpx.Response(201, json="tok"))
            router.get(f"{VCENTER}/api/appliance/system/version").mock(return_value=httpx.Response(503))
            logout = router.delete(f"{VCENTER}/api/session").mock(return_value=httpx.Response(204))
            result = check_vsphere(conn, db_session)

        assert not result.success
        assert logout.called

    def test_discovery_over_rest_records_hosts_and_logs_out(self, db_session):
        conn = _legacy_vcenter(db_session)
        with respx.mock(assert_all_called=False) as router:  # discovery does not read the version
            routes = _mock_legacy(router)
            result = _discover_vsphere(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 2, result["message"]
        nodes = _nodes(db_session, conn)
        assert nodes["10.20.0.11"].vm_count == 3 and nodes["10.20.0.11"].status == "online"
        assert nodes["esxi02.range.test"].vm_count == 0 and nodes["esxi02.range.test"].status == "offline"
        assert routes["logout"].called

    def test_discovery_over_rest_without_a_host_list_fails_loudly(self, db_session):
        conn = _legacy_vcenter(db_session)
        with respx.mock(assert_all_called=False) as router:
            routes = _mock_legacy(router, hosts=httpx.Response(404))
            result = _discover_vsphere(conn.id, conn, db_session)

        assert result["nodes_discovered"] == 0
        assert "does not list hosts" in result["message"]
        assert _nodes(db_session, conn) == {}
        assert routes["logout"].called

    def test_discovery_logs_out_on_the_api_dialect(self, db_session):
        conn = _vcenter(db_session)
        with respx.mock() as router:
            _mock_vcenter(router)
            _discover_vsphere(conn.id, conn, db_session)
            deletes = [c.request for c in router.calls if c.request.method == "DELETE"]
        assert [str(r.url) for r in deletes] == [f"{VCENTER}/api/session"]
        assert deletes[0].headers["vmware-api-session-id"] == "tok"


class TestHypervisorPermissions:
    def test_instructor_can_read_the_host_inventory(self, client, db_session):
        conn = _vcenter(db_session)
        db_session.add(HypervisorNode(connection_id=conn.id, node_name="esxi01", status="online", vm_count=5))
        db_session.flush()
        with acting_as(UserRole.instructor):
            r = client.get("/hypervisors/nodes")
            assert r.status_code == 200, r.text
            row = next(n for n in r.json() if n["node_name"] == "esxi01")
            assert row["vm_count"] == 5
            assert row["cpu_total"] is None
            assert client.get("/hypervisors/summary").status_code == 200

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/hypervisors/connections"),
            ("post", "/hypervisors/connections"),
            ("get", "/hypervisors/connections/{id}"),
            ("patch", "/hypervisors/connections/{id}"),
            ("delete", "/hypervisors/connections/{id}"),
            ("post", "/hypervisors/connections/{id}/test"),
            ("post", "/hypervisors/connections/{id}/discover"),
            ("get", "/hypervisors/connections/{id}/nodes"),
            ("get", "/hypervisors/connections/{id}/pools"),
            ("post", "/hypervisors/connections/{id}/set-primary"),
        ],
    )
    def test_instructor_cannot_touch_connections(self, client, db_session, method, path):
        conn = _vcenter(db_session)
        with acting_as(UserRole.instructor):
            kwargs = {"json": {}} if method in ("post", "patch") else {}
            r = getattr(client, method)(path.format(id=conn.id), **kwargs)
        assert r.status_code == 403, (method, path, r.status_code)

    def test_student_cannot_read_the_inventory(self, client):
        with acting_as(UserRole.student):
            assert client.get("/hypervisors/nodes").status_code == 403

    def test_range_ops_can_list_connections(self, client):
        with acting_as(UserRole.range_ops):
            assert client.get("/hypervisors/connections").status_code == 200


OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


class TestHypervisorTenancy:
    """Connections are tenant-owned. Until 2026-10-07 every by-id route used ``db.get``,
    so one tenant's range_ops could read, repoint or delete another tenant's vCenter."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/hypervisors/connections/{id}"),
            ("patch", "/hypervisors/connections/{id}"),
            ("delete", "/hypervisors/connections/{id}"),
            ("post", "/hypervisors/connections/{id}/test"),
            ("post", "/hypervisors/connections/{id}/discover"),
            ("get", "/hypervisors/connections/{id}/nodes"),
            ("get", "/hypervisors/connections/{id}/pools"),
            ("post", "/hypervisors/connections/{id}/set-primary"),
        ],
    )
    def test_foreign_connection_is_404(self, client, db_session, method, path):
        theirs = _vcenter(db_session, OTHER_TENANT)
        db_session.commit()
        kwargs = {"json": {"name": "pwned"}} if method == "patch" else {}
        r = getattr(client, method)(path.format(id=theirs.id), **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)
        db_session.refresh(theirs)
        assert theirs.name == "Range vCenter"

    def test_own_connection_is_200(self, client, db_session):
        mine = _vcenter(db_session)
        db_session.commit()
        assert client.get(f"/hypervisors/connections/{mine.id}").status_code == 200
        r = client.patch(f"/hypervisors/connections/{mine.id}", json={"name": "Renamed"})
        assert r.status_code == 200 and r.json()["name"] == "Renamed"
        assert client.get(f"/hypervisors/connections/{mine.id}/nodes").status_code == 200

    def test_lists_inventory_and_summary_exclude_other_tenants(self, client, db_session):
        mine = _vcenter(db_session)
        theirs = _vcenter(db_session, OTHER_TENANT)
        db_session.add(HypervisorNode(connection_id=mine.id, node_name="mine-esx", status="online", vm_count=1))
        db_session.add(HypervisorNode(connection_id=theirs.id, node_name="their-esx", status="online", vm_count=9))
        db_session.commit()
        ids = {c["id"] for c in client.get("/hypervisors/connections").json()}
        assert str(mine.id) in ids and str(theirs.id) not in ids
        names = {n["node_name"] for n in client.get("/hypervisors/nodes").json()}
        assert "mine-esx" in names and "their-esx" not in names
        summary = client.get("/hypervisors/summary").json()
        assert summary["total_connections"] == len(ids)

    def test_create_stamps_tenant_and_set_primary_stays_in_tenant(self, client, db_session):
        theirs = _vcenter(db_session, OTHER_TENANT)
        theirs.is_primary = True
        db_session.commit()
        body = {"name": "New", "hypervisor_type": "vsphere", "host": "vc.mine.test", "port": 443, "username": "u",
                "password": "pw"}
        r = client.post("/hypervisors/connections", json=body)
        assert r.status_code == 201, r.text
        created = db_session.get(HypervisorConnection, uuid.UUID(r.json()["id"]))
        assert str(created.tenant_id) == DEV_TENANT
        assert client.post(f"/hypervisors/connections/{created.id}/set-primary").status_code == 200
        db_session.refresh(theirs)
        assert theirs.is_primary is True
