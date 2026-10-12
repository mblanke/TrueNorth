"""Which hypervisor endpoint a range lives on: decided once, at build, and kept.

Review of #131 (per-tenant vCenter credentials), 2026-10-11: every operation re-resolved
the range's connection (its tenant's active one, else a shared one, else VSPHERE_*), and
nothing recorded which vCenter a range was built on. A tenant adding, deactivating or
deleting a connection silently moved its live ranges' destroy, power and snapshots to a
different vCenter, which then acted on bare ``vm-NN`` ids there.

The contract, for a backend that takes connection credentials (CREDENTIALS_HYPERVISOR):

* **Record.** A build writes ``provisioner_output["connection"] = {"id", "host"}`` before
  any VM exists (``record_build``): ``id`` is the ``hypervisor_connections`` row, or None
  for the worker's VSPHERE_* environment; ``host`` the endpoint URL logged in to. The
  output stored at ``ready`` keeps it (range_alloc.stored_output).
* **Resolve.** Every later operation, and a retried build, takes exactly that endpoint:
  the recorded row (active or not, still the range's tenant's or shared), or the
  environment. A row that is gone is a ``ConnectionBindingError``, never a fallback.
* **Verify.** The provisioner built from it must still reach the recorded host; a row
  edited to another host, or an environment that now names another vCenter, is refused.
* **Legacy.** A range with nothing recorded (built before this, or not built yet) uses
  today's lookup. The provisioner's range-ownership check (VM annotation) still guards
  what it deletes, powers or snapshots.

``ConnectionBindingError`` is final (fencing.FINAL_ERRORS): retrying cannot bring a
deleted connection back. The API refuses to deactivate, repoint or delete a connection
that live ranges are on (app/hypervisor_connection_guard.py), so this is the backstop.
"""

from __future__ import annotations

import json

from .provisioners import CREDENTIALS_HYPERVISOR, ConnectionBindingError

KEY = "connection"


def recorded(output) -> dict | None:
    """The endpoint a range's provisioner_output records, or None (nothing recorded)."""
    rec = output.get(KEY) if isinstance(output, dict) else None
    return rec if isinstance(rec, dict) and rec.get("host") else None


def _output(db, range_id: str) -> dict:
    from . import db_ops

    row = db_ops.range_output_and_backend(db, range_id)
    try:
        out = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        out = {}
    return out if isinstance(out, dict) else {}


def resolve(backend: str, range_id: str | None, output: dict | None = None) -> tuple[dict, dict | None]:
    """(credentials for get_provisioner, the recorded endpoint or None) for an operation
    on ``range_id``. ``output``: its provisioner_output when the caller has it already.
    {} credentials: the backend's environment."""
    hypervisor = CREDENTIALS_HYPERVISOR.get(backend)
    if not hypervisor or not range_id:
        return {}, None
    from . import db_ops
    from .tasks import _db_session  # at call time: tasks imports base_tasks, which imports this

    with _db_session() as db:
        rec = recorded(output if output is not None else _output(db, range_id))
        if rec is None:  # legacy, or a first build: today's lookup
            return db_ops.hypervisor_creds(db, hypervisor, range_id), None
        if not rec.get("id"):
            return {}, rec  # built on the environment's vCenter
        creds = db_ops.hypervisor_creds_by_id(db, hypervisor, str(rec["id"]), range_id)
    if not creds:
        raise ConnectionBindingError(
            f"range {range_id} was built on {rec['host']} through hypervisor connection {rec['id']}, which no "
            "longer exists (or is no longer this tenant's). Refusing to reach any other vCenter; restore the "
            "connection to act on the range."
        )
    return creds, rec


def verify(provisioner, rec: dict | None) -> None:
    """Refuse a provisioner that does not reach the recorded endpoint (None: nothing to check)."""
    if rec is None:
        return
    now = provisioner.connection_record() or {}
    if now.get("host") != rec.get("host") or (now.get("id") or None) != (rec.get("id") or None):
        source = f"hypervisor connection {rec['id']}" if rec.get("id") else "the worker's VSPHERE_* settings"
        raise ConnectionBindingError(
            f"range was built on {rec['host']} ({source}), which now points at {now.get('host') or 'nothing'}. "
            "Refusing to act on another vCenter by VM id; point the connection back to act on the range."
        )


def record_build(range_id: str, provisioner) -> None:
    """Record the endpoint a build is about to use, before any VM exists, so a destroy
    after a build that died half-way goes to the same one. Only while the range is still
    provisioning and (inside the fenced task) this execution still holds it."""
    rec = provisioner.connection_record()
    if not isinstance(rec, dict) or not rec:
        return
    from . import db_ops
    from .fencing import ensure_held  # fencing imports the provisioners package
    from .tasks import _db_session

    with _db_session() as db:
        if db_ops.lock_range_in_state(db, range_id, "provisioning"):
            ensure_held(db, range_id)
            db_ops.merge_range_output(db, range_id, {KEY: rec})
