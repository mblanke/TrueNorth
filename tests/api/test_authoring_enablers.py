"""Tests for the authoring enabler endpoints: schema validation + inject catalogue.

These are the contract the Scenario Studio builds against: the engine's own
JSON Schemas served over HTTP, and the injector registry introspected instead
of hard-coded into two different UIs.
"""

import textwrap
from unittest.mock import AsyncMock, patch

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


VALID_SCENARIO = textwrap.dedent("""
    name: Quick detection drill
    version: "1.0"
    range_template: small-enterprise
    timeline:
      - t: "00:00"
        action: inject.dns_spike
        params:
          domains: [bad.example]
      - t: "05:30"
        action: inject.email_phish
    objectives:
      - id: obj-1
        type: detection
        validator: validate.opensearch_query
        points: 10
""")


class TestScenarioValidate:
    def test_engine_dialect_document_passes(self, client):
        res = client.post("/scenarios/validate", json={"yaml": VALID_SCENARIO}).json()
        assert res["valid"] is True
        assert res["errors"] == []
        assert res["normalized"]["range_template"] == "small-enterprise"

    def test_missing_required_keys_fail_with_paths(self, client):
        res = client.post(
            "/scenarios/validate",
            json={"yaml": "name: x\nversion: '1'\ntimeline: []\nobjectives: []\n"},
        ).json()
        assert res["valid"] is False
        assert any("range_template" in e["message"] for e in res["errors"])
        # Schema-invalid but parseable: the document still comes back normalized,
        # so an editor can load it and show the errors at once.
        assert res["normalized"]["name"] == "x"

    def test_bad_timeline_entry_reports_its_path(self, client):
        doc = VALID_SCENARIO.replace('t: "00:00"', 't: "0:0"')
        res = client.post("/scenarios/validate", json={"yaml": doc}).json()
        assert res["valid"] is False
        assert any(e["path"].startswith("timeline.0") for e in res["errors"])

    def test_malformed_yaml_is_a_result_not_a_500(self, client):
        resp = client.post("/scenarios/validate", json={"yaml": "a: [unclosed"})
        assert resp.status_code == 200
        res = resp.json()
        assert res["valid"] is False
        assert res["normalized"] is None

    def test_non_mapping_document_is_rejected(self, client):
        res = client.post("/scenarios/validate", json={"yaml": "- just\n- a list\n"}).json()
        assert res["valid"] is False
        assert res["normalized"] is None


class TestTemplateValidate:
    def test_minimal_template_passes(self, client):
        res = client.post("/templates/validate", json={"yaml": "name: tiny-range\n"}).json()
        assert res["valid"] is True

    def test_bad_asset_type_fails(self, client):
        doc = "name: t\nassets:\n  - role: dc\n    type: mainframe\n    count: 1\n"
        res = client.post("/templates/validate", json={"yaml": doc}).json()
        assert res["valid"] is False
        assert any("mainframe" in e["message"] for e in res["errors"])


class TestInjectorCatalogue:
    def test_catalogue_lists_the_registry(self, client):
        items = client.get("/injectors").json()
        names = {i["name"] for i in items}
        # The registry currently holds nine injectors; assert the stable core
        # rather than an exact count so adding one does not break this test.
        assert {"ad_attack", "c2_beacon", "dns_spike", "email_phish", "simulated_execution"} <= names

    def test_param_and_mitre_metadata_survive(self, client):
        items = {i["name"]: i for i in client.get("/injectors").json()}
        ad = items["ad_attack"]
        assert set(ad["required_params"]) == {"attack_type", "target_dc"}
        assert "T1558.003" in ad["mitre_techniques"]
        # An injector with no declared params advertises an empty list.
        assert items["email_phish"]["required_params"] == []


