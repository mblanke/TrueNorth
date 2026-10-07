"""Reserve a build's shared values (VLANs, uplink addresses) before the provisioner runs.

The provisioner says what it needs (``BaseProvisioner.allocation_needs``: vSphere asks for
a physical VLAN per template VLAN and one uplink address for the edge firewall). Here they
are reserved on the API's ``network_reservations`` table (S4a: app/models_network.py,
app/network_inventory.py), so no two ranges, in any tenant, hold one value in one domain:

* one transaction for the whole build, the range's row locked and still ``provisioning``
  (an operator may have abandoned it since the task claimed it: then nothing is reserved);
* per (domain, kind) a PostgreSQL transaction-scoped advisory lock with the API's key
  (``lock_key``), taken in key order so two builds cannot deadlock; no Redis;
* idempotent per (range, kind, holder): a retry gets the same values, and values the
  range held for holders this build no longer has are freed;
* the reservation commits before the build starts and ``planned_output`` is merged into
  ``provisioner_output``, so a build that dies half-way still holds its values and its
  destroy still finds the port groups.

``release_after_destroy`` frees them once the range is recorded destroyed. Even without
it nothing leaks for long: a reservation held by a destroyed range is reclaimed by the
next reservation in its domain (here and in the API).

This module owns no schema. The two tables are Core mirrors of the API's columns (the
worker cannot import the API); the table itself comes with the S4a migration. Until that
migration has run, a build that needs reservations fails with
``ReservationsUnavailableError`` instead of building an unreserved range.

The worker wiring is two calls in tasks.py (see ``reserve_for_build`` and
``release_after_destroy``).
"""

from __future__ import annotations

import json
import uuid
import zlib
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import CHAR, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


class GUID(TypeDecorator):
    """The API's GUID column type (app/models.py): CHAR(36) on SQLite, UUID on PostgreSQL."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


metadata = sa.MetaData()

# Columns (and, for tests on SQLite, the constraints) of app.models_network.NetworkReservation.
network_reservations = sa.Table(
    "network_reservations",
    metadata,
    sa.Column("id", GUID(), primary_key=True),
    sa.Column("tenant_id", GUID(), nullable=False),
    sa.Column("range_id", GUID(), nullable=False),
    sa.Column("domain", sa.String(255), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("value", sa.String(64), nullable=False),
    sa.Column("holder", sa.String(255), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("domain", "kind", "value", name="uq_network_reservations_value"),
    sa.UniqueConstraint("range_id", "kind", "holder", name="uq_network_reservations_holder"),
)

# The columns of ``ranges`` this module reads and writes.
ranges = sa.Table(
    "ranges",
    metadata,
    sa.Column("id", GUID(), primary_key=True),
    sa.Column("tenant_id", GUID()),
    sa.Column("state", sa.String(32)),
    sa.Column("provisioner_output", sa.Text()),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
)


class AllocationError(RuntimeError):
    """A build's shared values could not be reserved; retrying will not help by itself."""


class PoolExhaustedError(AllocationError):
    """The pool cannot hold every holder that needs a value."""


class DomainChangedError(AllocationError):
    """The range holds values of this kind in another domain (the setting that names the
    domain changed under a live range). Refused: its old values still exist physically."""


class ReservationsUnavailableError(AllocationError):
    """The network_reservations table does not exist yet (the S4a migration has not run)."""


def lock_key(domain: str, kind: str) -> int:
    """The advisory-lock key for one (kind, domain): app.network_inventory._lock_domain's."""
    return zlib.crc32(f"{kind}:{domain}".encode()) - (1 << 31)  # signed 32-bit


def _now() -> datetime:
    return datetime.now(UTC)


