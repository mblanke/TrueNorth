"""Tests for Ops Center API endpoints."""

import json
import uuid
from datetime import UTC, datetime

from app.models import AnalystAnnotation, Exercise

EXERCISE_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _create_exercise(db, exercise_id=EXERCISE_ID):
    from app.models import Range, Scenario, Template

    # Create template
    t = db.query(Template).first()
    if not t:
        t = Template(id=uuid.uuid4(), name="Test Template", yaml="name: test")
        db.add(t)
        db.flush()

    # Create scenario
    s = db.query(Scenario).first()
    if not s:
        s = Scenario(id=uuid.uuid4(), name="Test Scenario", yaml="name: test")
        db.add(s)
        db.flush()

    # Create range
    r = db.query(Range).first()
    if not r:
        r = Range(id=uuid.uuid4(), name="Test Range", template_id=t.id)
        db.add(r)
        db.flush()

    ex = Exercise(
        id=exercise_id,
        range_id=r.id,
        scenario_id=s.id,
        name="Ops Test Exercise",
        started_at=datetime.now(UTC),
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


def _create_annotation(db, exercise_id=EXERCISE_ID, content="Test finding"):
    a = AnalystAnnotation(
        id=uuid.uuid4(),
        exercise_id=exercise_id,
        user_id=USER_ID,
        user_display_name="Test User",
        content=content,
        annotation_type="finding",
        severity="medium",
        tags=json.dumps(["test"]),
        created_at=datetime.now(UTC),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


class TestAnnotations:
    def test_list_empty(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.get(f"/ops/exercises/{ex.id}/annotations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_annotation(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.post(
            f"/ops/exercises/{ex.id}/annotations",
            json={
                "content": "Suspicious DNS query to known C2 domain",
                "annotation_type": "ioc",
                "severity": "high",
                "tags": ["dns", "c2"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["content"] == "Suspicious DNS query to known C2 domain"
        assert data["annotation_type"] == "ioc"
        assert data["severity"] == "high"
        assert data["tags"] == ["dns", "c2"]

    def test_list_with_data(self, client, db_session):
        ex = _create_exercise(db_session)
        _create_annotation(db_session, ex.id)
        resp = client.get(f"/ops/exercises/{ex.id}/annotations")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_delete_annotation(self, client, db_session):
        ex = _create_exercise(db_session)
        a = _create_annotation(db_session, ex.id)
        resp = client.delete(f"/ops/exercises/{ex.id}/annotations/{a.id}")
        assert resp.status_code == 204

    def test_delete_not_found(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.delete(f"/ops/exercises/{ex.id}/annotations/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_create_annotation_exercise_not_found(self, client):
        resp = client.post(
            f"/ops/exercises/{uuid.uuid4()}/annotations",
            json={
                "content": "test",
            },
        )
        assert resp.status_code == 404


class TestSharedCommands:
    def test_list_empty(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.get(f"/ops/exercises/{ex.id}/commands")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_share_command(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.post(
            f"/ops/exercises/{ex.id}/commands",
            json={
                "command": "Get-WinEvent -LogName Security -MaxEvents 100",
                "host_tag": "DC01",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["command"] == "Get-WinEvent -LogName Security -MaxEvents 100"
        assert data["host_tag"] == "DC01"


class TestInstructorInject:
    def test_inject(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.post(
            f"/ops/exercises/{ex.id}/inject",
            json={
                "inject_type": "dns_spike",
                "description": "Sudden spike in DNS queries",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "injected"
        assert data["inject"]["inject_type"] == "dns_spike"

    def test_inject_exercise_not_found(self, client):
        resp = client.post(
            f"/ops/exercises/{uuid.uuid4()}/inject",
            json={
                "inject_type": "custom",
            },
        )
        assert resp.status_code == 404


class TestOpsStats:
    def test_stats(self, client, db_session):
        ex = _create_exercise(db_session)
        resp = client.get(f"/ops/exercises/{ex.id}/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exercise_id"] == str(ex.id)
        assert data["annotations_count"] == 0
        assert data["shared_commands_count"] == 0
        assert data["elapsed_seconds"] >= 0

    def test_stats_not_found(self, client):
        resp = client.get(f"/ops/exercises/{uuid.uuid4()}/stats")
        assert resp.status_code == 404
