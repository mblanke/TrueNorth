"""Reserve a build's shared values (VLANs, uplink addresses) before the provisioner runs.

The provisioner says what it needs (``BaseProvisioner.allocation_needs``); everything is
reserved on the API's ``network_reservations`` table in one transaction
(``db_ops.reserve_values``: unique per domain across tenants, PostgreSQL advisory lock per
domain, no Redis). Locks are taken in key order, so two builds that both need VLANs and an
uplink address cannot deadlock. The reservation commits before the build starts and is
recorded in provisioner_output (``planned_output``), so a build that dies half-way still
holds its values and its destroy still finds them. A retry gets the same values. The
destroy releases them (tasks.destroy_range).
"""

from __future__ import annotations

from . import db_ops


def reserve_for_build(session, range_id: str, provisioner, template: dict) -> dict:
    """The allocations ``provisioner.provision`` needs, reserved and committed. ``session`` is
    the worker's session factory (tasks._db_session)."""
    needs = provisioner.allocation_needs(range_id, template)
    if not needs:
        return {}
    allocations: dict = {}
    with session() as db:
        for need in sorted(needs, key=lambda n: db_ops.reservation_lock_key(n.domain, n.kind)):
            got = db_ops.reserve_values(
                db, range_id, domain=need.domain, kind=need.kind, pool=need.pool, holders=need.holders
            )
            allocations[need.key] = got[need.holders[0]] if need.single else got
        if planned := provisioner.planned_output(range_id, allocations):
            db_ops.merge_range_output(db, range_id, planned)
    return allocations
