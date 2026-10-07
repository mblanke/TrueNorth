"""Tests for batch provisioning and stats endpoints."""

from datetime import timedelta

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
    """A range task the broker refused must not be lost, nor leave its range stuck: the
    worker builds or tears down only a range left in its in-progress state
    (worker/fencing.py). Each range action is a recorded operation (app/range_ops): with
    the broker down it is accepted, stays pending, visibly, and is re-sent once the broker
    is back. Found in the adversarial review of R1c: a batch answered 202 "dispatched"
    and stuck every range."""

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

    def _ops(self, client, rid):
        return client.get(f"/ranges/{rid}/operations").json()

    def test_a_batch_the_broker_refused_waits_visibly_and_is_sent_later(self, client, db_session, monkeypatch):
        from app import celery_client
        from app.range_ops.service import redispatch_pending

        rids = self._ranges(client)
        dispatch, sent = celery_client.dispatch, []
        monkeypatch.setattr(celery_client, "dispatch", lambda *a, **k: None)
        assert client.post("/ranges/batch-provision", json={"range_ids": rids}).status_code == 202
        assert [self._state(client, r) for r in rids] == ["provisioning", "provisioning"]
        first, *rest = [self._ops(client, r)[0] for r in sorted(rids)]
        assert first["status"] == "pending" and first["error"]["code"] == "broker_unavailable"
        # After one refusal the batch stops sending: the rest wait for the re-send loop.
        assert all(op["status"] == "pending" and op["dispatch_attempts"] == 0 for op in rest)
        monkeypatch.setattr(celery_client, "dispatch", lambda name, *a: sent.append((name, a)) or dispatch(name, *a))
        assert redispatch_pending(db_session, min_age=timedelta(0)) == 2  # the broker is back
        assert sorted(sent) == sorted(("provision_range", (r,)) for r in rids)
        assert all(self._ops(client, r)[0]["status"] == "dispatched" for r in rids)

    @pytest.mark.parametrize(
        ("action", "state", "claimed"), [("provision", "created", "provisioning"), ("destroy", "ready", "destroying")]
    )
    def test_a_single_action_the_broker_refused_is_kept(self, client, db_session, monkeypatch, action, state, claimed):
        import uuid

        from app import celery_client
        from app.models import Range, RangeState

        (rid,) = self._ranges(client, 1)
        db_session.get(Range, uuid.UUID(rid)).state = RangeState(state)
        db_session.commit()
        monkeypatch.setattr(celery_client, "dispatch", lambda *a, **k: None)
        resp = client.post(f"/ranges/{rid}/{action}")
        assert resp.status_code == 202 and resp.json()["state"] == claimed
        assert self._ops(client, rid)[0]["status"] == "pending"

    def test_a_sent_batch_is_one_operation_per_range(self, client, no_real_broker):
        rids = self._ranges(client)
        resp = client.post("/ranges/batch-provision", json={"range_ids": rids})
        assert resp.status_code == 202 and len(resp.json()["task_id"].split(",")) == 2
        assert [self._state(client, r) for r in rids] == ["provisioning", "provisioning"]
        assert sorted(no_real_broker.sent) == sorted(("worker.tasks.provision_range", [r]) for r in rids)

    def test_one_range_that_cannot_be_provisioned_refuses_the_whole_batch(self, client, db_session, no_real_broker):
        import uuid

        from app.models import Range, RangeState

        rids = self._ranges(client)
        db_session.get(Range, uuid.UUID(rids[1])).state = RangeState.ready
        db_session.commit()
        assert client.post("/ranges/batch-provision", json={"range_ids": rids}).status_code == 409
        assert self._state(client, rids[0]) == "created" and self._ops(client, rids[0]) == []
        assert no_real_broker.sent == []
