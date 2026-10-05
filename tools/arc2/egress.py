"""The only way out of a confined Course Studio job: an allow-listing HTTPS proxy.

A job runs request text from any tenant, and its environment holds the runner's model
credential. With open internet egress it could send that credential, or a run's
content, anywhere. The sandbox (arc2/confine.py) therefore denies every outbound IP
connection except to this proxy, which the runner runs outside the sandbox on
127.0.0.1. The proxy only tunnels ``CONNECT host:443`` for allow-listed hosts and
refuses everything else with 403. By default that is the model API
(``api.anthropic.com``); Claude Code with ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``
asks for nothing else (measured). ``ARC2_EGRESS_ALLOW`` replaces the list (comma
separated); the local model fallback is reached directly, not through the proxy.

What it does not do: inspect TLS. A job can still put text in its own model requests;
it cannot send them to anyone but the model provider.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading

logger = logging.getLogger("arc2.egress")

DEFAULT_ALLOW = ("api.anthropic.com",)
HEADER_LIMIT = 8192
IDLE_TIMEOUT = 600  # seconds a tunnel may sit idle


def allowed_hosts() -> tuple[str, ...]:
    spec = os.environ.get("ARC2_EGRESS_ALLOW")
    hosts = tuple(h.strip().lower() for h in (spec.split(",") if spec else DEFAULT_ALLOW) if h.strip())
    return hosts or DEFAULT_ALLOW


class EgressProxy:
    """A CONNECT-only proxy on 127.0.0.1 that tunnels to allow-listed host:443 only."""

    def __init__(
        self,
        allow: tuple[str, ...] | None = None,
        port: int = 0,
        ports: tuple[int, ...] = (443,),
        unix_path: str | None = None,
    ):
        self.allow = tuple(h.lower() for h in (allow or allowed_hosts()))
        self.ports = ports
        self.refused: list[str] = []  # "host:port" refused, for the job record and tests
        self.bridges: list = []  # Linux: (port, unix socket) the job's namespace forwards (runner sets)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        # Also on a Unix socket: a Linux job has a network namespace of its own and reaches
        # the proxy only through this, bind-mounted in (arc2/netbridge.py).
        self.unix_path = unix_path
        self._unix: socket.socket | None = None
        if unix_path:
            self._unix = socket.socket(socket.AF_UNIX)
            self._unix.bind(unix_path)
            self._unix.listen(64)
        self._threads = [
            threading.Thread(target=self._serve, args=(srv,), name="arc2-egress", daemon=True)
            for srv in (self._srv, self._unix)
            if srv
        ]

    def start(self) -> EgressProxy:
        for thread in self._threads:
            thread.start()
        return self

    def close(self) -> None:
        for srv in (self._srv, self._unix):
            if srv:
                with contextlib.suppress(OSError):
                    srv.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict:
        """What a job needs to use the proxy (and to keep the local fallback direct)."""
        return {
            "HTTPS_PROXY": self.url,
            "HTTP_PROXY": self.url,
            "https_proxy": self.url,
            "http_proxy": self.url,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def _serve(self, srv: socket.socket) -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(30)
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk or len(head) > HEADER_LIMIT:
                    return self._close(client)
                head += chunk
            method, target, *_ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            host, _, port_text = target.rpartition(":")
            host = host.strip("[]").lower()
            port = int(port_text) if port_text.isdigit() else 0
            if method.upper() != "CONNECT" or host not in self.allow or port not in self.ports:
                self.refused.append(f"{host or target}:{port}")
                logger.warning("egress refused: %s %s", method, target)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return self._close(client)
            upstream = socket.create_connection((host, port), timeout=30)
        except (OSError, ValueError, UnicodeDecodeError):
            return self._close(client)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        for s in (client, upstream):
            s.settimeout(IDLE_TIMEOUT)
        threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
        self._pipe(upstream, client)

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                EgressProxy._close(s)

    @staticmethod
    def _close(s: socket.socket) -> None:
        with contextlib.suppress(OSError):
            s.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            s.close()


class LocalForward:
    """A Unix socket that forwards to 127.0.0.1:<port> (the local model fallback), for a
    Linux job whose own network namespace cannot reach the host's loopback."""

    def __init__(self, unix_path: str, port: int):
        self.unix_path, self.port = unix_path, port
        self._srv = socket.socket(socket.AF_UNIX)
        self._srv.bind(unix_path)
        self._srv.listen(32)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", self.port), timeout=30)
            except OSError:
                EgressProxy._close(client)
                continue
            threading.Thread(target=EgressProxy._pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=EgressProxy._pipe, args=(upstream, client), daemon=True).start()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._srv.close()
