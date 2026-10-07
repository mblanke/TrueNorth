"""Scenario runs on the API: execution endpoints, recorded injects, the instructor inject's
dispatch, and DELETE /scenarios/{id} while an exercise uses it."""

from __future__ import annotations

import uuid

import pytest
from app.models import Exercise, Range, RangeState, Scenario, Template
from app.scenario_runs import InjectRecord, ScenarioExecution

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")  # AUTH_DISABLED's tenant
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

SCENARIO_YAML = """\
name: drill
timeline:
  - t: "00:00"
    action: inject.simulated_execution
    params: {technique: T1059}
  - t: "00:30"
    action: dns_spike
    params: {domains: [evil.test], count: 3}
objectives:
  - id: obj-1
    type: detection
    validator: validate.opensearch_query
    points: 10
    description: Detect the execution
"""


def _scenario(db, yaml_text=SCENARIO_YAML, tenant=TENANT_ID) -> Scenario:
    sc = Scenario(id=uuid.uuid4(), name=f"sc-{uuid.uuid4().hex[:6]}", yaml=yaml_text, tenant_id=tenant)
    db.add(sc)
    db.flush()
    return sc


def _range(db, state=RangeState.ready, tenant=TENANT_ID) -> Range:
    t = Template(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}", yaml="name: t", tenant_id=tenant)
    db.add(t)
    db.flush()
    r = Range(id=uuid.uuid4(), name="r", template_id=t.id, tenant_id=tenant, state=state)
    db.add(r)
    db.flush()
    return r


def _exercise(db, sc: Scenario, rng: Range, tenant=TENANT_ID) -> Exercise:
    ex = Exercise(id=uuid.uuid4(), name="ex", range_id=rng.id, scenario_id=sc.id, tenant_id=tenant)
    db.add(ex)
    db.commit()
    return ex


def _sent(broker, task: str) -> list[list]:
    return [args for name, args in broker.sent if name == f"worker.tasks.{task}"]


def test_injector_catalogue_advertises_what_runs_on_the_mock_backend(client):
    items = {i["name"]: i for i in client.get("/injectors").json()}
    assert items["simulated_execution"]["touches_range_hosts"] is False
    assert items["dns_spike"]["touches_range_hosts"] is True
    assert {i["execution_mode"] for i in items.values()} == {"simulated"}


# -- DELETE /scenarios/{id} ------------------------------------------------------
class TestDeleteScenario:
    def test_in_use_by_an_exercise_is_409_not_500(self, client, db_session):
        sc = _scenario(db_session)
        _exercise(db_session, sc, _range(db_session))
        resp = client.delete(f"/scenarios/{sc.id}")
        assert resp.status_code == 409
        assert resp.json()["detail"].startswith("Scenario is used by 1 exercise(s)")
        assert db_session.get(Scenario, sc.id) is not None

    def test_in_use_by_a_soft_deleted_exercise_is_still_409(self, client, db_session):
        from datetime import UTC, datetime

        sc = _scenario(db_session)
        ex = _exercise(db_session, sc, _range(db_session))
        ex.deleted_at = datetime.now(UTC)
        db_session.commit()
        assert client.delete(f"/scenarios/{sc.id}").status_code == 409

    def test_unused_is_204(self, client, db_session):
        sc = _scenario(db_session)
        db_session.commit()
        assert client.delete(f"/scenarios/{sc.id}").status_code == 204
        assert client.get(f"/scenarios/{sc.id}").status_code == 404

    def test_an_execution_does_not_block_deletion(self, client, db_session):
        sc = _scenario(db_session)
        rng = _range(db_session)
        db_session.commit()
        assert (
            client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)}).status_code
            == 202
        )
        assert client.delete(f"/scenarios/{sc.id}").status_code == 204

    def test_a_race_with_a_new_exercise_is_409(self, client, db_session, monkeypatch):
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.orm import Session

        sc = _scenario(db_session)
        db_session.commit()

        def _fk_violation(self):
            raise IntegrityError("DELETE", {}, Exception("exercises_scenario_id_fkey"))

        monkeypatch.setattr(Session, "commit", _fk_violation)
        resp = client.delete(f"/scenarios/{sc.id}")
        assert resp.status_code == 409 and "used by an exercise" in resp.json()["detail"]


