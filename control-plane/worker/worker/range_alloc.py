"""Reserve a build's shared values (VLANs, uplink addresses) before the provisioner runs.

The provisioner says what it needs (``BaseProvisioner.allocation_needs``); everything is
reserved on the API's ``network_reservations`` table in one transaction
(``db_ops.reserve_values``: unique per domain across tenants, PostgreSQL advisory lock per
domain, no Redis). Locks are taken in key order, so two builds that both need VLANs and an
uplink address cannot deadlock. The reservation commits before the build starts and is
recorded in provisioner_output (``planned_output``), so a build that dies half-way still
holds its values and its destroy still finds them. A retry gets the same values; values the
range held for holders this build no longer has are freed. The range's row is locked and
must still be ``provisioning``: an operator may have abandoned (and destroyed) it since the
task claimed it, and then it must reserve nothing. The destroy releases them
(tasks.destroy_range).
"""

from __future__ import annotations

from . import db_ops
from .fencing import PermanentError


def reserve_for_build(session, range_id: str, provisioner, template: dict) -> dict:
    """The allocations ``provisioner.provision`` needs, reserved and committed. ``session`` is
    the worker's session factory (tasks._db_session)."""
    needs = provisioner.allocation_needs(range_id, template)
    if not needs:
        return {}
    allocations: dict = {}
    with session() as db:
        if not db_ops.claim_range(db, range_id, "provisioning"):
            raise PermanentError(f"range {range_id} is no longer provisioning; nothing reserved")
        for need in sorted(needs, key=lambda n: db_ops.reservation_lock_key(n.domain, n.kind)):
            got = db_ops.reserve_values(
                db, range_id, domain=need.domain, kind=need.kind, pool=need.pool, holders=need.holders, prune=True
            )
            allocations[need.key] = got[need.holders[0]] if need.single else got
        if planned := provisioner.planned_output(range_id, allocations):
            db_ops.merge_range_output(db, range_id, planned)
    return allocations
