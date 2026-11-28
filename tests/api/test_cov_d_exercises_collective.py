# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Collective exercise router (``app.routers.exercises_collective``): the author's flows.

Complements test_qsp_tenant_isolation.py (cross-tenant is 404 everywhere) and
test_authoring_enablers.py (MESL patch vocabularies) with the rest: creating a
collective with objectives (duplicate refs skipped, refs auto-numbered), bad range
422, the list/get projections, objective and MESL CSV import (BOM, aliases, wholesale
replace), and MESL generation: 422 without objectives, a real answer parsed into
MeslEvent rows, an unusable answer 502, an orchestrator outage 502 and the
"AI not configured" state surfaced as its own 503.

No network: the orchestrator's sync ``httpx.post`` is the seam (no AI orchestrator runs
here; the MESL board is instructor-only so every request acts as the seeded admin).
"""

from __future__ import annotations

import httpx
from app.main import app as fastapi_app  # noqa: F401  (client fixture wires it)
from app.models import Exercise, ExerciseObjective, MeslEvent
from app.routers import exercises_collective as collective_router

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


def _seed_range(db, name="covd-range", state="ready"):
    from app.models import Range, Template

    tpl = Template(name=f"{name}-tpl", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db.add(tpl)
    db.flush()
    rng = Range(name=name, template_id=tpl.id, tenant_id=DEV_TENANT, state=state)
    db.add(rng)
    db.commit()
    return rng


def _collective(db, rng, name="Cobra drill") -> Exercise:
    ex = Exercise(name=name, kind="collective", range_id=rng.id, tenant_id=DEV_TENANT)
    db.add(ex)
    db.flush()
    return ex


# ── Create ───────────────────────────────────────────────────────────────


def test_create_ignores_duplicate_refs_and_numbers_the_rest(client, db_session):
    rng = _seed_range(db_session)
    r = client.post(
        "/collective-exercises",
        json={
            "name": "Cobra drill",
            "range_id": str(rng.id),
            "objectives": [
                {"ref": "O1", "text": "Detect the lure", "moe": "80% click rate down", "competency_code": "SOC-2"},
                {"ref": "O1", "text": "duplicate, dropped"},
                {"text": "no ref given"},  # gets the next free O<n>
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "collective"
    assert body["objectives_added"] == 2  # the duplicate O1 was skipped

    objs = db_session.query(ExerciseObjective).filter_by(exercise_id=body["id"]).all()
    refs = sorted(o.ref for o in objs)
    assert refs == ["O1", "O3"]  # numbered from the row index, counting the skipped duplicate
    by_ref = {o.ref: o for o in objs}
    assert by_ref["O1"].competency_code == "SOC-2"
    assert by_ref["O3"].text == "no ref given"


def test_create_on_an_unknown_or_foreign_range_is_422(client, db_session):
    import uuid

    r = client.post("/collective-exercises", json={"name": "x", "range_id": str(uuid.uuid4())})
    assert r.status_code == 422, r.text
    assert "range not found" in r.text
    assert db_session.query(Exercise).count() == 0

    r = client.post("/collective-exercises", json={"name": "x", "range_id": "not-a-uuid"})
    assert r.status_code == 422, r.text


# ── List / get projections ───────────────────────────────────────────────


def test_list_shows_only_collective_exercises_with_counts(client, db_session):
    rng = _seed_range(db_session)
    mine = _collective(db_session, rng, "Listed drill")
    solo = Exercise(name="plain", range_id=rng.id, tenant_id=DEV_TENANT)  # kind != collective
    db_session.add(solo)
    db_session.add(MeslEvent(exercise_id=mine.id, serial=1, title="First move", delivery_method="cyber"))
    db_session.add(ExerciseObjective(exercise_id=mine.id, ref="O1", text="t", ordinal=0))
    db_session.commit()

    rows = {row["id"]: row for row in client.get("/collective-exercises").json()}
    assert str(solo.id) not in rows
    row = rows[str(mine.id)]
    assert row["name"] == "Listed drill"
    assert row["state"] == "pending"
    assert row["objectives"] == 1
    assert row["mesl_events"] == 1


def test_get_orders_objectives_by_ordinal_and_mesl_by_serial(client, db_session):
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    db_session.add_all(
        [
            ExerciseObjective(exercise_id=ex.id, ref="O2", text="second", ordinal=1),
            ExerciseObjective(exercise_id=ex.id, ref="O1", text="first", ordinal=0),
            MeslEvent(exercise_id=ex.id, serial=2, title="Later", delivery_method="email", status="planned"),
            MeslEvent(exercise_id=ex.id, serial=1, title="Earlier", delivery_method="cyber", status="planned"),
        ]
    )
    db_session.commit()

    body = client.get(f"/collective-exercises/{ex.id}").json()
    assert [o["ref"] for o in body["objectives"]] == ["O1", "O2"]
    assert [m["serial"] for m in body["mesl"]] == [1, 2]
    assert body["mesl"][0]["title"] == "Earlier"


def test_get_is_404_for_a_non_collective_exercise_and_a_bad_id(client, db_session):
    rng = _seed_range(db_session)
    solo = Exercise(name="plain", range_id=rng.id, tenant_id=DEV_TENANT)
    db_session.add(solo)
    db_session.commit()

    r = client.get(f"/collective-exercises/{solo.id}")
    assert r.status_code == 404, r.text
    assert "collective exercise not found" in r.text
    assert client.get("/collective-exercises/not-a-uuid").status_code == 404


# ── CSV import ───────────────────────────────────────────────────────────


def test_objectives_import_handles_a_bom_and_the_objective_column_alias(client, db_session):
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    csv = "ref,text,moe,competency_code\nO1,Spot the beacon,9 of 10 runs,SOC-3\n".encode("utf-8-sig")
    r = client.post(f"/collective-exercises/{ex.id}/objectives/import", files=[("file", ("obj.csv", csv, "text/csv"))])
    assert r.status_code == 200, r.text
    assert r.json()["objectives_added"] == 1
    row = db_session.query(ExerciseObjective).filter_by(exercise_id=ex.id).one()
    assert row.ref == "O1" and row.moe == "9 of 10 runs" and row.competency_code == "SOC-3"


MESL_CSV = (
    "Serial,Phase,Scenario Time,Title,Delivery Method,To Participant,Expected Action\n"
    "2,II,D1 1200,Lateral move,email,Blue-1,Correlate the alerts\n"
    "1,I,D1 0900,Initial access,White Cell,OPFOR,Stage the inject\n"
)


def test_mesl_import_replaces_the_whole_list(client, db_session):
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    db_session.add(MeslEvent(exercise_id=ex.id, serial=9, title="Stale serial", delivery_method="cyber"))
    db_session.commit()

    r = client.post(
        f"/collective-exercises/{ex.id}/mesl/import", files=[("file", ("mesl.csv", MESL_CSV.encode(), "text/csv"))]
    )
    assert r.status_code == 200, r.text
    assert r.json()["serials"] == 2
    events = db_session.query(MeslEvent).filter_by(exercise_id=ex.id).all()
    by_serial = {e.serial: e for e in events}
    assert set(by_serial) == {1, 2}  # the stale serial is gone, the old row replaced
    # aliases and vocabulary: "White Cell" normalized, generated_by_model empty (imported)
    assert by_serial[1].delivery_method == "white_cell"
    assert by_serial[1].to_participant == "OPFOR"
    assert by_serial[2].expected_action == "Correlate the alerts"
    assert by_serial[1].generated_by_model == ""


# ── MESL generation ──────────────────────────────────────────────────────


def _add_objective(db, ex, ref="O1", text="Detect C2 beacons"):
    db.add(ExerciseObjective(exercise_id=ex.id, ref=ref, text=text, ordinal=0))
    db.commit()


def test_generate_refuses_an_exercise_without_objectives(client, db_session):
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    r = client.post(f"/collective-exercises/{ex.id}/mesl/generate", json={})
    assert r.status_code == 422, r.text
    assert "add objectives" in r.text


MESL_ANSWER = [
    {"serial": 1, "title": "Beacon starts", "delivery_method": "cyber", "objective_ref": "O1", "phase": "I"},
    {"serial": 2, "title": "Exfil attempt", "delivery_method": "email", "objective_ref": "O1", "phase": "II"},
]


def test_generate_parses_the_answer_into_serials_and_replaces_the_old_list(client, db_session, monkeypatch):
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng, "Generated drill")
    _add_objective(db_session, ex)
    db_session.add(MeslEvent(exercise_id=ex.id, serial=5, title="Old draft", delivery_method="cyber"))
    db_session.commit()

    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"], sent["body"], sent["timeout"] = url, json, timeout
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"output": f"```json\n{_mesl_json()}\n```", "model_used": "mesl-v9"},
        )

    def _mesl_json():
        import json

        return json.dumps(MESL_ANSWER)

    monkeypatch.setattr(collective_router.httpx, "post", fake_post)
    r = client.post(f"/collective-exercises/{ex.id}/mesl/generate", json={"event_count": 3, "duration_days": 2})
    assert r.status_code == 200, r.text
    assert r.json() == {"generated": True, "serials": 2, "model_used": "mesl-v9"}

    events = db_session.query(MeslEvent).filter_by(exercise_id=ex.id).all()
    assert sorted(e.serial for e in events) == [1, 2]  # the old draft (serial 5) was replaced
    first = next(e for e in events if e.serial == 1)
    assert first.title == "Beacon starts"
    assert first.status == "planned"  # a generated serial always starts planned
    assert first.generated_by_model == "mesl-v9"

    assert sent["url"].endswith("/ai/mesl-generate")
    assert sent["body"]["event_count"] == 3
    assert sent["body"]["duration_days"] == 2
    assert sent["body"]["objectives"] == [{"ref": "O1", "text": "Detect C2 beacons", "moe": ""}]
    assert sent["timeout"] == 330


def test_generate_surfaces_an_unusable_answer_as_502(client, db_session, monkeypatch):
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    _add_objective(db_session, ex)

    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(
            200, request=httpx.Request("POST", url), json={"output": "The model wrote prose with no JSON list."}
        )

    monkeypatch.setattr(collective_router.httpx, "post", fake_post)
    r = client.post(f"/collective-exercises/{ex.id}/mesl/generate", json={})
    assert r.status_code == 502, r.text
    assert "no usable MESL" in r.text


def test_generate_surfaces_an_orchestrator_outage_as_502(client, db_session, monkeypatch):
    import uuid as _uuid

    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    _add_objective(db_session, ex)

    def refused(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError(f"[Errno 111] {_uuid.uuid4()}")

    monkeypatch.setattr(collective_router.httpx, "post", refused)
    r = client.post(f"/collective-exercises/{ex.id}/mesl/generate", json={})
    assert r.status_code == 502, r.text
    assert "MESL generation failed" in r.text


def test_generate_maps_an_unconfigured_host_to_503_not_502(client, db_session, monkeypatch):
    """A 503 carrying X-AI-Not-Configured is the host's deliberate state, not a bad answer."""
    rng = _seed_range(db_session)
    ex = _collective(db_session, rng)
    _add_objective(db_session, ex)

    def not_configured(url, json=None, headers=None, timeout=None):
        return httpx.Response(
            503,
            request=httpx.Request("POST", url),
            headers={"X-AI-Not-Configured": "true"},
            json={"detail": "no endpoint configured"},
        )

    monkeypatch.setattr(collective_router.httpx, "post", not_configured)
    r = client.post(f"/collective-exercises/{ex.id}/mesl/generate", json={})
    assert r.status_code == 503, r.text
    assert "AI is not configured" in r.text


def test_parse_ai_json_pulls_a_list_out_of_prose():
    assert collective_router._parse_ai_json("Plan:\n[{'x': 1}]".replace("'", '"')) == [{"x": 1}]
    assert collective_router._parse_ai_json("no list here") is None
