"""Console for the mock provisioner: a URL that names the VM and opens nothing."""

from __future__ import annotations

import secrets
from typing import Any, ClassVar

from .base import BaseConsoleBackend


class MockConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "mock"
    #: What ``open_connections`` answers: mock VMs have no consoles. Tests set it.
    connections: ClassVar[int | None] = 0

    def open_connections(self, vms: list[dict[str, Any]], *, db=None, rng=None) -> int | None:
        return type(self).connections

    def open(self, vm: dict[str, Any], *, db=None, rng=None) -> dict[str, Any]:
        return {
            "kind": "mock",
            "url": f"mock://console/{vm.get('vm_id', 'vm')}?t={secrets.token_hex(8)}",
            "expires_in": 60,
        }
