"""POST /integrations/platforms/{id}/test goes through app.net_guard (PR #112 review).

Before: it GET the stored base_url with a plain httpx client (any address, redirects
per httpx defaults) and returned ``str(exception)`` on failure, so a tenant admin could
probe the platform network and read back why a connection failed.
"""

from __future__ import annotations

import socket
import uuid

import httpcore
import pytest
from _shared import act_as, real_tenant, real_user
from app import net_guard
from app.models import ExternalPlatform, IntegrationAuthType, UserRole
from app.routers.integrations import PROBE_REFUSED, PROBE_UNREACHABLE

PUBLIC_IP = "93.184.216.34"


def _resolve_to(ip: str):
    def fake(host, port, type=0, **_):  # noqa: A002 - socket.getaddrinfo's own name
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fake


class _Stream(httpcore.NetworkStream):
    def __init__(self, reply: bytes):
        self._reply = reply

    def read(self, max_bytes, timeout=None):
        chunk, self._reply = self._reply[:max_bytes], self._reply[max_bytes:]
        return chunk

    def write(self, buffer, timeout=None):
        pass

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None


class _Network(httpcore.NetworkBackend):
    def __init__(self, record, reply: bytes | None):
        self.record, self.reply = record, reply

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.record.append((host, port))
        if self.reply is None:
            raise httpcore.ConnectError(f"[Errno 111] Connection refused to {host}:{port}")
        return _Stream(self.reply)

    def sleep(self, seconds):
        pass


@pytest.fixture
def platform(db_session):
    tenant = real_tenant(db_session, "lms")
    act_as(real_user(db_session, UserRole.admin, tenant.id))
    row = ExternalPlatform(id=uuid.uuid4(), name="LMS", slug="lms", platform_type="moodle", base_url="http://lms.example.org",
                           auth_type=IntegrationAuthType.api_key, tenant_id=tenant.id)
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def network(monkeypatch):
    connects: list = []

    def use(reply: bytes | None):
        monkeypatch.setattr(net_guard, "_network_backend", lambda: _Network(connects, reply))
        return connects

    return use


@pytest.mark.parametrize("ip", ["169.254.169.254", "127.0.0.1", "10.0.0.5"])
def test_an_internal_platform_url_is_refused_without_a_connect(client, platform, network, monkeypatch, ip):
    connects = network(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(ip))

    body = client.post(f"/integrations/platforms/{platform.id}/test").json()

    assert body == {"platform_id": str(platform.id), "reachable": False, "error": PROBE_REFUSED}
    assert connects == []


def test_a_failure_reads_as_one_fixed_message(client, platform, network, monkeypatch):
    network(None)  # connection refused
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))

    body = client.post(f"/integrations/platforms/{platform.id}/test").json()

    assert body["reachable"] is False and body["error"] == PROBE_UNREACHABLE
    assert "Errno" not in str(body) and PUBLIC_IP not in str(body)


def test_a_public_platform_is_probed_pinned_and_redirects_are_not_followed(client, platform, network, monkeypatch):
    connects = network(b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/\r\nContent-Length: 0\r\n\r\n")
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))

    body = client.post(f"/integrations/platforms/{platform.id}/test").json()

    assert body == {"platform_id": str(platform.id), "reachable": True, "status_code": 302}
    assert connects == [(PUBLIC_IP, 80)]  # one connect, to the vetted address


def test_private_platforms_need_the_explicit_flag(client, platform, network, monkeypatch):
    monkeypatch.setenv("INTEGRATION_ALLOW_PRIVATE_URLS", "true")
    connects = network(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to("10.0.0.5"))

    assert client.post(f"/integrations/platforms/{platform.id}/test").json()["reachable"] is True
    assert connects == [("10.0.0.5", 80)]
