"""End-to-end API integration tests for TrueNorth Range."""

import uuid

# ── Helpers ────────────────────────────────────────────────────────────


def _make_template(client):
    """Create and return a template via the API."""
    resp = client.post(
        "/templates",
        json={
            "name": f"tmpl-{uuid.uuid4().hex[:8]}",
            "version": "1.0",
            "yaml": "id: test\nnodes:\n  - name: dc1",
            "is_public": True,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _make_scenario(client):
    """Create and return a scenario via the API."""
    resp = client.post(
        "/scenarios",
        json={
            "name": f"sc-{uuid.uuid4().hex[:8]}",
            "version": "1.0",
            "yaml": "name: test\nphases:\n  - init",
            "is_public": True,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _make_range(client, template_id):
    """Create and return a range via the API."""
    resp = client.post(
        "/ranges",
        json={
            "name": f"range-{uuid.uuid4().hex[:8]}",
            "template_id": str(template_id),
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _make_exercise(client, range_id, scenario_id, max_score=100):
    """Create and return an exercise via the API."""
    resp = client.post(
        "/exercises",
        json={
            "name": f"ex-{uuid.uuid4().hex[:8]}",
            "range_id": str(range_id),
            "scenario_id": str(scenario_id),
            "max_score": max_score,
        },
    )
    assert resp.status_code == 201
    return resp.json()


# ── Range Lifecycle ────────────────────────────────────────────────────


class TestRangeLifecycle:
    def test_full_lifecycle(self, client, db_session):
        """create tenant -> template -> scenario -> range -> provision -> check state."""
        slug = f"lc-{uuid.uuid4().hex[:6]}"
        client.post("/tenants", json={"name": f"LC Corp {slug}", "slug": slug})
        tmpl = _make_template(client)
        _make_scenario(client)
        rng = _make_range(client, tmpl["id"])
        assert rng["state"] == "created"
        # Provision
        resp = client.post(f"/ranges/{rng['id']}/provision")
        assert resp.status_code == 200
        assert resp.json()["state"] == "provisioning"
        # Verify persisted state
        got = client.get(f"/ranges/{rng['id']}")
        assert got.status_code == 200
        assert got.json()["state"] == "provisioning"

    def test_range_with_exercise(self, client):
        """create range -> exercise -> start -> complete -> verify scoring."""
        tmpl = _make_template(client)
        sc = _make_scenario(client)
        rng = _make_range(client, tmpl["id"])
        ex = _make_exercise(client, rng["id"], sc["id"], max_score=100)
        assert ex["state"] == "pending"
        r = client.post(f"/exercises/{ex['id']}/start")
        assert r.status_code == 200
        assert r.json()["state"] == "running"
        r = client.post(f"/exercises/{ex['id']}/complete")
        assert r.status_code == 200
        data = r.json()
        assert data["state"] == "completed"
        assert "total_score" in data

    def test_batch_provision_workflow(self, client):
        """Create ranges -> batch provision -> verify stats."""
        tmpl = _make_template(client)
        range_ids = [_make_range(client, tmpl["id"])["id"] for _ in range(3)]
        resp = client.post("/ranges/batch-provision", json={"range_ids": range_ids})
        # 202 if Celery present, 500 or 202 with mock-batch otherwise
        assert resp.status_code in (202, 500)
        stats = client.get("/ranges/stats")
        assert stats.status_code == 200
        assert stats.json()["total_ranges"] >= 3


# ── Multi-Tenant Isolation ─────────────────────────────────────────────


class TestMultiTenantIsolation:
    def test_tenant_ranges_isolated(self, client, db_session):
        """Ranges owned by another tenant must not appear in listings."""
        from app.models import Range, RangeState

        tmpl = _make_template(client)
        my_range = _make_range(client, tmpl["id"])
        other = Range(
            name="alien-range",
            template_id=uuid.UUID(tmpl["id"]),
            tenant_id=uuid.uuid4(),
            state=RangeState.created,
        )
        db_session.add(other)
        db_session.commit()
        resp = client.get("/ranges")
        ids = [r["id"] for r in resp.json()]
        assert my_range["id"] in ids
        assert str(other.id) not in ids

    def test_cross_tenant_access_denied(self, client, db_session):
        """A foreign range must not be readable by id, not merely hidden from lists.

        This test previously asserted `status_code == 200` — it codified the IDOR
        rather than catching it. Because it was named "access_denied" and passed, the
        leak read as covered. `list_ranges` filtered on tenant_id; every by-id handler
        did not, so any tenant could read, modify, provision or destroy another
        tenant's range given its UUID.
        """
        from app.models import Range, RangeState

        tmpl = _make_template(client)
        other = Range(
            name="hidden-range",
            template_id=uuid.UUID(tmpl["id"]),
            tenant_id=uuid.uuid4(),
            state=RangeState.created,
        )
        db_session.add(other)
        db_session.commit()
        resp = client.get(f"/ranges/{other.id}")
        assert resp.status_code == 404, (
            f"LEAK: another tenant's range was readable by id (status "
            f"{resp.status_code})"
        )
        ids = [r["id"] for r in client.get("/ranges").json()]
        assert str(other.id) not in ids


# ── Exercise Workflow ──────────────────────────────────────────────────


class TestExerciseWorkflow:
    def test_exercise_state_machine(self, client):
        """pending -> running -> paused -> (start blocked) -> completed."""
        tmpl = _make_template(client)
        sc = _make_scenario(client)
        rng = _make_range(client, tmpl["id"])
        ex = _make_exercise(client, rng["id"], sc["id"])
        assert ex["state"] == "pending"
        r = client.post(f"/exercises/{ex['id']}/start")
        assert r.status_code == 200 and r.json()["state"] == "running"
        r = client.post(f"/exercises/{ex['id']}/pause")
        assert r.status_code == 200 and r.json()["state"] == "paused"
        # Re-start from paused is rejected (start requires pending)
        r = client.post(f"/exercises/{ex['id']}/start")
        assert r.status_code == 409
        # Complete from paused is allowed
        r = client.post(f"/exercises/{ex['id']}/complete")
        assert r.status_code == 200 and r.json()["state"] == "completed"

    def test_exercise_objectives(self, client, db_session):
        """Objectives inserted in DB are retrievable via API."""
        from app.models import Objective, ObjectiveType

        tmpl = _make_template(client)
        sc = _make_scenario(client)
        rng = _make_range(client, tmpl["id"])
        ex = _make_exercise(client, rng["id"], sc["id"])
        for i in range(3):
            db_session.add(
                Objective(
                    exercise_id=uuid.UUID(ex["id"]),
                    ref_id=f"obj-{i}",
                    objective_type=ObjectiveType.detection,
                    description=f"Detect threat #{i}",
                    validator="manual",
                    points=50,
                )
            )
        db_session.commit()
        resp = client.get(f"/exercises/{ex['id']}/objectives")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_aar_generation(self, client, db_session):
        """Complete exercise -> generate AAR -> verify structure."""
        from app.models import Objective, ObjectiveType

        tmpl = _make_template(client)
        sc = _make_scenario(client)
        rng = _make_range(client, tmpl["id"])
        ex = _make_exercise(client, rng["id"], sc["id"], max_score=100)
        db_session.add(
            Objective(
                exercise_id=uuid.UUID(ex["id"]),
                ref_id="obj-a",
                objective_type=ObjectiveType.detection,
                description="Detect C2 callback",
                validator="manual",
                points=60,
                achieved=True,
            )
        )
        db_session.commit()
        client.post(f"/exercises/{ex['id']}/start")
        client.post(f"/exercises/{ex['id']}/complete")
        aar = client.post(f"/exercises/{ex['id']}/aar/generate")
        assert aar.status_code == 201
        data = aar.json()
        assert data["exercise_id"] == ex["id"]
        assert "report_json" in data
        assert "report_html" in data


# ── Error Handling ─────────────────────────────────────────────────────


class TestErrorHandling:
    def test_create_range_invalid_template(self, client):
        """Creating a range with a non-existent template returns 404."""
        resp = client.post(
            "/ranges",
            json={
                "name": "Bad",
                "template_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 404

    def test_provision_non_existent_range(self, client):
        """Provisioning a missing range returns 404."""
        resp = client.post(f"/ranges/{uuid.uuid4()}/provision")
        assert resp.status_code == 404

    def test_invalid_uuid_format(self, client):
        """Non-UUID path parameter returns 422."""
        resp = client.get("/ranges/not-a-uuid")
        assert resp.status_code == 422

    def test_duplicate_batch_provision(self, client):
        """Duplicate range_ids in batch payload are handled."""
        tmpl = _make_template(client)
        rng = _make_range(client, tmpl["id"])
        resp = client.post(
            "/ranges/batch-provision",
            json={
                "range_ids": [rng["id"], rng["id"]],
            },
        )
        assert resp.status_code in (202, 400, 409, 500)

    def test_large_payload_rejection(self, client):
        """Oversized name triggers Pydantic validation (max_length=255)."""
        resp = client.post(
            "/ranges",
            json={
                "name": "x" * 100_000,
                "template_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code in (413, 422)


# ── Audit Log ──────────────────────────────────────────────────────────


class TestAuditLog:
    def test_audit_log_records_operations(self, client):
        """CRUD operations generate audit log entries."""
        tmpl = _make_template(client)
        rng = _make_range(client, tmpl["id"])
        client.post(f"/ranges/{rng['id']}/provision")
        resp = client.get("/audit-log?limit=20")
        assert resp.status_code == 200
        actions = [entry["action"] for entry in resp.json()]
        assert "create" in actions

    def test_audit_log_pagination(self, client):
        """Audit log supports limit/offset pagination."""
        for _ in range(5):
            _make_template(client)
        p1 = client.get("/audit-log?limit=3&offset=0").json()
        p2 = client.get("/audit-log?limit=3&offset=3").json()
        assert len(p1) == 3
        ids1 = {e["id"] for e in p1}
        ids2 = {e["id"] for e in p2}
        assert ids1.isdisjoint(ids2)
