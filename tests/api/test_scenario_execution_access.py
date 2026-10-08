"""Who may run a scenario execution, on which range, and who sees its answer key
(security sweep H2 and M2).

H2: ``POST /scenarios/execute`` only checked ``exercise:start``, which Students hold, so a
Student could fire host-affecting injects at any ready range in the tenant, a lab
session's included. M2: the execution timeline handed ``action`` / ``detail`` /
``mitre_technique`` (the inject playbook, ADR 0005 §5) to anyone with ``exercise:read``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Range, RangeState, Scenario, Template, Tenant, User, UserRole
from app.scenario_runs import InjectRecord

TENANT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")  # seeded by conftest
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

SCENARIO_YAML = """\
name: drill
timeline:
  - t: "00:00"
    action: inject.simulated_execution
    params: {technique: T1059}
  - t: "00:30"
    action: dns_spike
    params: {domains: [evil.test], count: 3}
"""


@pytest.fixture
def tenants(db_session):
    db_session.add(Tenant(id=TENANT_B, name="Other Org", slug=f"other-{uuid.uuid4().hex[:6]}"))
    db_session.flush()


def _user(db, role: UserRole, tenant=TENANT_A) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def signed_in(u: User):
    fastapi_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        tenant_id=str(u.tenant_id),
        keycloak_id=u.keycloak_id,
    )
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _scenario_and_range(db, tenant=TENANT_A) -> tuple[Scenario, Range]:
    sc = Scenario(id=uuid.uuid4(), name=f"sc-{uuid.uuid4().hex[:6]}", yaml=SCENARIO_YAML, tenant_id=tenant)
    t = Template(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}", yaml="name: t", tenant_id=tenant)
    db.add_all([sc, t])
    db.flush()
    r = Range(id=uuid.uuid4(), name="r", template_id=t.id, tenant_id=tenant, state=RangeState.ready)
    db.add(r)
    db.commit()
    return sc, r


def _execute(client, sc, rng):
    return client.post("/scenarios/execute", json={"scenario_id": str(sc.id), "range_id": str(rng.id)})


def test_a_student_cannot_execute_a_scenario(client, db_session):
    sc, rng = _scenario_and_range(db_session)
    with signed_in(_user(db_session, UserRole.student)):
        assert _execute(client, sc, rng).status_code == 403


def test_staff_cannot_execute_on_a_lab_session_range(client, db_session, monkeypatch):
    from app.lab_sessions import service as lab_service

    sc, rng = _scenario_and_range(db_session)
    monkeypatch.setattr(lab_service, "lab_range_ids", lambda db, ids: {rng.id} & set(ids))
    with signed_in(_user(db_session, UserRole.instructor)):
        r = _execute(client, sc, rng)
    assert r.status_code == 409
    assert "lab session" in r.json()["detail"]


def test_staff_execute_on_an_ordinary_range(client, db_session):
    sc, rng = _scenario_and_range(db_session)
    with signed_in(_user(db_session, UserRole.instructor)):
        assert _execute(client, sc, rng).status_code == 202


def test_staff_of_another_tenant_cannot_execute_here(client, db_session, tenants):
    sc, rng = _scenario_and_range(db_session)
    with signed_in(_user(db_session, UserRole.instructor, tenant=TENANT_B)):
        assert _execute(client, sc, rng).status_code == 404


def _executed_with_one_recorded(client, db_session) -> str:
    sc, rng = _scenario_and_range(db_session)
    with signed_in(_user(db_session, UserRole.instructor)):
        xid = _execute(client, sc, rng).json()["id"]
    db_session.add(
        InjectRecord(
            execution_id=uuid.UUID(xid),
            seq=0,
            t="00:00",
            action="simulated_execution",
            status="fired",
            detail="ran powershell -enc on ws-01",
            mitre_technique="T1059.001",
            execution_mode="simulated",
        )
    )
    db_session.commit()
    return xid


def test_a_student_sees_only_seq_t_and_status_of_recorded_events(client, db_session):
    xid = _executed_with_one_recorded(client, db_session)
    with signed_in(_user(db_session, UserRole.student)):
        r = client.get(f"/scenarios/executions/{xid}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert [(e["seq"], e["t"], e["status"]) for e in body] == [(0, "00:00", "fired")]  # pending event withheld
    assert all(e["action"] == "" and e["detail"] == "" for e in body)
    assert all(e["mitre_technique"] is None and e["execution_mode"] is None for e in body)
    text = r.text
    for secret in ("powershell", "T1059", "simulated_execution", "dns_spike", "evil.test"):
        assert secret not in text


def test_staff_see_the_whole_timeline(client, db_session):
    xid = _executed_with_one_recorded(client, db_session)
    with signed_in(_user(db_session, UserRole.instructor)):
        body = client.get(f"/scenarios/executions/{xid}/timeline").json()
    assert [e["status"] for e in body] == ["fired", "pending"]
    assert body[0]["mitre_technique"] == "T1059.001" and body[1]["action"] == "dns_spike"


def test_a_student_of_another_tenant_sees_nothing(client, db_session, tenants):
    xid = _executed_with_one_recorded(client, db_session)
    with signed_in(_user(db_session, UserRole.student, tenant=TENANT_B)):
        assert client.get(f"/scenarios/executions/{xid}/timeline").status_code == 404
