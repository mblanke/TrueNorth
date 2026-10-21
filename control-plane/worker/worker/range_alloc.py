"""Reserve a build's shared values (VLANs, uplink addresses) before the provisioner runs.

The provisioner says what it needs (``BaseProvisioner.allocation_needs``: vSphere asks for
a physical VLAN per template VLAN and one uplink address for the edge firewall). They are
reserved on the API's ``network_reservations`` table (S4a: app/network_inventory), through
db_ops on the generated table mirror, so no two ranges, in any tenant, hold one value in
one domain:

* one transaction for the whole build, the range's row locked and still ``provisioning``
  (an operator may have abandoned it since the task claimed it: then nothing is reserved);
* per (domain, kind) a PostgreSQL transaction-scoped advisory lock with the API's key,
  taken in key order so two builds cannot deadlock; no Redis;
* idempotent per (range, kind, holder): a retry gets the same values, and values the range
  held for holders this build no longer has are freed;
* the reservation commits before the build starts and ``planned_output`` is merged into
  ``provisioner_output``, so a build that dies half-way still holds its values and its
  destroy still finds the port groups.

``release_after_destroy`` frees them once the range is recorded destroyed. Even without it
nothing leaks for long: a reservation held by a destroyed range is reclaimed by the next
reservation in its domain (here and in the API, which also releases on reconcile).

Range certification S1 (atomic VLAN allocation) adds three things:

* **External reservations.** Before the transaction, the provisioner reads the live
  switch (``live_usage``: every port group's VLANs, trunks expanded). Inside it, under
  the same advisory lock, ``record_external`` stores the pool VLANs used outside
  TrueNorth as reservations with no range (holder ``external:<source>:<port group>``),
  so the same unique constraint keeps every allocator off them. Each read replaces its
  own source's entries (a VLAN freed outside TrueNorth becomes allocatable again);
  another source's entries expire when not seen for ``EXTERNAL_TTL``.
* **Reserved values.** ``AllocationNeed.excluded`` (VSPHERE_RESERVED_VLANS) are never
  handed out, and a range that holds one is refused; template subnets overlapping
  TN_RANGE_RESERVED_CIDRS are refused before anything is reserved (reserved_networks.py).
* **A collision at creation.** The provisioner rechecks the live switch right before it
  creates each port group; on a collision it stops and reports ``result.conflict``.
  ``build_failure`` then releases the VLANs the range no longer needs, records the
  collision as external and fails the range with ``NetworkConflictError`` (final: a
  retry would collide again).

tasks.provision_range calls ``reserve_for_build``, ``stored_output`` and, for a failed
build, ``build_failure``; tasks.destroy_range calls ``release_after_destroy``.
``AllocationError`` is final (fencing.FINAL_ERRORS): a full pool is not fixed by retrying.
"""

from __future__ import annotations

import itertools
import logging
import zlib
from datetime import UTC, datetime, timedelta

from . import db_ops, reserved_networks

logger = logging.getLogger("truenorth.worker")

EXTERNAL = "external:"  # app.network_inventory.models.EXTERNAL: holder prefix of a value used outside TrueNorth
# Another source's external entries not seen by any read for this long are dropped (each
# read refreshes its own source at once). Long: a stale entry only costs one pool value.
EXTERNAL_TTL = timedelta(days=30)


class AllocationError(RuntimeError):
    """A build's shared values could not be reserved; retrying will not help by itself."""


class PoolExhaustedError(AllocationError):
    """The pool cannot hold every holder that needs a value."""


class DomainChangedError(AllocationError):
    """The range holds values of this kind in another domain (the setting that names the
    domain changed under a live range). Refused: its old values still exist physically."""


class ReservedNetworkError(AllocationError):
    """The template's subnets overlap a reserved site network, or the range holds a value
    that is now reserved (TN_RANGE_RESERVED_CIDRS, VSPHERE_RESERVED_VLANS)."""


