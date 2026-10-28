"""Console for the mock provisioner: a URL that names the VM and opens nothing."""

from __future__ import annotations

import secrets
import time
from typing import Any, ClassVar

from .base import BaseConsoleBackend, ConsoleReader, ConsoleTarget


class MockConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "mock"
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

    def open(self, vm: dict[str, Any], *, db=None, rng=None) -> dict[str, Any]:
        return {
            "kind": "mock",
            "url": f"mock://console/{vm.get('vm_id', 'vm')}?t={secrets.token_hex(8)}",
            "expires_in": 60,
        }

    def open_view_only(self, vm: dict[str, Any], *, db=None, rng=None) -> dict[str, Any]:
        return {**self.open(vm, db=db, rng=rng), "url": f"mock://console/{vm.get('vm_id', 'vm')}?view=1"}
