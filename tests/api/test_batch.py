"""Tests for batch provisioning and stats endpoints."""

import pytest


class TestBatchProvision:
    def _setup_range(self, client):
        """Create a tenant, template, and range. Return range_id."""
        client.post("/tenants", json={"name": "Batch Corp", "slug": "batch-corp"})
        tmpl = client.post(
            "/templates", json={"name": "batch-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
        ).json()
        r = client.post("/ranges", json={"name": "batch-range-1", "template_id": tmpl["id"]}).json()
        return r["id"]

    def test_batch_provision_single(self, client):
        rid = self._setup_range(client)
        resp = client.post("/ranges/batch-provision", json={"range_ids": [rid]})
        assert resp.status_code == 202
        data = resp.json()
        assert data["dispatched"] == 1
        assert data["task_id"] is not None

    def test_batch_provision_empty_list(self, client):
        resp = client.post("/ranges/batch-provision", json={"range_ids": []})
        # Empty list — should return validation error or 0 dispatched
        assert resp.status_code in (202, 400, 422)

    def test_batch_provision_multiple(self, client):
        # Create tenant and template once
        client.post("/tenants", json={"name": "Multi Corp", "slug": "multi-corp"})
        tmpl = client.post(
            "/templates", json={"name": "multi-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
        ).json()
        rids = []
        for i in range(3):
            r = client.post("/ranges", json={"name": f"multi-range-{i}", "template_id": tmpl["id"]}).json()
            rids.append(r["id"])
        resp = client.post("/ranges/batch-provision", json={"range_ids": rids})
        assert resp.status_code == 202
        data = resp.json()
        assert data["dispatched"] == 3


class TestRangeStats:
    def test_stats_empty(self, client):
        resp = client.get("/ranges/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_state" in data
        assert "total_ranges" in data

    def test_stats_after_creation(self, client):
        # Create some ranges
        client.post("/tenants", json={"name": "Stats Corp", "slug": "stats-corp"})
        tmpl = client.post(
            "/templates", json={"name": "stats-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
        ).json()
        for i in range(2):
            client.post("/ranges", json={"name": f"stats-range-{i}", "template_id": tmpl["id"]})
        resp = client.get("/ranges/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_ranges"] >= 2
        assert data["by_state"].get("created", 0) >= 2


class TestBrokerDown:
    """A range task the broker refused must not leave its range in provisioning or
    destroying: the worker builds or tears down only a range left in that state
    (worker/fencing.py), so nothing could ever move it again. Found in the adversarial
    review of R1c: a batch answered 202 "dispatched" and stuck every range."""

    def _ranges(self, client, n=2):
        client.post("/tenants", json={"name": "Down Corp", "slug": "down-corp"})
        tmpl = client.post(
            "/templates", json={"name": "down-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
        ).json()
        return [
            client.post("/ranges", json={"name": f"down-{i}", "template_id": tmpl["id"]}).json()["id"] for i in range(n)
        ]

    def _state(self, client, rid):
        return client.get(f"/ranges/{rid}").json()["state"]

    def test_a_refused_batch_leaves_every_range_as_it_was(self, client, monkeypatch):
        from app import celery_client

        rids = self._ranges(client)
        dispatch = celery_client.dispatch
        monkeypatch.setattr(celery_client, "dispatch", lambda *a, **k: None)
        resp = client.post("/ranges/batch-provision", json={"range_ids": rids})
        assert resp.status_code == 503
        assert [self._state(client, r) for r in rids] == ["created", "created"]
        monkeypatch.setattr(celery_client, "dispatch", dispatch)  # the broker is back
        assert client.post("/ranges/batch-provision", json={"range_ids": rids}).status_code == 202

    @pytest.mark.parametrize(("action", "state"), [("provision", "created"), ("destroy", "ready")])
    def test_a_refused_single_action_leaves_the_range_as_it_was(self, client, db_session, monkeypatch, action, state):
        import uuid

        from app import celery_client
        from app.models import Range, RangeState

        (rid,) = self._ranges(client, 1)
        db_session.get(Range, uuid.UUID(rid)).state = RangeState(state)
        db_session.commit()
        monkeypatch.setattr(celery_client, "dispatch", lambda *a, **k: None)
        assert client.post(f"/ranges/{rid}/{action}").status_code == 503
        assert self._state(client, rid) == state

    def test_a_sent_batch_records_provisioning_before_the_task_goes(self, client, no_real_broker):
        rids = self._ranges(client)
        assert client.post("/ranges/batch-provision", json={"range_ids": rids}).status_code == 202
        assert [self._state(client, r) for r in rids] == ["provisioning", "provisioning"]
        assert [name for name, _ in no_real_broker.sent] == ["worker.tasks.batch_provision"]
