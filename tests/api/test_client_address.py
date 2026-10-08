"""client_ip believes X-Forwarded-For only from a trusted proxy (security sweep M1).

It used to take the header's first entry from anyone, so a client chose the address the
auth-zone allowlist, the rate limiter and NOISE_AGENT_CIDRS saw.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.client_address import client_ip
from starlette.requests import Request

REPO = Path(__file__).resolve().parents[2]


def _req(peer: str | None, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1) if peer else None}
    )


@pytest.fixture(autouse=True)
def _default_proxies(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)


def test_an_untrusted_peer_cannot_name_its_address():
    assert client_ip(_req("203.0.113.9", "10.255.0.9")) == "203.0.113.9"


def test_a_trusted_proxy_forwards_the_client():
    assert client_ip(_req("172.18.0.5", "203.0.113.9")) == "203.0.113.9"
    assert client_ip(_req("127.0.0.1", "198.51.100.4")) == "198.51.100.4"


def test_the_chain_is_read_right_to_left_past_trusted_hops():
    # a client-seeded entry on the left is ignored; the edge proxy appended the real one
    assert client_ip(_req("172.18.0.5", "10.255.0.9, 203.0.113.9, 172.18.0.2")) == "203.0.113.9"


def test_no_header_or_a_malformed_one_falls_back_to_the_peer():
    assert client_ip(_req("172.18.0.5")) == "172.18.0.5"
    assert client_ip(_req("172.18.0.5", "not-an-ip")) == "172.18.0.5"
    assert client_ip(_req(None)) is None


def test_an_ipv4_mapped_peer_is_matched_as_ipv4():
    assert client_ip(_req("::ffff:127.0.0.1", "203.0.113.9")) == "203.0.113.9"


def test_the_trusted_set_is_configurable(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/24")
    assert client_ip(_req("172.18.0.5", "203.0.113.9")) == "172.18.0.5"
    assert client_ip(_req("10.0.0.4", "203.0.113.9")) == "203.0.113.9"
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "none")
    assert client_ip(_req("127.0.0.1", "203.0.113.9")) == "127.0.0.1"


def test_bundled_nginx_overwrites_x_forwarded_for():
    for conf in (REPO / "control-plane/web/nginx.conf", REPO / "infra/platform/nginx/conf.d/truenorth.conf"):
        text = conf.read_text()
        assert "$proxy_add_x_forwarded_for" not in text, conf
        api_blocks = re.findall(r"location /api/ \{[\s\S]*?\n    \}", text)
        assert api_blocks and all("proxy_set_header X-Forwarded-For   $remote_addr;" in b for b in api_blocks), conf
