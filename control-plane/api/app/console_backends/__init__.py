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

from .base import BaseConsoleBackend, ConsoleError, ConsoleReader, ConsoleTarget
from .mock import MockConsole
from .vsphere_webmks import VsphereWebMKSConsole

__all__ = [
    "BaseConsoleBackend",
    "ConsoleError",
    "ConsoleReader",
    "ConsoleTarget",
    "MockConsole",
    "VsphereWebMKSConsole",
    "console_backend_for",
    "expects_console",
    "get_console_backend",
]

_REGISTRY: dict[str, type[BaseConsoleBackend]] = {
    MockConsole.kind: MockConsole,
    VsphereWebMKSConsole.kind: VsphereWebMKSConsole,
}


def get_console_backend(kind: str | None) -> BaseConsoleBackend:
    cls = _REGISTRY.get(kind or "")
    if cls is None:
        raise ValueError(f"no browser console for provisioner backend {kind!r}; one of {sorted(_REGISTRY)}")
    return cls()


def console_backend_for(kind: str | None) -> BaseConsoleBackend | None:
    """The console adapter of a provisioner backend, or None when there is none."""
    cls = _REGISTRY.get(kind or "")
    return cls() if cls else None


def expects_console(kind: str | None) -> bool:
    """Whether ranges of this provisioner backend are on a hypervisor family that has browser
    consoles (one an adapter here serves). With no adapter for the backend itself, the lab
    sweep then cannot tell whether a console is open (unknown); a backend of a family with no
    console concept closes idle labs on heartbeats alone."""
    from ..hypervisor_backends import get_hypervisor_backend, supported_hypervisor_types

    for family_kind in supported_hypervisor_types():
        family = get_hypervisor_backend(family_kind)
        if family is not None and family.has_consoles and (kind or "") in family.provisioner_backends:
            return True
    return False