# -- POST /scenarios/execute -------------------------------------------------------
class TestExecuteScenario:
    def test_queues_the_run_with_the_parsed_definition(self, client, db_session, no_real_broker):
        sc, rng = _scenario(db_session), _range(db_session)
        db_session.commit()
        resp = client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["state"] == "pending" and body["scenario_name"] == sc.name
        [[xid, definition]] = _sent(no_real_broker, "run_scenario_execution")
        assert xid == body["id"]
        assert [e["action"] for e in definition["timeline"]] == ["inject.simulated_execution", "dns_spike"]
        assert definition["objectives"] == [{"ref_id": "obj-1", "description": "Detect the execution"}]
        x = db_session.get(ScenarioExecution, uuid.UUID(xid))
        assert x.task_id and x.tenant_id == TENANT_ID

    def test_another_tenants_scenario_is_404(self, client, db_session, no_real_broker):
        sc, rng = _scenario(db_session, tenant=OTHER_TENANT), _range(db_session)
        db_session.commit()
        resp = client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})
        assert resp.status_code == 404
        assert _sent(no_real_broker, "run_scenario_execution") == []

    def test_another_tenants_range_is_404(self, client, db_session):
        sc, rng = _scenario(db_session), _range(db_session, tenant=OTHER_TENANT)
        db_session.commit()
        resp = client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})
        assert resp.status_code == 404

    @pytest.mark.parametrize("state", [RangeState.created, RangeState.provisioning, RangeState.destroyed])
    def test_a_range_that_is_not_ready_is_409(self, client, db_session, no_real_broker, state):
        sc, rng = _scenario(db_session), _range(db_session, state=state)
        db_session.commit()
        resp = client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})
        assert resp.status_code == 409 and state.value in resp.json()["detail"]
        assert _sent(no_real_broker, "run_scenario_execution") == []

    @pytest.mark.parametrize(
        ("doc", "detail"),
        [("- just\n- a list\n", "must be a mapping"), ("name: x\ntimeline: {t: '00:00'}\n", "timeline must be a list")],
    )
    def test_a_malformed_definition_is_422_and_not_run(self, client, db_session, no_real_broker, doc, detail):
        sc, rng = _scenario(db_session, yaml_text=doc), _range(db_session)
        db_session.commit()
        resp = client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})
        assert resp.status_code == 422 and detail in resp.json()["detail"]
        assert _sent(no_real_broker, "run_scenario_execution") == []

    def test_a_down_broker_is_503_and_the_execution_is_recorded_failed(self, client, db_session, monkeypatch):
        from app import celery_client

        monkeypatch.setattr(celery_client, "dispatch", lambda *a: None)
        sc, rng = _scenario(db_session), _range(db_session)
        db_session.commit()
        resp = client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})
        assert resp.status_code == 503
        [x] = db_session.query(ScenarioExecution).filter(ScenarioExecution.scenario_id == sc.id).all()
        assert x.state == "failed" and "not queued" in x.error


# -- results / timeline --------------------------------------------------------------
def _execute(client, db) -> str:
    sc, rng = _scenario(db), _range(db)
    db.commit()
    return client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)}).json()["id"]


def _record(db, xid: str, seq: int, status: str, **kw) -> None:
    db.add(
        InjectRecord(
            execution_id=uuid.UUID(xid),
            seq=seq,
            t=kw.pop("t", "00:00"),
            action=kw.pop("action", "simulated_execution"),
            status=status,
            **kw,
        )
    )
    db.commit()


