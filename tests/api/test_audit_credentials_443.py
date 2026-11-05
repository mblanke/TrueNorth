"""Failing oracle tests for audit issue #443 (range credentials and rotation).

Card TN10X-AUDIT-tests-creds: one test per confirmed audit finding that is still present
at the base commit (280a838b), each naming its finding (file:line,
title) in its docstring, each asserting the *correct* behaviour — so it fails until the
finding is fixed, and becomes that fix's acceptance check. No app code is changed here.

Status of the audit's findings at this base:

* PRESENT — #443-1 ``service.py:450`` rotate of a never-applied account settles applied:
  :test:`test_rotation_of_a_never_applied_account_cannot_settle_as_applied`;
* PRESENT — #443-2 ``ranges.py:964`` deleting a snapshot over a pending restore rotation:
  :test:`test_deleting_a_snapshot_while_its_restore_rotation_is_pending_is_refused`;
* PRESENT — #443-3 ``service.py:414`` force_settle racing a still-running worker task:
  :test:`test_force_settling_while_the_worker_task_is_still_running_leaves_a_lie_behind`;
* PRESENT — #443-4 ``credential_rotation.py:258`` a failed post-restore send strands every
  pending row: :test:`test_a_failed_post_restore_rotation_send_must_not_strand_the_rows`;
* FIXED — the audit's console finding (a console closed when its sign-in token expired) was
  repaired in #442 (commits 54dca7af, bcff2c74, both in this base) and is covered by the
  tests that fix added (tests/api/test_console_relay.py, tests/api/test_class_consoles_321.py);
  no test here, and none that could fail.

Fixtures come from the neighbouring files by module path (never ``from conftest import``):
``client``/``db_session`` live in tests/conftest.py; the ``_range``/``_row``/``_rotate``/
``_snapshot``/``_settle`` helpers and the shared building blocks in
``test_range_credentials_hardening`` and ``_shared``. The ``world`` and ``no_tasks``
fixtures are copied from ``test_range_credentials_hardening`` verbatim (their bodies, not
their imports, so no fixture parameter shadows an imported name).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import RangeSnapshot, RangeState, UserRole
from app.range_credentials import service as rc
from test_range_credentials_hardening import (
    _range,
    _rotate,
    _row,
    _settle,
    _snapshot,
)


@pytest.fixture
def world(db_session):
    """The hardening suite's two-tenant world (tenant ``a`` with its admin, instructor and
    range_ops, tenant ``b`` with a foreign admin), built with the same helpers."""
    a, b = real_tenant(db_session, "audit-a"), real_tenant(db_session, "audit-b")
    w = {"a": a, "b": b}
    for name, role, tenant in (("admin", UserRole.admin, a), ("instructor", UserRole.instructor, a),
                               ("range_ops", UserRole.range_ops, a), ("foreign_admin", UserRole.admin, b)):
        w[name] = real_user(db_session, role, tenant.id)
    return w


@pytest.fixture
def no_tasks(monkeypatch):
    """The hardening suite's dispatch recorder: every Celery send is recorded, none leaves."""
    from app import celery_client
    from app.routers import ranges as ranges_router

    sent = []
    monkeypatch.setattr(ranges_router, "_dispatch_task", lambda name, *a: sent.append((name, *a)) or "t")
    monkeypatch.setattr(celery_client, "dispatch", lambda name, *a: sent.append((name, *a)) or "t")
    return sent


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Admin rotation of a never-applied account settles as applied=True with a password the guest never had (control-plane/api/app/range_credentials/service.py:450)")
def test_rotation_of_a_never_applied_account_cannot_settle_as_applied(client, db_session, world, no_tasks):
    """#443-1 (high) — control-plane/api/app/range_credentials/service.py:450:
    "Admin rotation of a never-applied account: a failed guest change settles as applied=True
    with a password the guest never had; certification flips to PASS".

    The build never verified the account (``applied=False``: the panel says not_set and the
    certification says INCOMPLETE). An admin rotates it to "fix" that; the worker cannot set
    it in the guest, and the change fails. Correct behaviour: the row must never end
    ``applied=True`` with a password no build or rotation ever verified — either ``rotate()``
    refuses a not-applied row of a built range (the guard ``on_restore`` already has at
    service.py:353) or a failed change of a never-applied row settles as not applied. Today
    the rotation is accepted (``rotate`` never looks at ``applied``) and the worker's failure
    path (``settle_credential_change``, worker/db_ops.py:332) writes ``applied=True`` with
    ``previous_password`` — the pre-rotation value, which the guest never had.
    """
    from worker import db_ops

    rng = _range(client, db_session, world, RangeState.ready)
    row = _row(db_session, rng, "web01", "analyst")
    row.applied = False  # the build never set or verified this account in the guest
    db_session.commit()
    act_as(world["admin"])
    resp = _rotate(client, rng, row)
    if resp.status_code != 202:  # a refusal of a not-applied row also fixes the finding
        assert resp.status_code == 409, resp.text
        return
    operation = resp.json()["operation_id"]
    db_session.expire_all()
    row = _row(db_session, rng, "web01", "analyst")
    # The worker's real failure path (credential_rotation._settle -> settle_credential_change):
    # the guest did not take the new password.
    assert db_ops.settle_credential_change(db_session, str(rng.id), str(row.id), row.sealed_password,
                                           applied=False, operation=operation)
    db_session.expire_all()
    row = _row(db_session, rng, "web01", "analyst")
    assert not row.applied or row.pending, (
        "a failed rotation of an account the build never set settled as applied=True with the "
        "pre-rotation password, which the guest never had: the Student panel calls it verified "
        "and the certification's INCOMPLETE turns into a PASS no build ever earned")


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Deleting a snapshot right after restoring it destroys the passwords the queued rotation needs (control-plane/api/app/routers/ranges.py:964)")
def test_deleting_a_snapshot_while_its_restore_rotation_is_pending_is_refused(client, db_session, world, no_tasks):
    """#443-2 (high) — control-plane/api/app/routers/ranges.py:964:
    "Deleting a snapshot right after restoring it destroys the passwords the queued rotation
    needs; Administrator can be locked out and reveal lies".

    A restore's task has put the snapshot back to ``ready`` (its revert ran) while its
    ``restore:<id>`` password rotation is still pending — exactly the window between the
    task returning and the rotation dispatch (worker/credential_rotation.py:258) settling.
    Correct behaviour: deleting the snapshot now is refused with 409, the way a restore
    refuses over a pending change (ranges.py:901): ``forget_snapshot``
    (range_credentials/service.py:310) drops the RangeCredentialSnapshot rows the queued
    rotation logs in with and reverts to, so the worker finds no snapshot-time password,
    none of the machine's admin logins is accepted, and the change settles as a lie.
    """
    rng = _range(client, db_session, world, RangeState.ready)
    act_as(world["admin"])
    snap = _snapshot(client, db_session, rng)
    assert _rotate(client, rng, _row(db_session, rng, "web01", "ubuntu")).status_code == 202
    _settle(db_session, _row(db_session, rng, "web01", "ubuntu").id)  # the worker verified it
    assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
    db_session.get(RangeSnapshot, snap.id).snapshot_state = "ready"  # the revert ran; rotation queued
    db_session.commit()
    assert rc.any_pending(db_session, rng)  # the restore:<id> rotation is still being applied
    assert client.delete(f"/ranges/{rng.id}/snapshots/{snap.id}").status_code == 409, (
        "deleting the snapshot while its restore rotation is pending destroys the recorded "
        "passwords that rotation logs in with and reverts to")


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: force_settle has no guard against a still-running worker task; the later CAS miss is logged at INFO (control-plane/api/app/range_credentials/service.py:414)")
def test_force_settling_while_the_worker_task_is_still_running_leaves_a_lie_behind(client, db_session, world,
                                                                                   no_tasks):
    """#443-3 (medium) — control-plane/api/app/range_credentials/service.py:414:
    "force_settle has no guard against a still-running worker task; the later CAS miss is
    logged at INFO and leaves the row showing a password the guest no longer has".

    The rotation task is merely slow (RANGE_ROTATION_TIMEOUT defaults to 900 s). The admin
    assumes it died and force-settles the account; the worker then succeeds in the guest
    anyway, and its compare-and-set (worker/db_ops.py:336) misses — reported only at INFO.
    Correct behaviour: the worker's settle of the version it set wins, so the row ends with
    the password the guest actually has. Today ``force_settle`` has already overwritten the
    row, so the CAS misses and the row keeps the old password, which no longer logs in —
    and that is what reveal hands out.
    """
    from worker import db_ops

    rng = _range(client, db_session, world, RangeState.ready)
    p0 = _row(db_session, rng, "web01", "analyst").password
    act_as(world["admin"])
    assert _rotate(client, rng, _row(db_session, rng, "web01", "analyst")).status_code == 202
    db_session.expire_all()
    # What the task read and is about to set (credential_rotation.run -> account_rows):
    rows = {r["id"]: r for r in db_ops.account_rows(db_session, str(rng.id))}
    row = _row(db_session, rng, "web01", "analyst")
    task = rows[str(row.id)]
    sealed, new_pw, operation = task["sealed"], task["password"], task["operation"]
    assert new_pw != p0  # the task carries a new password, not yet in the guest
    # The admin force-settles while the task is still in flight:
    resp = client.post(f"/ranges/{rng.id}/credentials/{row.id}/settle", json={"reason": "assumed stuck"})
    if resp.status_code != 200:  # a refusal while the task can still be in flight also fixes it
        assert resp.status_code == 409, resp.text
        return
    # ... and the task then succeeds in the guest and settles the version it set:
    landed = db_ops.settle_credential_change(db_session, str(rng.id), str(row.id), sealed,
                                             applied=True, operation=operation)
    db_session.expire_all()
    row = _row(db_session, rng, "web01", "analyst")
    assert landed and row.password == new_pw, (
        "the worker's settle of the password it set lost to the admin's force_settle (only an "
        "INFO line): the row now shows the old password, which the guest no longer accepts, "
        "and reveal hands it out")


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Post-restore rotation dispatched only from a task_success signal; a failed send strands every pending row (control-plane/worker/worker/credential_rotation.py:258)")
def test_a_failed_post_restore_rotation_send_must_not_strand_the_rows(client, db_session, world, no_tasks,
                                                                      monkeypatch):
    """#443-4 (low) — control-plane/worker/worker/credential_rotation.py:258:
    "Post-restore rotation is dispatched only from a Celery task_success signal; if that send
    fails every Student row of the class stays pending with no sweep, only per-account manual
    settle".

    The restore succeeded (its revert ran: the guests have the snapshot's passwords) but the
    broker refuses the rotation task. Correct behaviour: the pending ``restore:<id>`` changes
    are not stranded — this test accepts the rollback direction (they go back to the snapshot
    seal the reverted guests actually have; ``roll_back_operation`` settles to
    ``previous_password`` instead — it passes no ``revert_to`` — which the second assertion
    rejects); a fix that only re-enqueues the send asynchronously leaves the rows pending at
    this instant and needs its own assertion on the retry. Today there is neither: one warning,
    every row stays pending, every later
    restore of the range is refused 409 (ranges.py:901) and one manual ``POST .../settle``
    per account is the only recovery.
    """
    import worker.tasks as tasks
    from worker import credential_rotation as cr

    @contextmanager
    def _api_db():  # a fix's rollback runs through the worker's session factory
        yield db_session

    monkeypatch.setattr(tasks, "_db_session", _api_db)

    def down(*a, **k):
        raise ConnectionError("broker down")

    # On the class, as conftest's no_real_broker does: patching send_task on the app
    # instance would outlive monkeypatch's undo — the undo writes the recorder it removed
    # back as a permanent instance attribute, shadowing every later test's recorder
    # (the trap test_inject_dispatch.py's refused-telemetry test warns about).
    from celery import Celery

    monkeypatch.setattr(Celery, "send_task", down)  # the broker refuses the rotation send
    rng = _range(client, db_session, world, RangeState.ready)
    act_as(world["admin"])
    snap = _snapshot(client, db_session, rng)
    p0 = _row(db_session, rng, "web01", "analyst").password  # the password the snapshot sealed
    assert _rotate(client, rng, _row(db_session, rng, "web01", "analyst")).status_code == 202
    _settle(db_session, _row(db_session, rng, "web01", "analyst").id)
    assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
    db_session.get(RangeSnapshot, snap.id).snapshot_state = "ready"  # the revert ran
    db_session.commit()
    assert rc.any_pending(db_session, rng)
    cr._after_restore(sender=SimpleNamespace(name=cr.RESTORE_TASK,
                                             request=SimpleNamespace(args=[str(rng.id), str(snap.id)])),
                      result={"status": "restored", "range_id": str(rng.id)})
    assert not rc.any_pending(db_session, rng), (
        "the rotation send failed and the restore's pending password changes are stranded: "
        "nothing sweeps or retries them, every later restore of the range is refused 409 and "
        "the only recovery is one manual settle per account")
    row = _row(db_session, rng, "web01", "analyst")
    assert row.pending or row.password == p0, (
        "the rows were put back to their pre-restore password, not to the snapshot seal the "
        "reverted guests actually have: reveal hands out a password that no longer logs in")
