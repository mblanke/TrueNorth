"""Console for the mock provisioner: a URL that names the VM and opens nothing."""

from __future__ import annotations

import secrets
from typing import Any, ClassVar

from .base import BaseConsoleBackend


class MockConsole(BaseConsoleBackend):
    kind: ClassVar[str] = "mock"

    def open(self, vm: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "mock",
            "url": f"mock://console/{vm.get('vm_id', 'vm')}?t={secrets.token_hex(8)}",
            "expires_in": 60,
        }