class TestExecutionResults:
    def test_timeline_is_pending_until_the_worker_records_it(self, client, db_session):
        xid = _execute(client, db_session)
        resp = client.get(f"/scenarios/executions/{xid}/timeline")
        assert resp.status_code == 200
        assert [(e["seq"], e["action"], e["status"]) for e in resp.json()] == [
            (0, "inject.simulated_execution", "pending"),
            (1, "dns_spike", "pending"),
        ]

    def test_timeline_and_results_reflect_recorded_outcomes(self, client, db_session):
        xid = _execute(client, db_session)
        _record(db_session, xid, 0, "fired", execution_mode="simulated", mitre_technique="T1059", telemetry_count=1)
        _record(
            db_session,
            xid,
            1,
            "skipped",
            action="dns_spike",
            detail="skipped: mock backend (injector needs range hosts)",
        )
        timeline = client.get(f"/scenarios/executions/{xid}/timeline").json()
        assert [e["status"] for e in timeline] == ["fired", "skipped"]
        assert timeline[0]["execution_mode"] == "simulated" and timeline[0]["telemetry_count"] == 1
        results = client.get(f"/scenarios/executions/{xid}/results").json()
        assert results["injects"] == {"total": 2, "fired": 1, "skipped": 1, "failed": 0, "pending": 0}
        # Never scored: no Students, no evidence review. Absence of evidence is not a pass.
        assert results["objectives"] == [
            {"ref_id": "obj-1", "description": "Detect the execution", "status": "unassessed"}
        ]

    def test_unknown_or_foreign_execution_is_404(self, client, db_session):
        assert client.get(f"/scenarios/executions/{uuid.uuid4()}/results").status_code == 404
        x = ScenarioExecution(tenant_id=OTHER_TENANT, scenario_name="x", state="completed", definition={})
        db_session.add(x)
        db_session.commit()
        assert client.get(f"/scenarios/executions/{x.id}/timeline").status_code == 404
        assert client.get(f"/scenarios/executions/{x.id}/results").status_code == 404


# -- GET /exercises/{id}/injects ------------------------------------------------------
class TestExerciseInjects:
    def test_lists_recorded_injects(self, client, db_session):
        ex = _exercise(db_session, _scenario(db_session), _range(db_session))
        db_session.add_all(
            [
                InjectRecord(
                    exercise_id=ex.id,
                    run_id="r1",
                    seq=0,
                    t="00:00",
                    action="simulated_execution",
                    status="fired",
                    execution_mode="simulated",
                    telemetry_count=1,
                    telemetry_shipped=True,
                ),
                InjectRecord(
                    exercise_id=ex.id,
                    source="instructor",
                    action="dns_spike",
                    status="skipped",
                    detail="skipped: mock backend (injector needs range hosts)",
                ),
            ]
        )
        db_session.commit()
        resp = client.get(f"/exercises/{ex.id}/injects")
        assert resp.status_code == 200
        rows = resp.json()
        assert {(r["source"], r["action"], r["status"]) for r in rows} == {
            ("timeline", "simulated_execution", "fired"),
            ("instructor", "dns_spike", "skipped"),
        }

    def test_another_tenants_exercise_is_404(self, client, db_session):
        ex = _exercise(
            db_session,
            _scenario(db_session, tenant=OTHER_TENANT),
            _range(db_session, tenant=OTHER_TENANT),
            tenant=OTHER_TENANT,
        )
        assert client.get(f"/exercises/{ex.id}/injects").status_code == 404


# -- POST /ops/exercises/{id}/inject ------------------------------------------------------
class TestInstructorInjectDispatch:
    def test_dispatches_run_inject(self, client, db_session, no_real_broker):
        ex = _exercise(db_session, _scenario(db_session), _range(db_session))
        resp = client.post(
            f"/ops/exercises/{ex.id}/inject", json={"inject_type": "dns_spike", "params": {"domains": ["a.test"]}}
        )
        assert resp.status_code == 200
        assert resp.json()["dispatch"]["queued"] is True
        assert _sent(no_real_broker, "run_inject") == [[str(ex.id), "dns_spike", {"domains": ["a.test"]}]]

    def test_a_custom_inject_is_broadcast_only(self, client, db_session, no_real_broker):
        ex = _exercise(db_session, _scenario(db_session), _range(db_session))
        resp = client.post(f"/ops/exercises/{ex.id}/inject", json={"inject_type": "custom"})
        assert resp.json()["dispatch"] == {"queued": False, "task_id": None, "reason": "custom inject: broadcast only"}
        assert _sent(no_real_broker, "run_inject") == []

    def test_a_down_broker_is_reported_not_raised(self, client, db_session, monkeypatch):
        from app import celery_client

        monkeypatch.setattr(celery_client, "dispatch", lambda *a: None)
        ex = _exercise(db_session, _scenario(db_session), _range(db_session))
        resp = client.post(f"/ops/exercises/{ex.id}/inject", json={"inject_type": "simulated_execution"})
        assert resp.status_code == 200
        assert resp.json()["dispatch"] == {"queued": False, "task_id": None, "reason": "worker broker unavailable"}
