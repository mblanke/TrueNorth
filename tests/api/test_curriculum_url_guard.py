"""Curriculum URL ingest goes through app.net_guard (H1).

Before: ``httpx.AsyncClient(follow_redirects=True)`` with only a scheme check in the
router, so a course author could register ``http://169.254.169.254/...`` (or a public
page that redirects there) and read the answer back through RAG search.
"""

from __future__ import annotations

import asyncio
import socket

import httpcore
import pytest
from app import curriculum_ingest, net_guard

PUBLIC_IP = "93.184.216.34"


def _resolve_to(ip: str):
    def fake(host, port, type=0, **_):  # noqa: A002 - socket.getaddrinfo's own name
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fake


class _Stream(httpcore.NetworkStream):
    def __init__(self, record, reply: bytes):
        self.record, self._reply = record, reply

    def read(self, max_bytes, timeout=None):
        chunk, self._reply = self._reply[:max_bytes], self._reply[max_bytes:]
        return chunk

    def write(self, buffer, timeout=None):
        self.record.setdefault("sent", b"")
        self.record["sent"] += buffer

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None


class _Network(httpcore.NetworkBackend):
    def __init__(self, record, reply: bytes):
        self.record, self.reply = record, reply
        record["connects"] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.record["connects"].append((host, port))
        return _Stream(self.record, self.reply)

    def sleep(self, seconds):
        pass


def _http(status: str, body: bytes = b"", headers: str = "Content-Type: text/html\r\n") -> bytes:
    return b"HTTP/1.1 %s\r\n%sContent-Length: %d\r\n\r\n%s" % (status.encode(), headers.encode(), len(body), body)


def _fetch(url):
    return asyncio.run(curriculum_ingest.fetch_url_text(url))


@pytest.fixture
def network(monkeypatch):
    record: dict = {}

    def use(reply: bytes):
        monkeypatch.setattr(net_guard, "_network_backend", lambda: _Network(record, reply))
        return record

    return use


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "10.0.0.5", "::1", "::ffff:127.0.0.1", "100.64.0.1"])
def test_internal_destinations_are_refused_before_any_connect(monkeypatch, network, ip):
    record = network(_http("200 OK", b"<title>secret</title>internal"))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(ip))

    with pytest.raises(ValueError) as refused:
        _fetch("http://metadata.internal/latest/meta-data/")

    assert str(refused.value) == curriculum_ingest.URL_REFUSED
    assert record == {}  # no network backend was even built


def test_a_redirect_to_metadata_is_not_followed(monkeypatch, network):
    record = network(_http("302 Found", headers="Location: http://169.254.169.254/latest/\r\n"))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))

    with pytest.raises(ValueError) as failed:
        _fetch("https://docs.example.org/course")

    # The hop is re-vetted like a first fetch, and refused before any connect.
    assert str(failed.value) == curriculum_ingest.URL_REFUSED
    assert record["connects"] == [(PUBLIC_IP, 443)]  # one connect, to the vetted host only


class _Hops(httpcore.NetworkBackend):
    """Answers each connect with the next reply, recording (dialled address, port).
    net_guard builds one backend per hop, so the record and the replies are shared."""

    def __init__(self, record, replies: list[bytes]):
        self.record, self.replies = record, replies
        record.setdefault("connects", [])

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.record["connects"].append((host, port))
        return _Stream(self.record, self.replies.pop(0))

    def sleep(self, seconds):
        pass


def _by_host(table: dict[str, str]):
    def fake(host, port, type=0, **_):  # noqa: A002 - socket.getaddrinfo's own name
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    return fake


def test_redirects_are_followed_and_each_hop_is_vetted_and_pinned(monkeypatch):
    record: dict = {}
    replies = [
        _http("301 Moved Permanently", headers="Location: https://docs.example.org/course\r\n"),
        _http("302 Found", headers="Location: https://cdn.example.net/course/\r\n"),
        _http("200 OK", b"<title>Moved</title>here"),
    ]
    monkeypatch.setattr(net_guard, "_network_backend", lambda: _Hops(record, replies))
    monkeypatch.setattr(net_guard, "_resolve", _by_host({"docs.example.org": PUBLIC_IP, "cdn.example.net": "93.184.216.35"}))

    assert _fetch("http://docs.example.org/course")[0] == "Moved"
    assert record["connects"] == [(PUBLIC_IP, 80), (PUBLIC_IP, 443), ("93.184.216.35", 443)]


def test_a_redirect_whose_host_resolves_inside_is_refused(monkeypatch):
    record: dict = {}
    replies = [_http("302 Found", headers="Location: http://intranet.example.org/\r\n"), _http("200 OK", b"secret")]
    monkeypatch.setattr(net_guard, "_network_backend", lambda: _Hops(record, replies))
    monkeypatch.setattr(net_guard, "_resolve", _by_host({"docs.example.org": PUBLIC_IP, "intranet.example.org": "10.1.2.3"}))

    with pytest.raises(ValueError) as refused:
        _fetch("https://docs.example.org/")

    assert str(refused.value) == curriculum_ingest.URL_REFUSED
    assert record["connects"] == [(PUBLIC_IP, 443)]


def test_more_than_three_redirects_are_not_followed(monkeypatch):
    record: dict = {}
    loop = _http("302 Found", headers="Location: https://docs.example.org/again\r\n")
    monkeypatch.setattr(net_guard, "_network_backend", lambda: _Hops(record, [loop] * 5))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))

    with pytest.raises(ValueError) as failed:
        _fetch("https://docs.example.org/")

    assert str(failed.value) == curriculum_ingest.URL_UNREACHABLE
    assert len(record["connects"]) == 1 + curriculum_ingest.URL_MAX_REDIRECTS


def test_failures_read_the_same_whatever_the_cause(monkeypatch, network):
    def no_dns(*_a, **_k):
        raise socket.gaierror("Name or service not known")

    network(_http("404 Not Found"))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))
    with pytest.raises(ValueError) as not_found:
        _fetch("https://docs.example.org/missing")
    monkeypatch.setattr(net_guard, "_resolve", no_dns)
    with pytest.raises(ValueError) as no_host:
        _fetch("https://nowhere.example.org/")
    assert str(not_found.value) == str(no_host.value) == curriculum_ingest.URL_UNREACHABLE


def test_an_oversize_page_is_refused(monkeypatch, network):
    monkeypatch.setenv("CURRICULUM_URL_MAX_BYTES", "32")
    network(_http("200 OK", b"x" * 200))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))
    with pytest.raises(ValueError, match="larger than 32 bytes"):
        _fetch("https://docs.example.org/big")


def test_a_public_page_is_fetched_and_pinned(monkeypatch, network):
    record = network(_http("200 OK", b"<html><title>Intro</title><p>Hello course</p></html>"))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to(PUBLIC_IP))

    title, text = _fetch("https://docs.example.org/intro")

    assert title == "Intro" and "Hello course" in text
    assert record["connects"] == [(PUBLIC_IP, 443)]
    assert b"\r\nHost: docs.example.org\r\n" in record["sent"]


def test_private_pages_need_the_explicit_flag(monkeypatch, network):
    monkeypatch.setenv("CURRICULUM_ALLOW_PRIVATE_URLS", "true")
    network(_http("200 OK", b"<title>Range wiki</title>ok"))
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to("10.0.0.5"))
    assert _fetch("http://wiki.range.local/")[0] == "Range wiki"
    monkeypatch.setattr(net_guard, "_resolve", _resolve_to("169.254.169.254"))
    with pytest.raises(ValueError):  # the flag never opens link-local / loopback
        _fetch("http://wiki.range.local/")