def reserve_values(
    db, range_id: str, *, domain: str, kind: str, pool: list[str], holders: list[str], prune: bool = False
) -> dict[str, str]:
    """{holder: value} for every holder, reserving the lowest free pool values as needed.

    Does not commit. ``prune`` (a fresh build) first frees the range's holders of this kind
    that are not in ``holders``. Raises PoolExhaustedError, reserving nothing, when the
    pool is too small, and DomainChangedError when the range holds this kind elsewhere.
    """
    if not domain:
        raise ValueError("a reservation needs the shared network's domain")
    nr = network_reservations
    if db.get_bind().dialect.name == "postgresql":
        db.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key(domain, kind))))
    rows = db.execute(
        sa.select(nr.c.holder, nr.c.value, nr.c.domain).where(nr.c.range_id == range_id, nr.c.kind == kind)
    ).all()
    if elsewhere := sorted({d for _, _, d in rows if d != domain}):
        raise DomainChangedError(
            f"range holds {kind} reservations in {', '.join(elsewhere)}, not {domain}: the allocation domain "
            "changed under it. Restore the setting, or destroy the range and build it again"
        )
    mine = {h: v for h, v, _ in rows}
    if prune and (stale := [h for h in mine if h not in holders]):
        db.execute(sa.delete(nr).where(nr.c.range_id == range_id, nr.c.kind == kind, nr.c.holder.in_(stale)))
        mine = {h: v for h, v in mine.items() if h in holders}
    needed = [h for h in dict.fromkeys(holders) if h not in mine]
    if not needed:
        return {h: mine[h] for h in holders}
    same = (nr.c.domain == domain, nr.c.kind == kind)
    destroyed = sa.select(ranges.c.id).where(sa.cast(ranges.c.state, sa.Text) == "destroyed")
    db.execute(sa.delete(nr).where(*same, nr.c.range_id.in_(destroyed)))  # a destroyed range holds nothing
    taken = set(db.execute(sa.select(nr.c.value).where(*same)).scalars())
    free = [v for v in pool if v not in taken]
    if len(free) < len(needed):
        raise PoolExhaustedError(
            f"{kind} pool for {domain} has {len(free)} free of {len(pool)}; this range needs {len(needed)} more"
        )
    tenant = db.execute(sa.select(ranges.c.tenant_id).where(ranges.c.id == range_id)).scalar_one()
    now = _now()
    new = [
        {"id": uuid.uuid4(), "tenant_id": tenant, "range_id": range_id, "domain": domain, "kind": kind,
         "value": value, "holder": holder, "created_at": now, "updated_at": now}
        for holder, value in zip(needed, free, strict=False)
    ]
    db.execute(sa.insert(nr), new)  # the unique constraints have the last word
    mine.update({r["holder"]: r["value"] for r in new})
    return {h: mine[h] for h in holders}


def _has_table(db) -> bool:
    return sa.inspect(db.get_bind()).has_table("network_reservations")


def _merge_output(db, range_id: str, updates: dict) -> None:
    raw = db.execute(sa.select(ranges.c.provisioner_output).where(ranges.c.id == range_id)).scalar()
    try:
        current = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        current = {}
    merged = {**(current if isinstance(current, dict) else {}), **updates}
    db.execute(sa.update(ranges).where(ranges.c.id == range_id)
               .values(provisioner_output=json.dumps(merged), updated_at=_now()))


def reserve_for_build(session, range_id: str, provisioner, template: dict) -> dict:
    """The allocations ``provisioner.provision`` needs, reserved and committed.

    ``session`` is the worker's session factory (tasks._db_session: commits on success,
    rolls back on an exception). Returns {} at once for a backend or template that needs
    nothing, without touching the database. In tasks.provision_range, after the backend
    is chosen:

        allocations.update(range_alloc.reserve_for_build(_db_session, range_id, provisioner, template))
    """
    needs = provisioner.allocation_needs(range_id, template)
    if not needs:
        return {}
    allocations: dict = {}
    with session() as db:
        if not _has_table(db):
            raise ReservationsUnavailableError(
                "this build needs VLAN/uplink reservations, but the network_reservations table does not exist: "
                "run the database migrations (S4a) first"
            )
        claimed = db.execute(
            sa.select(ranges.c.id)
            .where(ranges.c.id == range_id, sa.cast(ranges.c.state, sa.Text) == "provisioning")
            .with_for_update()
        ).first()
        if claimed is None:
            raise AllocationError(f"range {range_id} is no longer provisioning; nothing reserved")
        for need in sorted(needs, key=lambda n: lock_key(n.domain, n.kind)):
            got = reserve_values(
                db, range_id, domain=need.domain, kind=need.kind, pool=need.pool, holders=need.holders, prune=True
            )
            allocations[need.key] = got[need.holders[0]] if need.single else got
        if planned := provisioner.planned_output(range_id, allocations):
            _merge_output(db, range_id, planned)
    return allocations


def release_after_destroy(session, range_id: str) -> int:
    """Free everything the range holds, once it is recorded destroyed. Returns how many.

    A no-op before the S4a migration. In tasks.destroy_range, after the range is recorded
    destroyed:

        range_alloc.release_after_destroy(_db_session, range_id)
    """
    with session() as db:
        if not _has_table(db):
            return 0
        # Only a range that is destroyed: a late or duplicate call must not free the values
        # of a range that is (still, or again) live.
        destroyed = sa.select(ranges.c.id).where(ranges.c.id == range_id,
                                                 sa.cast(ranges.c.state, sa.Text) == "destroyed")
        return db.execute(
            sa.delete(network_reservations).where(network_reservations.c.range_id.in_(destroyed))
        ).rowcount
