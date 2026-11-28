# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""tools/arc2/netbridge.py (kept under tests/scripts/ as this card's owned test location):
the bridge listens on 127.0.0.1:<port>, forwards connections to the mapped Unix socket and
exits with the command's status. A real Unix-socket echo server stands in for the egress
proxy; the bridged command is a real subprocess; the echo round trip runs through the
bridge's TCP listener, so a pass proves the byte forwarding itself. localhost loopback
only."""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_c_netbridge", ROOT / "tools" / "arc2" / "netbridge.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture
def echo_sock(tmp_path):
    """A Unix-socket echo server: whatever one client sends comes back once."""
    sock_path = tmp_path / "echo.sock"
    stop = threading.Event()
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(sock_path))
    srv.listen(8)
    srv.settimeout(0.2)

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:  # socket.timeout, aliased on 3.10+
                continue
            except OSError:
                break
            try:
                data = conn.recv(65536)
                if data:
                    conn.sendall(data)
            finally:
                conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    yield sock_path
    stop.set()


def _free_tcp_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_listener(port: float) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"netbridge never listened on 127.0.0.1:{port}")


def test_bridge_forwards_tcp_to_the_unix_socket_and_returns_the_command_status(echo_sock):
    port = _free_tcp_port()
    # The bridged command runs for a moment so the test can talk through the bridge,
    # then exits with a distinctive status the bridge must return.
    result: list[int] = []

    def run():
        result.append(
            mod.main(
                [
                    "--map",
                    f"127.0.0.1:{port}={echo_sock}",
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(1.5); raise SystemExit(7)",
                ]
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for_listener(port)

    # The client is on the TCP side (inside the sandbox); the echo server is only
    # reachable on its Unix path, so a round trip proves the bridge moved the bytes.
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
    client.settimeout(10)
    client.sendall(b"ping")
    assert client.recv(100) == b"ping"
    client.close()

    thread.join(timeout=30)
    assert not thread.is_alive()
    assert result == [7]


def test_main_refuses_to_run_without_a_command(echo_sock):
    with pytest.raises(SystemExit) as exc:
        mod.main(["--map", f"127.0.0.1:9={echo_sock}"])
    assert exc.value.code == 2
