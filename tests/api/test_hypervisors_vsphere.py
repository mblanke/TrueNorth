"""vSphere is the range platform. These cover what the dashboard relies on.

- Discovery against a (mocked) vCenter records each ESXi host with its real VM count,
  keeps the vCenter's address out of the host's IP, and leaves CPU/memory empty —
  the vCenter REST API does not report them, and 0 would read as an idle host.
- ``GET /hypervisors/nodes`` is readable by instructors (INFRA_READ) for the dashboard,
  while every connection route — hosts, usernames, stored credentials — stays INFRA_WRITE.
"""

import uuid
from contextlib import contextmanager

import httpx
import pytest
import respx
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import HypervisorConnection, HypervisorNode, UserRole
from app.routers.hypervisors import _discover_vsphere

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


def _vcenter(db) -> HypervisorConnection:
    conn = HypervisorConnection(
        id=uuid.uuid4(),
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