def _seed_range(db, name="R", state="ready"):
    from app.models import Range, Template

    tpl = Template(name=f"{name}-tpl", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db.add(tpl)
    db.flush()
    rng = Range(name=name, template_id=tpl.id, tenant_id=DEV_TENANT, state=state)
    db.add(rng)
    db.commit()
    return rng


class TestForgeRangeAndHistory:
    @patch("app.routers.exercise_forge._call_forge_ai", new_callable=AsyncMock)
    def test_generate_attaches_to_chosen_range(self, mock_ai, client, db_session):
        chosen = _seed_range(db_session, "chosen")
        _seed_range(db_session, "other")  # exists but must not be picked
        mock_ai.return_value = ("name: X\ntimeline: []\nobjectives: []\n", "m")
        resp = client.post(
            "/exercise-forge/generate",
            json={
                "indicators": [{"indicator_type": "domain", "value": "e.com", "severity": "high"}],
                "range_id": str(chosen.id),
            },
        )
        assert resp.status_code == 200
        from app.models import Exercise

        ex = db_session.query(Exercise).filter_by(id=resp.json()["exercise_id"]).one()
        assert str(ex.range_id) == str(chosen.id)

    def test_generate_bad_range_id_is_404(self, client, db_session):
        _seed_range(db_session, "present")
        import uuid

        with patch("app.routers.exercise_forge._call_forge_ai", new_callable=AsyncMock) as m:
            m.return_value = ("name: X\ntimeline: []\nobjectives: []\n", "m")
            resp = client.post(
                "/exercise-forge/generate",
                json={
                    "indicators": [{"indicator_type": "domain", "value": "e.com", "severity": "high"}],
                    "range_id": str(uuid.uuid4()),
                },
            )
        assert resp.status_code == 404

    def test_history_lists_provenance_newest_first(self, client, db_session):
        from app.models import Exercise, ForgedExercise, Scenario

        rng = _seed_range(db_session, "histrange")
        for i in range(2):
            sc = Scenario(name=f"s{i}", yaml="name: s\n", tenant_id=DEV_TENANT)
            db_session.add(sc)
            db_session.flush()
            ex = Exercise(name=f"forged-{i}", range_id=rng.id, scenario_id=sc.id, tenant_id=DEV_TENANT)
            db_session.add(ex)
            db_session.flush()
            db_session.add(ForgedExercise(
                exercise_id=ex.id, scenario_id=sc.id, scenario_yaml="name: s\n",
                difficulty="intermediate", model_used="m", tenant_id=DEV_TENANT,
            ))
        db_session.commit()
        res = client.get("/exercise-forge/history").json()
        assert res["total"] == 2
        assert {i["exercise_name"] for i in res["items"]} == {"forged-0", "forged-1"}
        assert res["items"][0]["source"] in {"feed", "manual/curriculum"}


class TestTemplateDiagramPreview:
    def test_preview_renders_declared_assets(self, client, db_session):
        from app.models import Template

        tpl = Template(
            name="ad-lab", version="1.0", tenant_id=DEV_TENANT,
            yaml="name: ad-lab\nassets:\n  - role: dc\n    type: vm\n    count: 1\n  - role: workstation\n    type: vm\n    count: 3\n",
        )
        db_session.add(tpl)
        db_session.commit()
        res = client.post(f"/templates/{tpl.id}/diagram-preview").json()
        cells = res["diagram_json"]["cells"]
        node_types = [c.get("nodeType") for c in cells]
        assert "dc" in node_types
        assert node_types.count("workstation") == 3
        assert "firewall" in node_types  # gateway always laid in

    def test_unparseable_template_is_422(self, client, db_session):
        from app.models import Template

        tpl = Template(name="broken", version="1.0", tenant_id=DEV_TENANT, yaml="a: [unclosed")
        db_session.add(tpl)
        db_session.commit()
        assert client.post(f"/templates/{tpl.id}/diagram-preview").status_code == 422


class TestAarAiEnhance:
    def test_calls_the_real_orchestrator_route_and_body(self, client, db_session, monkeypatch):
        import httpx
        from app.models import AfterActionReport, Exercise, Range, Template

        tpl = Template(name="t", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
        db_session.add(tpl)
        db_session.flush()
        rng = Range(name="r", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
        db_session.add(rng)
        db_session.flush()
        ex = Exercise(name="ex", range_id=rng.id, tenant_id=DEV_TENANT)
        db_session.add(ex)
        db_session.flush()
        db_session.add(AfterActionReport(exercise_id=ex.id, report_json='{"score": 88}', report_html="<body></body>"))
        db_session.commit()

        captured = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"output": "Strong detection posture.", "model_used": "test-model"}

            def raise_for_status(self):
                pass

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        resp = client.post(f"/exercises/{ex.id}/aar/ai-enhance")
        assert resp.status_code == 200
        assert captured["url"].endswith("/ai/aar-analysis")
        assert "report_data" in captured["json"]
        assert "task_type" not in captured["json"]  # the old broken field is gone


class TestMeslEventPatch:
    def _collective_with_event(self, client, db_session):
        from app.models import Exercise, MeslEvent

        rng = _seed_range(db_session, "meslrange")
        ex = Exercise(name="Collective", kind="collective", range_id=rng.id, tenant_id=DEV_TENANT)
        db_session.add(ex)
        db_session.flush()
        ev = MeslEvent(
            exercise_id=ex.id, serial=1, phase="I", scenario_time="D1 0800", title="Initial access",
            delivery_method="cyber", status="planned",
        )
        db_session.add(ev)
        db_session.commit()
        return str(ex.id), str(ev.id)

    def test_partial_update_persists(self, client, db_session):
        ex_id, ev_id = self._collective_with_event(client, db_session)
        resp = client.patch(
            f"/collective-exercises/{ex_id}/mesl/{ev_id}",
            json={"status": "delivered", "to_participant": "Blue-1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "delivered"
        assert body["to_participant"] == "Blue-1"
        assert body["title"] == "Initial access"  # untouched

    def test_bad_vocabulary_rejected(self, client, db_session):
        ex_id, ev_id = self._collective_with_event(client, db_session)
        assert client.patch(f"/collective-exercises/{ex_id}/mesl/{ev_id}", json={"status": "bogus"}).status_code == 422
        assert client.patch(
            f"/collective-exercises/{ex_id}/mesl/{ev_id}", json={"delivery_method": "carrier_pigeon"}
        ).status_code == 422

    def test_event_from_another_exercise_is_404(self, client, db_session):
        ex_id, ev_id = self._collective_with_event(client, db_session)
        other_id, _ = self._collective_with_event(client, db_session)
        assert client.patch(f"/collective-exercises/{other_id}/mesl/{ev_id}", json={"status": "staged"}).status_code == 404
