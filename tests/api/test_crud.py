"""Tests for core API endpoints."""

import uuid


class TestHealth:
    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_has_db_flag(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "db" in data
        assert isinstance(data["db"], bool)

    def test_health_has_redis_flag(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "redis" in data
        assert isinstance(data["redis"], bool)


class TestTenants:
    def test_create_tenant(self, client):
        resp = client.post("/tenants", json={"name": "Test Corp", "slug": "test-corp"})
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["name"] == "Test Corp"
        assert data["slug"] == "test-corp"
        assert "id" in data

    def test_list_tenants(self, client):
        client.post("/tenants", json={"name": "A", "slug": "a"})
        resp = client.get("/tenants")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_duplicate_slug_fails(self, client):
        client.post("/tenants", json={"name": "One", "slug": "dup"})
        resp = client.post("/tenants", json={"name": "Two", "slug": "dup"})
        # Should fail with 409 or 400
        assert resp.status_code in (400, 409, 422, 500)


class TestTemplates:
    def test_create_template(self, client):
        payload = {
            "name": "Test Template",
            "version": "1.0",
            "yaml": "id: test\nnodes: []",
            "is_public": True,
        }
        resp = client.post("/templates", json=payload)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["name"] == "Test Template"
        assert data["version"] == "1.0"
        assert "id" in data
        assert "created_at" in data

    def test_list_templates(self, client):
        resp = client.get("/templates")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_template(self, client):
        create_resp = client.post(
            "/templates",
            json={
                "name": "Fetched",
                "version": "2.0",
                "yaml": "id: fetch\nnodes: []",
                "is_public": False,
            },
        )
        tid = create_resp.json()["id"]
        resp = client.get(f"/templates/{tid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Fetched"

    def test_get_template_not_found(self, client):
        resp = client.get(f"/templates/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_update_template(self, client):
        create_resp = client.post(
            "/templates",
            json={
                "name": "Original",
                "version": "1.0",
                "yaml": "id: orig\nnodes: []",
                "is_public": False,
            },
        )
        tid = create_resp.json()["id"]
        resp = client.put(f"/templates/{tid}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_delete_template(self, client):
        create_resp = client.post(
            "/templates",
            json={
                "name": "ToDelete",
                "version": "1.0",
                "yaml": "id: del\nnodes: []",
                "is_public": False,
            },
        )
        tid = create_resp.json()["id"]
        resp = client.delete(f"/templates/{tid}")
        assert resp.status_code in (200, 204)
        # Verify deletion
        resp2 = client.get(f"/templates/{tid}")
        assert resp2.status_code == 404


class TestScenarios:
    def test_create_scenario(self, client):
        payload = {
            "name": "Test Scenario",
            "version": "1.0",
            "yaml": "id: test\ntimeline: []",
            "is_public": True,
        }
        resp = client.post("/scenarios", json=payload)
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["name"] == "Test Scenario"
        assert "id" in data

    def test_list_scenarios(self, client):
        resp = client.get("/scenarios")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_scenario_not_found(self, client):
        resp = client.get(f"/scenarios/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestRanges:
    def _create_template(self, client):
        resp = client.post(
            "/templates",
            json={
                "name": "Range Template",
                "version": "1.0",
                "yaml": "id: rt\nnodes: []",
                "is_public": True,
            },
        )
        return resp.json()["id"]

    def test_create_range(self, client):
        tid = self._create_template(client)
        resp = client.post("/ranges", json={"name": "Test Range", "template_id": tid})
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["name"] == "Test Range"
        assert data["state"] == "created"

    def test_list_ranges(self, client):
        resp = client.get("/ranges")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_range_not_found(self, client):
        resp = client.get(f"/ranges/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_provision_range(self, client):
        tid = self._create_template(client)
        create_resp = client.post("/ranges", json={"name": "Prov Range", "template_id": tid})
        rid = create_resp.json()["id"]
        resp = client.post(f"/ranges/{rid}/provision")
        assert resp.status_code in (200, 202)


class TestTeams:
    def test_create_team(self, client):
        resp = client.post("/teams", json={"name": "Blue Team Alpha"})
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["name"] == "Blue Team Alpha"
        assert "id" in data

    def test_list_teams(self, client):
        resp = client.get("/teams")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestExercises:
    def _setup_range(self, client):
        t = client.post(
            "/templates",
            json={
                "name": "Ex Template",
                "version": "1.0",
                "yaml": "id: ex\nnodes: []",
                "is_public": True,
            },
        ).json()
        r = client.post("/ranges", json={"name": "Ex Range", "template_id": t["id"]}).json()
        s = client.post(
            "/scenarios",
            json={
                "name": "Ex Scenario",
                "version": "1.0",
                "yaml": "id: exs\ntimeline: []",
                "is_public": True,
            },
        ).json()
        return r["id"], s["id"]

    def test_create_exercise(self, client):
        rid, sid = self._setup_range(client)
        resp = client.post(
            "/exercises",
            json={
                "name": "Test Exercise",
                "range_id": rid,
                "scenario_id": sid,
                "max_score": 100,
            },
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["name"] == "Test Exercise"
        assert data["state"] == "pending"
        assert data["max_score"] == 100

    def test_list_exercises(self, client):
        resp = client.get("/exercises")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestAuditLog:
    def test_list_audit_log(self, client):
        resp = client.get("/audit-log")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
