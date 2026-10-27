"""The console seam: a short-lived URL that opens one VM's screen in a browser."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, ClassVar


class ConsoleError(Exception):
    """The console could not be opened; the message is safe to show the student."""


@dataclass
class ConsoleTarget:
    """One lab whose consoles the sweep asks about: ``key`` comes back in the answer.
    ``range_id`` and ``vms`` are plain copies, all a reader's threads may use; ``rng`` (the
    lab's Range) is for ``console_reader`` on the caller's thread only, never for a thread."""

    key: Hashable
    range_id: str
    vms: list[dict[str, Any]] = field(default_factory=list)  # its provisioner output's VMs (plain)
    rng: Any = None


ConsoleReader = Callable[[], dict[Hashable, int | None]]


class BaseConsoleBackend(ABC):
    kind: ClassVar[str]

    @abstractmethod
    def open(self, vm: dict[str, Any], *, db=None, rng=None) -> dict[str, Any]:
        """Console access for one VM of a range (an entry of its provisioner output:
        ``vm_id``, ``name``, ...). Returns ``{"kind", "url", "expires_in"}``; the URL is
        single-use or short-lived, never a standing credential. ``rng`` is the VM's range
        (and ``db`` a session): a backend reaches the hypervisor that range was built on,
        and opens only a VM that is that range's."""

    def console_reader(self, db, targets: list[ConsoleTarget]) -> ConsoleReader:
        """How many console connections are open now on each target's VMs, in one batch (the
        lab sweep asks before idle closes, ADR 0014 §18: console input reaches the hypervisor,
        never TrueNorth). Two phases: this call reads what it needs from ``db`` (logins,
        ownership) in the caller's thread; the reader it returns touches no database, talks to
        the hypervisor, and may be run on another thread under a deadline. The reader answers
        ``{key: count}``; None for a target means unknown (no answer, or this backend cannot
        tell), which the caller treats as in use."""
        keys = [t.key for t in targets]
        return lambda: dict.fromkeys(keys)
