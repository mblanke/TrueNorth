"""Tests for Adaptive Learning API endpoints."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
EXERCISE_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _create_assessment(db, user_id=USER_ID, exercise_id=None, raw_score=80, max_score=100):
    from app.models import CompetencyAutoAssessment

    a = CompetencyAutoAssessment(
        id=uuid.uuid4(),
        user_id=user_id,
        exercise_id=exercise_id or uuid.uuid4(),
        competency_mappings=json.dumps(
            [
                {"category": "network_defense", "delta": 5},
                {"category": "incident_response", "delta": -2},
            ]
        ),
        raw_score=raw_score,
        max_score=max_score,
        assessed_at=datetime.now(UTC),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _create_recommendation(db, user_id=USER_ID):
    from app.models import LearningRecommendation

    r = LearningRecommendation(
        id=uuid.uuid4(),
        user_id=user_id,
        summary="Focus on incident response skills",
        strengths=json.dumps(["network_defense"]),
        gaps=json.dumps(["incident_response"]),
        recommendations=json.dumps([{"area": "IR", "action": "Complete IR lab"}]),
        target_role_readiness=0.65,
        next_milestone="IR Fundamentals certification",
        model_used="llama3",
        generated_at=datetime.now(UTC),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


class TestAutoAssessments:
    def test_list_empty(self, client):
        resp = client.get(f"/adaptive/users/{USER_ID}/auto-assessments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_data(self, client, db_session):
        a = _create_assessment(db_session)
        resp = client.get(f"/adaptive/users/{USER_ID}/auto-assessments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == str(a.id)
        assert data[0]["raw_score"] == 80

    def test_get_single(self, client, db_session):
        a = _create_assessment(db_session)
        resp = client.get(f"/adaptive/users/{USER_ID}/auto-assessments/{a.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(a.id)

    def test_get_not_found(self, client):
        fake_id = uuid.uuid4()
        resp = client.get(f"/adaptive/users/{USER_ID}/auto-assessments/{fake_id}")
        assert resp.status_code == 404


class TestRecommendations:
    def test_list_empty(self, client):
        resp = client.get(f"/adaptive/users/{USER_ID}/recommendations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_data(self, client, db_session):
        _create_recommendation(db_session)
        resp = client.get(f"/adaptive/users/{USER_ID}/recommendations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["summary"] == "Focus on incident response skills"

    def test_get_single(self, client, db_session):
        r = _create_recommendation(db_session)
        resp = client.get(f"/adaptive/users/{USER_ID}/recommendations/{r.id}")
        assert resp.status_code == 200
        assert resp.json()["next_milestone"] == "IR Fundamentals certification"

    def test_get_not_found(self, client):
        fake_id = uuid.uuid4()
        resp = client.get(f"/adaptive/users/{USER_ID}/recommendations/{fake_id}")
        assert resp.status_code == 404

    @patch("worker.celery_app.app")
    def test_trigger_recommendation(self, mock_celery, client):
        mock_celery.send_task.return_value = None
        resp = client.post(f"/adaptive/users/{USER_ID}/recommendations")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"


class TestProgressSummary:
    def test_empty_progress(self, client):
        resp = client.get(f"/adaptive/users/{USER_ID}/progress")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_exercises"] == 0
        assert data["avg_score"] == 0.0
        assert data["strongest_areas"] == []
        assert data["weakest_areas"] == []

    def test_progress_with_assessments(self, client, db_session):
        _create_assessment(db_session, raw_score=90, max_score=100)
        _create_assessment(db_session, raw_score=70, max_score=100)
        resp = client.get(f"/adaptive/users/{USER_ID}/progress")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_exercises"] == 2
        assert data["avg_score"] == 80.0
        assert len(data["recent_assessments"]) == 2
