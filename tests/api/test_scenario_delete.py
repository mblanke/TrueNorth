"""DELETE /scenarios/{id} refuses with 409 while an exercise still references it.

SQLite in the unit-test engine does not enforce foreign keys, so the referencing
exercise is simulated by making the commit raise the IntegrityError Postgres
raises (exercises_scenario_id_fkey). The live-stack case is covered by
tests/integration/test_exercise_lifecycle.py teardown.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError

PAYLOAD = {"name": "Delete me", "version": "1.0", "yaml": "id: del\ntimeline: []", "is_public": True}


def test_delete_unreferenced_scenario_is_204(client):
    sid = client.post("/scenarios", json=PAYLOAD).json()["id"]
    assert client.delete(f"/scenarios/{sid}").status_code == 204
    assert client.get(f"/scenarios/{sid}").status_code == 404


def test_delete_referenced_scenario_is_409_not_500(client, db_session, monkeypatch):
    sid = client.post("/scenarios", json=PAYLOAD).json()["id"]

    def _fk_violation():
        raise IntegrityError("DELETE FROM scenarios", {}, Exception("exercises_scenario_id_fkey"))

    monkeypatch.setattr(db_session, "commit", _fk_violation)
    resp = client.delete(f"/scenarios/{sid}")
    assert resp.status_code == 409
    assert "exercises" in resp.json()["detail"]


def test_delete_unknown_scenario_is_404(client):
    assert client.delete(f"/scenarios/{uuid.uuid4()}").status_code == 404
