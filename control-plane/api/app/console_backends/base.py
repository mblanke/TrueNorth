"""The console seam: a short-lived URL that opens one VM's screen in a browser."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class ConsoleError(Exception):
    """The console could not be opened; the message is safe to show the student."""


class BaseConsoleBackend(ABC):
    kind: ClassVar[str]

    @abstractmethod
    def open(self, vm: dict[str, Any], *, db=None, rng=None) -> dict[str, Any]:
        """Console access for one VM of a range (an entry of its provisioner output:
        ``vm_id``, ``name``, ...). Returns ``{"kind", "url", "expires_in"}``; the URL is
        single-use or short-lived, never a standing credential. ``rng`` is the VM's range
        (and ``db`` a session): a backend reaches the hypervisor that range was built on,
        and opens only a VM that is that range's."""

    def open_connections(self, vms: list[dict[str, Any]], *, db=None, rng=None) -> int | None:
        """How many console connections are open now on these VMs of ``rng`` (entries of its
        provisioner output). The lab sweep asks before an idle close (ADR 0014 §18): console
        input reaches the hypervisor, never TrueNorth. None: this backend cannot tell, or the
        hypervisor did not answer; the caller then treats the lab as in use (fail safe)."""
        return None
