"""The egress proxy tunnels to allow-listed hosts only (tools/arc2/egress.py)."""

from __future__ import annotations

import socket
import threading

from arc2.egress import DEFAULT_ALLOW, EgressProxy, allowed_hosts


def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def serve():
        conn, _ = srv.accept()
        conn.sendall(conn.recv(1024).upper())
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def _connect(proxy: EgressProxy, request: bytes) -> tuple[socket.socket, str]:
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(request)
    return s, s.recv(128).decode("latin-1")


def test_an_allow_listed_host_is_tunnelled():
    srv, port = _echo_server()
    proxy = EgressProxy(allow=("127.0.0.1",), ports=(port,)).start()
    try:
        s, reply = _connect(proxy, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert " 200 " in reply
        s.sendall(b"hello")
        assert s.recv(16) == b"HELLO"
        s.close()
    finally:
        proxy.close()
        srv.close()


def test_any_other_host_port_or_method_is_refused():
    proxy = EgressProxy(allow=("api.anthropic.com",)).start()
    try:
        for request in (
            b"CONNECT exfil.example.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT api.anthropic.com:22 HTTP/1.1\r\n\r\n",
            b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            b"CONNECT API.ANTHROPIC.COM.evil.test:443 HTTP/1.1\r\n\r\n",
        ):
            s, reply = _connect(proxy, request)
            assert reply.startswith("HTTP/1.1 403"), (request, reply)
            s.close()
        assert "exfil.example.com:443" in proxy.refused
    finally:
        proxy.close()


def test_a_host_port_entry_tunnels_exactly_that_port():
    srv, port = _echo_server()
    proxy = EgressProxy(allow=(f"127.0.0.1:{port}",)).start()
    try:
        s, reply = _connect(proxy, f"CONNECT 127.0.0.1:{port + 1} HTTP/1.1\r\n\r\n".encode())
        assert reply.startswith("HTTP/1.1 403")
        s.close()
        s, reply = _connect(proxy, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert reply.startswith("HTTP/1.1 403"), "a host:port entry does not open 443 on that host"
        s.close()
        s, reply = _connect(proxy, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert " 200 " in reply
        s.sendall(b"hi")
        assert s.recv(16) == b"HI"
        s.close()
    finally:
        proxy.close()
        srv.close()


def test_the_allow_list_defaults_to_the_model_api(monkeypatch):
    monkeypatch.delenv("ARC2_EGRESS_ALLOW", raising=False)
    assert allowed_hosts() == DEFAULT_ALLOW == ("api.anthropic.com",)
    monkeypatch.setenv("ARC2_EGRESS_ALLOW", "api.anthropic.com, Proxy.Corp.Example ")
    assert allowed_hosts() == ("api.anthropic.com", "proxy.corp.example")


def test_the_job_environment_points_at_the_proxy_and_keeps_the_local_fallback_direct():
    proxy = EgressProxy()
    try:
        env = proxy.env()
        assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{proxy.port}"
        assert "127.0.0.1" in env["NO_PROXY"] and "localhost" in env["NO_PROXY"]
    finally:
        proxy.close()
