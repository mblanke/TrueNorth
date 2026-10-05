"""Reserve addresses and VLANs on shared networks, without collisions, for a range.

``reserve`` gives each holder (node, NIC) in a range a value from a pool, the lowest that
nobody in the same ``domain`` holds, across all tenants. It is:

* **Idempotent.** A holder that already has a reservation keeps it. Re-running a deploy or
  a provision retry gets the same addresses.
* **Collision-free.** The database enforces (domain, kind, value) uniqueness. Concurrent
  reservations in one domain are also serialised: on PostgreSQL with a transaction-scoped
  advisory lock on the domain (SQLite serialises writers itself). Neither depends on
  Redis.
* **Capacity-checked.** If the pool cannot hold every new holder, nothing is reserved
  and ``PoolExhaustedError`` names the domain and the shortfall.

``sync`` is ``reserve`` for a holder set that can shrink or move: it first releases what
the range holds for holders no longer listed or in another domain. ``release_range`` frees
everything a range holds; ``range_ops.reconcile`` calls it when a
destroy succeeds. Rows also go with the range (ON DELETE CASCADE).

Consumers: noise agents' management addresses (app/noise/mgmt.py, reserved when a
provision is accepted, S4b), and the worker's vSphere VLAN and uplink allocation (S5a),
which reserves on the same table with the same lock and rules (worker/db_ops.reserve_values;
the worker cannot import this module).
"""

from __future__ import annotations

import ipaddress
import uuid
import zlib

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from .models import Range, RangeState
from .models_network import KINDS, NetworkReservation


class PoolExhaustedError(RuntimeError):
    """The pool cannot hold every holder that needs a value."""


class DomainChangedError(RuntimeError):
    """The range already holds values of this kind in another domain."""


def parse_ip_pool(spec: str) -> list[str]:
    """A CIDR (``10.255.0.0/24``: its hosts), a range (``10.30.32.100-199`` or
    ``10.30.32.100-10.30.32.199``) or a comma list of those, as sorted addresses.

    Same forms as worker/uplink_pool.py, which the API may not import.
    """
    out: set[ipaddress.IPv4Address] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            out.update(ipaddress.IPv4Network(part, strict=False).hosts())
            continue
        lo, _, hi = part.partition("-")
        start = ipaddress.IPv4Address(lo.strip())
        if not hi:
            end = start
        elif "." in hi:
            end = ipaddress.IPv4Address(hi.strip())
        else:
            end = ipaddress.IPv4Address(f"{lo.strip().rsplit('.', 1)[0]}.{hi.strip()}")
        if end < start:
            raise ValueError(f"pool entry {part!r} runs backwards")
        out.update(ipaddress.IPv4Address(i) for i in range(int(start), int(end) + 1))
    if not out:
        raise ValueError(f"pool {spec!r} is empty")
    return [str(a) for a in sorted(out)]


def vlan_pool(spec: str) -> list[str]:
    """``"2000-2999"`` or a comma list of ids/ranges, as sorted VLAN ids (text)."""
    ids: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        start, end = int(lo), int(hi or lo)
        if not (1 <= start <= end <= 4094):
            raise ValueError(f"VLAN range {part!r} is outside 1-4094 or runs backwards")
        ids.update(range(start, end + 1))
    if not ids:
        raise ValueError(f"VLAN pool {spec!r} is empty")
    return [str(i) for i in sorted(ids)]


def lock_key(domain: str, kind: str) -> int:
    """The advisory-lock key for one (kind, domain). The worker takes the same lock
    (worker/db_ops.reservation_lock_key) when it reserves vSphere VLANs and uplinks."""
    return zlib.crc32(f"{kind}:{domain}".encode()) - (1 << 31)  # signed 32-bit


def _lock_domain(db: Session, domain: str, kind: str) -> None:
    """Serialise reservations in one domain until this transaction ends (PostgreSQL)."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key(domain, kind)})


def reserve(db: Session, rng: Range, *, domain: str, kind: str, pool: list[str], holders: list[str]) -> dict[str, str]:
    """{holder: value} for every holder, reserving new values as needed. Does not commit."""
    if kind not in KINDS:
        raise ValueError(f"unknown reservation kind {kind!r}")
    if not domain:
        raise ValueError("a reservation needs the shared network's domain")
    _lock_domain(db, domain, kind)
    held = db.query(NetworkReservation).filter(NetworkReservation.range_id == rng.id, NetworkReservation.kind == kind)
    if elsewhere := sorted({r.domain for r in held if r.domain != domain}):
        # The domain's name changed under a live range: its old values still exist. Same
        # rule as the worker (db_ops.reserve_values).
        raise DomainChangedError(f"range holds {kind} reservations in {', '.join(elsewhere)}, not {domain}")
    mine = {r.holder: r.value for r in held}
    needed = [h for h in dict.fromkeys(holders) if h not in mine]
    if not needed:
        return {h: mine[h] for h in holders}
    # A destroyed range no longer holds anything, even if nothing released it yet.
    gone = db.query(Range.id).filter(Range.state == RangeState.destroyed)
    db.execute(
        delete(NetworkReservation)
        .where(
            NetworkReservation.domain == domain,
            NetworkReservation.kind == kind,
            NetworkReservation.range_id.in_(gone.scalar_subquery()),
        )
        .execution_options(synchronize_session=False)
    )
    taken = {
        v
        for (v,) in db.query(NetworkReservation.value).filter(
            NetworkReservation.domain == domain, NetworkReservation.kind == kind
        )
    }
    free = [v for v in pool if v not in taken]
    if len(free) < len(needed):
        raise PoolExhaustedError(
            f"{kind} pool for {domain} has {len(free)} free of {len(pool)}; this range needs {len(needed)} more"
        )
    for holder, value in zip(needed, free, strict=False):
        db.add(
            NetworkReservation(
                id=uuid.uuid4(),
                tenant_id=rng.tenant_id,
                range_id=rng.id,
                domain=domain,
                kind=kind,
                value=value,
                holder=holder,
            )
        )
        mine[holder] = value
    db.flush()  # the unique constraints are the last word, inside this transaction
    return {h: mine[h] for h in holders}


def sync(db: Session, rng: Range, *, domain: str, kind: str, pool: list[str], holders: list[str]) -> dict[str, str]:
    """Make the range hold exactly ``holders`` in ``domain``, from ``pool``: first release
    what it holds of ``kind`` for anything else (a holder that went away, another domain,
    a value outside the pool after the range's template changed), then ``reserve``.
    Does not commit. An empty ``holders`` releases all of ``kind``."""
    keep, allowed = set(holders), set(pool)
    stale = [
        r.id
        for r in db.query(NetworkReservation).filter(
            NetworkReservation.range_id == rng.id, NetworkReservation.kind == kind
        )
        if r.domain != domain or r.holder not in keep or r.value not in allowed
    ]
    if stale:
        db.execute(
            delete(NetworkReservation)
            .where(NetworkReservation.id.in_(stale))
            .execution_options(synchronize_session=False)
        )
    if not holders:
        return {}
    return reserve(db, rng, domain=domain, kind=kind, pool=pool, holders=holders)


def release_range(db: Session, range_id: uuid.UUID) -> int:
    """Free everything the range holds. Does not commit. Returns how many were freed."""
    return db.execute(
        delete(NetworkReservation)
        .where(NetworkReservation.range_id == range_id)
        .execution_options(synchronize_session=False)
    ).rowcount
