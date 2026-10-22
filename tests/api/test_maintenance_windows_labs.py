"""Maintenance windows and Student lab sessions (ADR 0013 §7, range certification S7).

A lab's reset (a snapshot restore or a rebuild) is refused inside a window, and a new lab
waits in its queue until the window ends. Its teardowns never wait: ending it, its expiry
and the cleanup of a failed build all run inside a window.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.lab_sessions import service
from app.maintenance_windows import MaintenanceWindow
from app.models import Range, RangeState
from test_lab_sessions import act, lab, launch, student, until, worker  # noqa: F401 — fixtures

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _window(db, **extra) -> MaintenanceWindow:
    now = datetime.now(UTC)
    w = MaintenanceWindow(
        id=uuid.uuid4(), tenant_id=DEV, reason="storage maintenance", starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(hours=1), scope="all", range_ids=[], tags=[], **extra,
    )
    db.add(w)
    db.commit()
    return w


def _calls(fake_worker, name):
    return [c for c in fake_worker.calls if c[0] == name]


def test_a_new_lab_waits_in_its_queue_during_a_window(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    _, rid, _ = lab
    w = _window(db_session)
    s, _ = launch(db_session, student(db_session), rid)
    assert s.state == "queued" and "maintenance window" in s.error and "storage maintenance" in s.error
    assert _calls(worker, "provision_range") == []
    db_session.delete(w)
    db_session.commit()
    act(db_session, service.advance, s)
    assert s.state != "queued" and _calls(worker, "provision_range")


def test_a_reset_is_refused_during_a_window(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    client, rid, _ = lab
    s, _ = launch(db_session, student(db_session), rid)
    until(db_session, s, "ready")
    _window(db_session)
    worker.calls.clear()
    try:
        act(db_session, service.reset, s)
        raise AssertionError("a reset inside a window must be refused")
    except service.LabRefusedError as exc:
        assert exc.status == 409 and "Resetting is refused" in str(exc) and "storage maintenance" in str(exc)
    assert s.state == "ready" and worker.calls == []


def test_ending_a_lab_tears_it_down_inside_a_window(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    _, rid, _ = lab
    s, _ = launch(db_session, student(db_session), rid)
    until(db_session, s, "ready")
    _window(db_session)
    act(db_session, service.end, s)
    assert _calls(worker, "destroy_range")
    until(db_session, s, "destroyed")


def test_an_expired_lab_is_torn_down_inside_a_window(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    _, rid, _ = lab
    s, _ = launch(db_session, student(db_session), rid)
    until(db_session, s, "ready")
    _window(db_session)
    s.max_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    act(db_session, service.advance, s)
    assert s.end_reason == "expired" and _calls(worker, "destroy_range")
    until(db_session, s, "destroyed")


def test_a_failed_build_is_cleaned_up_inside_a_window(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    _, rid, _ = lab
    worker.hold = True
    s, _ = launch(db_session, student(db_session), rid)
    _window(db_session)  # opened after the launch was accepted
    worker.fail_provision.add(str(s.range_id))
    worker.run()
    act(db_session, service.advance, s)
    assert s.state == "cleaning" and _calls(worker, "destroy_range")
    worker.run()
    assert db_session.get(Range, s.range_id).state == RangeState.destroyed


def test_a_rebuild_reset_accepted_before_a_window_rebuilds_after_it(lab, db_session, worker, monkeypatch):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    _, rid, _ = lab
    monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
    monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01,pg-lab-02")
    monkeypatch.setattr(
        service,
        "_profile",
        lambda db, s: {
            **service.release_profile(db, db.get(service.CourseRelease, s.release_id), s.activity_id),
            "reset": {"mode": "rebuild"},
        },
    )
    s, _ = launch(db_session, student(db_session), rid)
    until(db_session, s, "ready")
    old = s.range_id
    act(db_session, service.reset, s)  # the old range is torn down (that always runs)
    assert db_session.get(Range, old).state == RangeState.destroyed
    w = _window(db_session)
    worker.calls.clear()
    act(db_session, service.advance, s)
    assert s.state == "resetting" and s.range_id == old and "maintenance window" in s.error
    assert _calls(worker, "provision_range") == []
    db_session.delete(w)
    db_session.commit()
    until(db_session, s, "ready")
    assert s.range_id != old


def test_a_build_the_worker_held_is_sent_again_after_the_window(lab, db_session, worker):  # noqa: F811
    """The worker puts a lab's range back to created when a window covers it (never failed)."""
    _, rid, _ = lab
    worker.hold = True
    s, _ = launch(db_session, student(db_session), rid)
    worker.queue.clear()  # the worker held this build: the range goes back to created
    rng = db_session.get(Range, s.range_id)
    rng.state, rng.error_message = RangeState.created, "Provisioning is held by the worker: a maintenance window"
    db_session.commit()
    w = _window(db_session)
    worker.calls.clear()
    act(db_session, service.advance, s)
    assert s.state == "provisioning" and "maintenance window" in s.error and worker.calls == []
    db_session.delete(w)
    db_session.commit()
    act(db_session, service.advance, s)
    assert _calls(worker, "provision_range") and db_session.get(Range, s.range_id).state == RangeState.provisioning
    worker.hold = False
    worker.run()
    until(db_session, s, "ready")