class NetworkConflictError(AllocationError):
    """A VLAN or port group name appeared on the live switch between allocation and
    creation. The build stopped cleanly; a retry would collide again."""


def lock_key(domain: str, kind: str) -> int:
    """The advisory-lock key for one (kind, domain): app.network_inventory.service._lock_domain's."""
    return zlib.crc32(f"{kind}:{domain}".encode()) - (1 << 31)  # signed 32-bit


def reserve_values(
    db, range_id: str, *, domain: str, kind: str, pool: list[str], holders: list[str], prune: bool = False,
    excluded: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """{holder: value} for every holder, reserving the lowest free pool values as needed.

    Does not commit. ``prune`` (a fresh build) first frees the range's holders of this kind
    that are not in ``holders``. Raises PoolExhaustedError, reserving nothing, when the
    pool is too small, DomainChangedError when the range holds this kind elsewhere, and
    ReservedNetworkError when it holds an ``excluded`` value. Never hands one out.
    """
    if not domain:
        raise ValueError("a reservation needs the shared network's domain")
    db_ops.lock_reservation_domain(db, lock_key(domain, kind))
    rows = db_ops.range_reservations(db, range_id, kind)
    if elsewhere := sorted({d for _, _, d in rows if d != domain}):
        raise DomainChangedError(
            f"range holds {kind} reservations in {', '.join(elsewhere)}, not {domain}: the allocation domain "
            "changed under it. Restore the setting, or destroy the range and build it again"
        )
    mine = {h: v for h, v, _ in rows}
    if prune and (stale := [h for h in mine if h not in holders]):
        db_ops.drop_range_reservations(db, range_id, kind, stale)
        mine = {h: v for h, v in mine.items() if h in holders}
    if held := sorted({v for v in mine.values() if v in excluded}):
        raise ReservedNetworkError(
            f"range holds reserved {kind} {', '.join(held)} in {domain} (reserved after it was allocated): "
            "destroy the range and build it again"
        )
    needed = [h for h in dict.fromkeys(holders) if h not in mine]
    if not needed:
        return {h: mine[h] for h in holders}
    taken = db_ops.taken_reservation_values(db, domain, kind) | set(excluded)
    # Lazily: a pool may be a large generated sequence (vSphere NIC MACs: 262,144 values).
    free = list(itertools.islice((v for v in pool if v not in taken), len(needed)))
    if len(free) < len(needed):
        outside = len(db_ops.external_reservations(db, domain, kind))
        raise PoolExhaustedError(
            f"{kind} pool for {domain} has {len(free)} free of {len(pool)}; this range needs {len(needed)} more"
            + (f" ({outside} in use outside TrueNorth on the live switch)" if outside else "")
        )
    new = dict(zip(needed, free, strict=False))
    db_ops.insert_reservations(db, range_id, domain, kind, new)
    mine.update(new)
    return {h: mine[h] for h in holders}


def record_external(db, usage, now: datetime | None = None) -> dict[str, str]:
    """Store what one live read (``LiveUsage``) found as external reservations; returns
    {value: holder} recorded for its source. Does not commit.

    Under the (domain, kind) lock: the source's earlier entries are replaced by this read
    (a VLAN freed outside TrueNorth is allocatable again), other sources' entries not seen
    for EXTERNAL_TTL expire. ``managed`` values (TrueNorth's own port groups, by name) are
    recorded only when no live range holds them: an orphan a failed teardown left still
    carries its VLAN. A ``foreign`` value a range holds is a collision on the wire that no
    reservation can fix; it is logged, and that range's next build stops on it.
    """
    db_ops.lock_reservation_domain(db, lock_key(usage.domain, usage.kind))
    db_ops.taken_reservation_values(db, usage.domain, usage.kind)  # frees destroyed ranges' values first
    held = db_ops.range_held_values(db, usage.domain, usage.kind)
    prefix = f"{EXTERNAL}{usage.source}:"
    want = {v: f"{prefix}{name}" for v, name in usage.foreign.items()}
    for v, name in usage.managed.items():
        if v not in held:
            want.setdefault(v, f"{prefix}{name} (orphaned TrueNorth port group)")
    if clash := sorted(set(usage.foreign) & held, key=lambda v: (len(v), v)):
        logger.warning("[alloc] %s %s held by a range is also used outside TrueNorth on %s: %s", usage.kind,
                       ", ".join(clash), usage.source, ", ".join(usage.foreign[v] for v in clash))
    want = {v: h for v, h in want.items() if v not in held}
    now = now or datetime.now(UTC)
    db_ops.expire_external(db, usage.domain, usage.kind, prefix, now - EXTERNAL_TTL)
    rows = db_ops.external_reservations(db, usage.domain, usage.kind)
    ours = [r for r in rows if r.holder.startswith(prefix)]
    db_ops.drop_reservation_ids(db, [r.id for r in ours if r.value not in want])
    db_ops.touch_reservation_ids(db, [r.id for r in ours if r.value in want])
    present = {r.value for r in rows if not r.holder.startswith(prefix) or r.value in want}
    db_ops.insert_external_reservations(db, usage.domain, usage.kind,
                                        {v: h for v, h in want.items() if v not in present})
    return want


def reserve_for_build(session, range_id: str, provisioner, template: dict) -> dict:
    """The allocations ``provisioner.provision`` needs, reserved and committed.

    ``session`` is the worker's session factory (tasks._db_session: commits on success,
    rolls back on an exception). Returns {} at once for a backend or template that needs
    nothing (mock, Proxmox, a lab session on leased port groups), without touching the
    database. A template whose subnets overlap a reserved site network is refused first.

    The provisioner's live read (``live_usage``) happens before the transaction, so no
    hypervisor call holds the lock; the creation-time recheck covers what changes after it.
    """
    if clashes := reserved_networks.conflicts(template):
        raise ReservedNetworkError("template subnets overlap reserved site networks: " + "; ".join(clashes))
    declare = getattr(provisioner, "allocation_needs", None)  # BaseProvisioner's default: none
    needs = declare(range_id, template) if declare else []
    if not needs:
        return {}
    from .fencing import ensure_held  # fencing imports this module

    observe = getattr(provisioner, "live_usage", None)
    try:
        usage = list(observe(needs) or []) if observe else []
    except Exception as exc:  # retryable: the switch could not be read, nothing was reserved
        raise RuntimeError(f"could not read the VLANs in use on the live switch before allocating: {exc}") from exc

    allocations: dict = {}
    with session() as db:
        if not db_ops.lock_range_in_state(db, range_id, "provisioning"):
            raise AllocationError(f"range {range_id} is no longer provisioning; nothing reserved")
        ensure_held(db, range_id)  # an abandoned build reserves nothing for the range's next one
        for need in sorted(needs, key=lambda n: lock_key(n.domain, n.kind)):
            for seen in usage:
                if (seen.domain, seen.kind) == (need.domain, need.kind):
                    record_external(db, seen)
            got = reserve_values(
                db, range_id, domain=need.domain, kind=need.kind, pool=need.pool, holders=need.holders, prune=True,
                excluded=frozenset(getattr(need, "excluded", ()) or ()),
            )
            allocations[need.key] = got[need.holders[0]] if need.single else got
        if planned := provisioner.planned_output(range_id, allocations):
            db_ops.merge_range_output(db, range_id, planned)
    return allocations


def build_failure(session, range_id: str, result) -> Exception:
    """The exception a failed build (``result.status == "failed"``) raises.

    A collision on the live switch (``result.conflict``, range certification S1) becomes
    NetworkConflictError, final, after one transaction that frees the VLANs the range no
    longer needs (``release``), drops their networks from its recorded output, and records
    the colliding VLANs as external. Only while the range is still provisioning; if that
    transaction fails, the range keeps its values until its destroy (as any failed build).
    Anything else is a RuntimeError, retried as before."""
    message = "; ".join(result.errors) or "Provisioning failed"
    conflict = getattr(result, "conflict", None)
    if not conflict:
        return RuntimeError(message)
    try:
        with session() as db:
            if db_ops.lock_range_in_state(db, range_id, "provisioning"):
                release_conflict(db, range_id, conflict)
    except Exception as exc:  # noqa: BLE001 — the build is failed either way; say why the values stay
        logger.warning("[alloc] range %s: VLANs not released after the collision (%s)", range_id, exc)
        message += f" (its VLAN reservations were kept: {exc})"
    return NetworkConflictError(message)


def release_conflict(db, range_id: str, conflict: dict) -> None:
    """``build_failure``'s transaction. Does not commit."""
    domain, source = conflict["domain"], conflict.get("source") or "live switch"
    db_ops.lock_reservation_domain(db, lock_key(domain, "vlan"))
    release = [str(v) for v in conflict.get("release") or []]
    freed = db_ops.drop_range_values(db, range_id, domain, "vlan", release)
    db_ops.drop_range_networks(db, range_id, {int(v) for v in release})
    taken = db_ops.taken_reservation_values(db, domain, "vlan")
    db_ops.insert_external_reservations(db, domain, "vlan", {
        str(v): f"{EXTERNAL}{source}:{name}" for v, name in (conflict.get("vlans") or {}).items()
        if str(v) not in taken})
    logger.warning("[alloc] range %s: VLAN collision on %s (%s); released %d reservation(s)", range_id, source,
                   ", ".join(f"{v}: {n}" for v, n in (conflict.get("vlans") or {}).items()), freed)


def release_after_destroy(session, range_id: str) -> int:
    """Free everything the range holds, once it is recorded destroyed (only then: a late or
    duplicate call must not free a live range's values). Returns how many.

    Best effort: the range is already destroyed, so a failure here must not fail (or retry)
    the destroy; the values are reclaimed by the next reservation in their domain anyway."""
    try:
        with session() as db:
            return db_ops.release_destroyed_range(db, range_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[destroy] range %s: reservations not released now (%s); the next reservation in "
                       "their domain reclaims them", range_id, exc)
        return 0


def stored_output(backend: str, range_id: str, result, provisioner=None) -> dict:
    """What provision_range records as the range's provisioner_output: the built VMs and
    networks, and (vSphere) the edge uplink, mirror sessions and warnings, which destroy
    and the range pages read back, and the endpoint it was built on (``connection``:
    worker/connection_binding.py), which every later operation must reach again."""
    out = {"provider": backend, "range_id": range_id, "vms": result.vms, "networks": result.networks}
    for key in ("uplink", "mirrors", "warnings", "not_ready"):
        if getattr(result, key, None):
            out[key] = getattr(result, key)
    record = getattr(provisioner, "connection_record", None)  # BaseProvisioner's; None: not one
    rec = record() if callable(record) else None
    if isinstance(rec, dict) and rec:
        out["connection"] = rec
    return out


def build_outcome(result) -> tuple[str, str | None]:
    """The state a finished (not ``failed``) build records, and its error: ``ready``, or
    ``failed`` when a VM was built but is not ready (``result.not_ready``; vSphere: its
    planned guest address never showed in VMware Tools, vsphere_readiness.py).

    Not ``ready`` with the plan's addresses on show (2026-10-20). Recorded as ``failed``
    with the built output, not raised: a retry would clone the same VMs again, and the
    recorded VMs are what destroy, and the API's "destroy it first" check, work from."""
    names = list(getattr(result, "not_ready", None) or [])
    if not names:
        return "ready", None
    reasons = [e for e in result.errors if any(e.startswith(f"VM {n}:") for n in names)] or names
    return "failed", f"{len(names)} VM(s) built but not ready: " + "; ".join(reasons)
