"""Tests for batch provisioning and stats endpoints."""


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
