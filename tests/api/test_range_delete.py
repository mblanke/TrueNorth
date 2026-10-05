"""DELETE /ranges/{id}: refuse while anything depends on the range, never a 500.

It used to call db.delete() and let the database decide. A range with an exercise,
or with a scheduled event, then failed on a foreign key
(`exercises_range_id_fkey`) and the IntegrityError came back as a 500. The rule now
lives in `routers/ranges.py` next to `_DELETABLE_RANGE_STATES`.

`ranges.id` is referenced by exercises, range_snapshots, range_documents and
scheduled_events. Each has a case here.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

DEV_TENANT = "00000000-0000-0000-0000-000000000001"  # what AUTH_DISABLED signs in as
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _range(db, state: str = "destroyed", tenant_id: str = DEV_TENANT):
    from app.models import Range, RangeState

    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(tenant_id), name=f"range-{state}", template_id=uuid.uuid4())
    r.state = RangeState(state)
    db.add(r)
    db.flush()
    return r


def _exercise(db, rng, deleted: bool = False):
    from app.models import Exercise

    ex = Exercise(id=uuid.uuid4(), name="ex", range_id=rng.id, tenant_id=rng.tenant_id)
    if deleted:
        ex.soft_delete()
    db.add(ex)
    db.flush()
    return ex


def _event(db, rng, state: str):
    from app.scheduler.models import EventState, ScheduledEvent

    start = datetime.now(UTC)
    ev = ScheduledEvent(
        id=uuid.uuid4(),
        name=f"event-{state}",
        state=EventState(state),
        tenant_id=rng.tenant_id,
        range_id=rng.id,
        start_time=start,
        end_time=start + timedelta(hours=2),
    )
    db.add(ev)
    db.flush()
    return ev


def _exists(db, model, row_id) -> bool:
    db.expire_all()
    return db.get(model, row_id) is not None


class TestRefusals:
    def test_a_range_with_an_exercise_is_kept_409(self, client, db_session):
        """The reported bug: this was a 500 from exercises_range_id_fkey."""
        from app.models import Exercise, Range

        rng = _range(db_session)
        ex = _exercise(db_session, rng)
        db_session.commit()

        resp = client.delete(f"/ranges/{rng.id}")

        assert resp.status_code == 409, resp.text
        assert "1 exercise(s)" in resp.json()["detail"]
        assert _exists(db_session, Range, rng.id)
        assert _exists(db_session, Exercise, ex.id)

    def test_a_soft_deleted_exercise_still_counts(self, client, db_session):
        """Its row still holds the foreign key, so the range cannot go."""
        rng = _range(db_session)
        _exercise(db_session, rng, deleted=True)
        db_session.commit()

        assert client.delete(f"/ranges/{rng.id}").status_code == 409

    @pytest.mark.parametrize("state", ["provisioning", "ready", "running", "stopped", "destroying", "failed"])
    def test_a_range_that_may_still_have_vms_is_kept_409(self, client, db_session, state):
        """Deleting the row would orphan its VMs on the hypervisor; destroy comes first."""
        from app.models import Range

        rng = _range(db_session, state)
        db_session.commit()

        resp = client.delete(f"/ranges/{rng.id}")

        assert resp.status_code == 409
        assert "destroy it first" in resp.json()["detail"]
        assert _exists(db_session, Range, rng.id)

    @pytest.mark.parametrize("state", ["draft", "scheduled", "active"])
    def test_a_range_a_scheduled_event_still_reserves_is_kept_409(self, client, db_session, state):
        rng = _range(db_session)
        _event(db_session, rng, state)
        db_session.commit()

        resp = client.delete(f"/ranges/{rng.id}")

        assert resp.status_code == 409
        assert "scheduled event" in resp.json()["detail"]

    def test_another_tenants_range_is_not_found(self, client, db_session):
        from app.models import Range

        theirs = _range(db_session, tenant_id=OTHER_TENANT)
        db_session.commit()

        assert client.delete(f"/ranges/{theirs.id}").status_code == 404
        assert _exists(db_session, Range, theirs.id)

    def test_an_unhandled_reference_is_409_not_500(self, client, db_session):
        """A foreign key added later, which the endpoint does not know about yet."""
        rng = _range(db_session)
        db_session.commit()

        fk_error = IntegrityError("DELETE FROM ranges", {}, Exception("violates foreign key constraint"))
        with patch.object(db_session, "commit", side_effect=fk_error):
            resp = client.delete(f"/ranges/{rng.id}")

        assert resp.status_code == 409
        assert "still referenced" in resp.json()["detail"]


class TestDeletion:
    @pytest.mark.parametrize("state", ["created", "destroyed"])
    def test_a_range_nothing_depends_on_is_deleted(self, client, db_session, state):
        from app.models import Range

        rng = _range(db_session, state)
        db_session.commit()

        assert client.delete(f"/ranges/{rng.id}").status_code == 204
        assert not _exists(db_session, Range, rng.id)

    def test_snapshots_and_documents_go_with_it_including_stored_bytes(self, client, db_session):
        from app.models import RangeDocument, RangeSnapshot

        rng = _range(db_session)
        snap = RangeSnapshot(
            range_id=rng.id,
            name="s",
            range_state_at_snapshot="ready",
            tenant_id=rng.tenant_id,
            snapshot_state="ready",
        )
        doc = RangeDocument(
            range_id=rng.id, filename="roe.pdf", minio_key=f"{rng.id}/d/roe.pdf", tenant_id=rng.tenant_id
        )
        db_session.add_all([snap, doc])
        db_session.commit()
        snap_id, doc_id, key = snap.id, doc.id, doc.minio_key

        with patch("app.routers.ranges.object_store.delete_object") as delete_object:
            assert client.delete(f"/ranges/{rng.id}").status_code == 204

        assert not _exists(db_session, RangeSnapshot, snap_id)
        assert not _exists(db_session, RangeDocument, doc_id)
        delete_object.assert_called_once_with(key, bucket="ranges")

    def test_past_scheduled_events_are_kept_without_the_range(self, client, db_session):
        from app.models import Range
        from app.scheduler.models import ScheduledEvent

        rng = _range(db_session)
        done = _event(db_session, rng, "completed")
        cancelled = _event(db_session, rng, "cancelled")
        db_session.commit()

        assert client.delete(f"/ranges/{rng.id}").status_code == 204

        assert not _exists(db_session, Range, rng.id)
        for ev in (done, cancelled):
            kept = db_session.get(ScheduledEvent, ev.id)
            assert kept is not None and kept.range_id is None
