"""The console seam: a short-lived URL that opens one VM's screen in a browser."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from ssl import SSLContext
from typing import Any, ClassVar

#: How long a console grant (and so the ``expires_in`` of ``open``) is good for connecting.
GRANT_SECONDS = 300
#: ``kind`` of every console that is relayed through the API (``/ws/console/<grant>``).
RELAY_KIND = "rfb-ws"


class ConsoleError(Exception):
    """The console could not be opened; the message is safe to show the student."""


@dataclass(frozen=True)
class DialGrant:
    """What an adapter may know of a console grant (app/console_relay): plain data, no ORM
    object and no session. The adapter finds the VM from ``vm_id``/``vm_name`` and the range
    it is handed, and decides nothing about who may connect (the relay did)."""

    id: str
    range_id: str
    vm_id: str
    vm_name: str
    view_only: bool = False  # the relay always filters its input (rfb_filter, S4); an adapter may not widen it


@dataclass(frozen=True)
class DialTarget:
    """Where the relay connects to reach one VM's screen: a WebSocket URL (it may carry a
    one-time ticket: never log it), the TLS context to use for ``wss`` (None for ``ws``) and
    the subprotocols to offer. The stream is RFB (WebMKS: subprotocol ``binary``)."""

    url: str
    ssl: SSLContext | None = None
    subprotocols: tuple[str, ...] = ("binary",)


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
    #: A console from ``open_view_only`` really is view-only (an observer's console, ADR 0014
    #: §19): either the hypervisor refuses input on that ticket, or the adapter's stream is RFB
    #: and the relay's allowlist (``console_relay/rfb_filter.py``, which runs on every
    #: ``view_only`` grant whatever the adapter) is enough. A page that merely hides the keyboard
    #: is not view-only.
    supports_view_only: ClassVar[bool] = False

    def open_view_only(self, vm: dict[str, Any], *, db=None, rng=None,
                       relay_url: str | None = None) -> dict[str, Any]:
        """As ``open``, for a console the hypervisor enforces as view-only. Only when
        ``supports_view_only``; callers fall back to ``open``, marked observed."""
        raise ConsoleError("this hypervisor has no view-only console")

    @abstractmethod
    def open(self, vm: dict[str, Any], *, db=None, rng=None, relay_url: str | None = None) -> dict[str, Any]:
        """Console access for one VM of a range (an entry of its provisioner output:
        ``vm_id``, ``name``, ...). Returns ``{"kind", "url", "expires_in"}``: ``kind`` is
        ``rfb-ws`` and ``url`` the ``relay_url`` the caller made for the grant it just
        created (``/ws/console/<grant>`` on TrueNorth), good for ``expires_in`` seconds. The
        browser never reaches the hypervisor; ``dial`` does, when the socket opens. ``rng``
        is the VM's range (and ``db`` a session)."""

    @abstractmethod
    def dial(self, grant: DialGrant, *, db=None, rng=None) -> DialTarget:
        """Where the relay connects for ``grant``, asked when the browser's socket opens (a
        hypervisor ticket lives about a minute, so it is taken now, not at grant time).
        Blocking: the relay runs it on a worker thread. ``rng`` is the grant's range (and
        ``db`` a session): a backend reaches the hypervisor that range was built on and
        serves only a VM that is that range's. Raises ``ConsoleError`` with a message safe
        to show the student."""

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


def relay_access(relay_url: str | None) -> dict[str, Any]:
    """The ``open`` answer of an adapter whose console is relayed."""
    if not relay_url:
        raise ConsoleError("no console relay address for this grant")
    return {"kind": RELAY_KIND, "url": relay_url, "expires_in": GRANT_SECONDS}
