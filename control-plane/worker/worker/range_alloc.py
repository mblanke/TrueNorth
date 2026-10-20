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

tasks.provision_range calls ``reserve_for_build`` and ``stored_output``;
tasks.destroy_range calls ``release_after_destroy``. ``AllocationError`` is final
(fencing.FINAL_ERRORS): a full pool is not fixed by retrying.
"""

from __future__ import annotations

import itertools
import logging
import zlib

from . import db_ops

logger = logging.getLogger("truenorth.worker")


class AllocationError(RuntimeError):
    """A build's shared values could not be reserved; retrying will not help by itself."""


class PoolExhaustedError(AllocationError):
    """The pool cannot hold every holder that needs a value."""


class DomainChangedError(AllocationError):
    """The range holds values of this kind in another domain (the setting that names the
    domain changed under a live range). Refused: its old values still exist physically."""


def lock_key(domain: str, kind: str) -> int:
    """The advisory-lock key for one (kind, domain): app.network_inventory.service._lock_domain's."""
    return zlib.crc32(f"{kind}:{domain}".encode()) - (1 << 31)  # signed 32-bit


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
    needed = [h for h in dict.fromkeys(holders) if h not in mine]
    if not needed:
        return {h: mine[h] for h in holders}
    taken = db_ops.taken_reservation_values(db, domain, kind)
    # Lazily: a pool may be a large generated sequence (vSphere NIC MACs: 262,144 values).
    free = list(itertools.islice((v for v in pool if v not in taken), len(needed)))
    if len(free) < len(needed):
        raise PoolExhaustedError(
            f"{kind} pool for {domain} has {len(free)} free of {len(pool)}; this range needs {len(needed)} more"
        )
    new = dict(zip(needed, free, strict=False))
    db_ops.insert_reservations(db, range_id, domain, kind, new)
    mine.update(new)
    return {h: mine[h] for h in holders}


def reserve_for_build(session, range_id: str, provisioner, template: dict) -> dict:
    """The allocations ``provisioner.provision`` needs, reserved and committed.

    ``session`` is the worker's session factory (tasks._db_session: commits on success,
    rolls back on an exception). Returns {} at once for a backend or template that needs
    nothing (mock, Proxmox, a lab session on leased port groups), without touching the
    database.
    """
    declare = getattr(provisioner, "allocation_needs", None)  # BaseProvisioner's default: none
    needs = declare(range_id, template) if declare else []
    if not needs:
        return {}
    from .fencing import ensure_held  # fencing imports this module

    allocations: dict = {}
    with session() as db:
        if not db_ops.lock_range_in_state(db, range_id, "provisioning"):
            raise AllocationError(f"range {range_id} is no longer provisioning; nothing reserved")
        ensure_held(db, range_id)  # an abandoned build reserves nothing for the range's next one
        for need in sorted(needs, key=lambda n: lock_key(n.domain, n.kind)):
            got = reserve_values(
                db, range_id, domain=need.domain, kind=need.kind, pool=need.pool, holders=need.holders, prune=True
            )
            allocations[need.key] = got[need.holders[0]] if need.single else got
        if planned := provisioner.planned_output(range_id, allocations):
            db_ops.merge_range_output(db, range_id, planned)
    return allocations


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
