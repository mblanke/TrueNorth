"""Console for the mock provisioner: the relay's real path against an in-process RFB stub.

``dial`` starts (once per process) a WebSocket server on 127.0.0.1 and hands the relay its
URL, so the relay's pump, close codes and single-socket rule run for real in tests and
offline development. The stub is not a VNC server: on connect it sends the RFB protocol
version greeting, ``b"RFB 003.008\\n"``, then echoes every message back. It offers the
``binary`` subprotocol, as WebMKS does. Tests steer it with ``dial_error`` (the hypervisor
refuses a ticket) and ``dial_unreachable`` (the ticket names a host that does not answer).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, ClassVar

from .base import BaseConsoleBackend, ConsoleError, ConsoleReader, ConsoleTarget, DialGrant, DialTarget, relay_access

RFB_GREETING = b"RFB 003.008\n"
_STUB_LOCK = threading.Lock()
_STUB_PORT: int | None = None


def _stub_port() -> int:
    """The port of the process's echo stub, started on first use on a daemon thread."""
    global _STUB_PORT
    with _STUB_LOCK:
        if _STUB_PORT is not None:
            return _STUB_PORT
        from websockets.asyncio.server import serve

        async def handler(conn) -> None:
            from websockets.exceptions import ConnectionClosed

            try:
                await conn.send(RFB_GREETING)
                async for message in conn:
                    await conn.send(message)
            except ConnectionClosed:  # the relay hung up, with or without a close frame
                pass

        ready = threading.Event()
        box: dict[str, int] = {}

        def run() -> None:
            async def main() -> None:
                async with serve(handler, "127.0.0.1", 0, subprotocols=["binary"]) as server:
                    box["port"] = server.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.Future()  # until the process ends

            asyncio.run(main())

        threading.Thread(target=run, name="tn-mock-rfb", daemon=True).start()
        ready.wait(10)
        _STUB_PORT = box["port"]
        return _STUB_PORT


class MockConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "mock"
    #: Tests set it to play a hypervisor that refuses the console (``dial`` raises with it).
    dial_error: ClassVar[str | None] = None
    #: Tests set it to play a ticket whose host does not answer (a closed local port).
    dial_unreachable: ClassVar[bool] = False
    #: What the console reader answers for every lab: mock VMs have no consoles. Tests set
    #: it, and ``delay`` (seconds) to play a hypervisor that is slow to answer.
    connections: ClassVar[int | None] = 0
    delay: ClassVar[float] = 0.0
    logins: ClassVar[int] = 0
    #: Tests set it to play a hypervisor that enforces view-only consoles.
    supports_view_only: ClassVar[bool] = False

    def console_reader(self, db, targets: list[ConsoleTarget]) -> ConsoleReader:
        cls = type(self)
        keys = [t.key for t in targets]

        def read() -> dict:
            cls.logins += 1
            if cls.delay:
                time.sleep(cls.delay)
            return dict.fromkeys(keys, cls.connections)

        return read

    def open(self, vm: dict[str, Any], *, db=None, rng=None, relay_url: str | None = None) -> dict[str, Any]:
        return relay_access(relay_url)

    def open_view_only(self, vm: dict[str, Any], *, db=None, rng=None,
                       relay_url: str | None = None) -> dict[str, Any]:
        return self.open(vm, db=db, rng=rng, relay_url=relay_url)

    def dial(self, grant: DialGrant, *, db=None, rng=None) -> DialTarget:
        if type(self).dial_error:
            raise ConsoleError(type(self).dial_error)
        if type(self).dial_unreachable:
            return DialTarget(url="ws://127.0.0.1:1/ticket/none")
        return DialTarget(url=f"ws://127.0.0.1:{_stub_port()}/ticket/mock")
