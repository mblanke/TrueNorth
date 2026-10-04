"""ADR 0001: hypervisor connection backends and external platforms go through registries."""

from __future__ import annotations

import inspect
import uuid
from unittest import mock

import httpx
import pytest
import respx
from app.hypervisor_backends import (
    _REGISTRY as HYPERVISORS,
)
from app.hypervisor_backends import (
    BaseHypervisorBackend,
    get_hypervisor_backend,
    supported_hypervisor_types,
)
from app.models import HypervisorConnection
from app.platforms import get_platform_adapter, supported_platform_types
from app.schemas import HypervisorTestResult


@pytest.mark.parametrize("kind", sorted(HYPERVISORS))
def test_every_hypervisor_backend_honours_the_interface(kind):
    backend = get_hypervisor_backend(kind)
    assert isinstance(backend, BaseHypervisorBackend)
    assert backend.kind == kind
    assert not inspect.isabstract(type(backend))


def test_unknown_hypervisor_type_has_no_backend():
    assert get_hypervisor_backend("xen") is None
    assert get_hypervisor_backend(None) is None
    assert supported_hypervisor_types() == ["hyperv", "proxmox", "vsphere"]


def _conn(db, kind: str) -> HypervisorConnection:
    conn = HypervisorConnection(
        id=str(uuid.uuid4()),
        name=f"{kind}-test",
        hypervisor_type=kind,
        host="hv.range.test",
        port=443,
        username="u",
        password_encrypted="p",
        verify_ssl=False,
    )
    db.add(conn)
    db.flush()
    return conn


@pytest.mark.parametrize("kind", sorted(HYPERVISORS))
def test_unreachable_hypervisor_reports_failure_instead_of_raising(db_session, kind):
    """Contract: a hypervisor-side failure is a result, never an exception."""
    conn = _conn(db_session, kind)
    boom = mock.Mock(side_effect=ConnectionError("unreachable"))
    with (
        respx.mock(assert_all_called=False) as r,
        mock.patch("app.hypervisor_backends.proxmox.proxmox_client", boom),
        mock.patch("app.hypervisor_backends.hyperv._session", boom),
    ):
        r.route().mock(side_effect=httpx.ConnectError("unreachable"))
        backend = get_hypervisor_backend(kind)
        checked = backend.check_connection(conn, db_session)
        found = backend.discover(conn.id, conn, db_session)

    assert isinstance(checked, HypervisorTestResult)
    assert checked.success is False
    assert conn.is_active is False
    assert found["nodes_discovered"] == 0
    assert "fail" in found["message"].lower()


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        ("moodle", "https://lms.test/lib/ajax/service-nologin.php"),
        ("immersive_labs", "https://lms.test/api/health"),
        ("offsec", "https://lms.test/"),
        ("something_new", "https://lms.test/"),
    ],
)
def test_platform_health_url(kind, url):
    assert get_platform_adapter(kind).health_url("https://lms.test/") == url


def test_platform_registry_lists_known_types():
    assert supported_platform_types() == ["immersive_labs", "moodle", "offsec"]
