"""Students do not see what an inject did, over HTTP or WebSocket (H3).

Before: GET /exercises/{id}/injects returned action, detail and MITRE technique to anyone
with exercise:read, and an instructor inject's type and params were broadcast on
``exercise.<id>``, which every user of the tenant may subscribe to.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.scenario_runs import InjectRecord
from app.ws_auth import authorize, staff_channel


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session, "inj-a"), real_tenant(db_session, "inj-b")
    ex = real_exercise(db_session, a.id)
    db_session.add(InjectRecord(
        id=uuid.uuid4(), exercise_id=ex.id, tenant_id=a.id, source="timeline", seq=0, t="0:05",
        action="dns_beacon", status="fired", detail="beacon to northwind-update.example",
        mitre_technique="T1071.004", telemetry_count=12, telemetry_shipped=True,
    ))
    db_session.flush()
    return {
        "ex": ex,
        "student": real_user(db_session, UserRole.student, a.id),
        "instructor": real_user(db_session, UserRole.instructor, a.id),
        "other_instructor": real_user(db_session, UserRole.instructor, b.id),
    }


def test_a_student_sees_only_when_and_whether_injects_ran(client, world):
    act_as(world["student"])
    [row] = client.get(f"/exercises/{world['ex'].id}/injects").json()
    assert (row["seq"], row["t"], row["status"]) == (0, "0:05", "fired")
    assert row["action"] == "" and row["detail"] == "" and row["mitre_technique"] is None
    assert row["telemetry_count"] == 0


def test_an_instructor_sees_the_full_record(client, world):
    act_as(world["instructor"])
    [row] = client.get(f"/exercises/{world['ex'].id}/injects").json()
    assert row["action"] == "dns_beacon" and row["mitre_technique"] == "T1071.004"
    assert "northwind" in row["detail"]


def test_the_staff_channel_needs_scenario_update_and_the_tenant(db_session, world):
    ch = staff_channel(world["ex"].id)
    assert authorize(ch, world["instructor"], db_session)
    assert not authorize(ch, world["student"], db_session)
    assert not authorize(ch, world["other_instructor"], db_session)
    assert authorize(f"exercise.{world['ex'].id}", world["student"], db_session)  # participants still get theirs


class _Ws:
    def __init__(self):
        self.sent: list[tuple[str, str, dict]] = []

    async def broadcast(self, channel, kind, data):
        self.sent.append((channel, kind, data))


@pytest.mark.parametrize("inject_type", ["dns_spike", "custom"])
def test_inject_params_go_to_the_staff_channel_only(client, world, monkeypatch, inject_type):
    ws = _Ws()
    monkeypatch.setattr(fastapi_app.state, "ws_manager", ws, raising=False)
    act_as(world["instructor"])
    ex = world["ex"]

    r = client.post(f"/ops/exercises/{ex.id}/inject", json={
        "inject_type": inject_type, "params": {"domain": "evil.example"}, "description": "Phone call from the CEO",
    })

    assert r.status_code == 200, r.text
    sent = {ch: data for ch, kind, data in ws.sent if kind == "instructor_inject"}
    assert sent[staff_channel(ex.id)]["params"] == {"domain": "evil.example"}
    public = sent[f"exercise.{ex.id}"]
    assert "params" not in public and "inject_type" not in public and "evil.example" not in str(public)
    if inject_type == "custom":
        assert public["description"] == "Phone call from the CEO"  # narrative is for participants
    else:
        assert public["description"] is None
