"""Browser console access to a lab VM (ADR 0001).

Usage:
    from app.console_backends import get_console_backend

    console = get_console_backend(session.backend)   # the session's provisioner backend
    access = console.open(vm)                        # {"kind", "url", "expires_in"}

Keyed by provisioner backend: ``vsphere_api`` hands out a VMware WebMKS ticket, ``mock``
a placeholder for tests and offline development. Vendor SDKs (pyVmomi) are imported only
inside this package.
"""

from __future__ import annotations

from .base import BaseConsoleBackend, ConsoleError
from .mock import MockConsole
from .vsphere_webmks import VsphereWebMKSConsole

__all__ = ["BaseConsoleBackend", "ConsoleError", "MockConsole", "VsphereWebMKSConsole", "get_console_backend"]

_REGISTRY: dict[str, type[BaseConsoleBackend]] = {
    MockConsole.kind: MockConsole,
    VsphereWebMKSConsole.kind: VsphereWebMKSConsole,
}


def get_console_backend(kind: str | None) -> BaseConsoleBackend:
    cls = _REGISTRY.get(kind or "")
    if cls is None:
        raise ValueError(f"no browser console for provisioner backend {kind!r}; one of {sorted(_REGISTRY)}")
    return cls()
