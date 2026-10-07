"""After-action report: the data it is built from, the HTML page, the PDF, and who may read them.

The AAR used to be a one-line ``<h1>AAR: {name}</h1>`` with the name unescaped, every read
route looked the report up by exercise id with no tenant predicate, and the PDF route was
only ever exercised by hand.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.aar_html import detection_summary, pdf_text, render_html

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

SCENARIO_YAML = """\
name: aar-scenario
timeline:
  - t: "0:00"
    action: email_phish
    params: {subject: "Q4 report", mitre_technique: T1566}
  - t: "0:10"
    action: http_burst
    description: Beacon to C2
"""


def _world(db, tenant=DEV_TENANT, name="Op Northern Watch"):
    """A completed exercise with two objectives, a team, a MESL serial and an annotation."""
    from app.models import (
        AnalystAnnotation,
        Exercise,
        ExerciseState,
        MeslEvent,
        Objective,
        ObjectiveType,
        Range,
        Scenario,
        Team,
        TeamMembership,
        Template,
        User,
    )

    tpl = Template(name=f"tpl-{uuid.uuid4().hex[:6]}", version="1.0", yaml="nodes: []", tenant_id=tenant)
    db.add(tpl)
    db.flush()
    rng = Range(name="aar-range", template_id=tpl.id, tenant_id=tenant, state="ready")
    sc = Scenario(name="aar-scenario", yaml=SCENARIO_YAML, tenant_id=tenant)
    db.add_all([rng, sc])
    db.flush()
    start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    ex = Exercise(
        name=name,
        range_id=rng.id,
        scenario_id=sc.id,
        tenant_id=tenant,
        state=ExerciseState.completed,
        started_at=start,
        completed_at=start + timedelta(hours=2),
        total_score=60,
        max_score=100,
    )
    db.add(ex)
    db.flush()
    db.add_all(
        [
            Objective(
                exercise_id=ex.id,
                ref_id="det-c2",
                objective_type=ObjectiveType.detection,
                description="Detect the C2 beacon",
                validator="manual",
                points=60,
                achieved=True,
                evidence="alert 42",
                achieved_at=start + timedelta(minutes=30),
            ),
            Objective(
                exercise_id=ex.id,
                ref_id="report",
                objective_type=ObjectiveType.deliverable,
                description="Submit the incident report",
                validator="manual",
                points=40,
                achieved=False,
            ),
        ]
    )
    student = User(
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Cpl Avery Student",
        tenant_id=tenant,
    )
    team = Team(name="Blue Cell", tenant_id=tenant, exercise_id=ex.id)
    db.add_all([student, team])
    db.flush()
    db.add(TeamMembership(user_id=student.id, team_id=team.id, role="analyst"))
    db.add(MeslEvent(exercise_id=ex.id, serial=1, scenario_time="D1 0900", title="Ransom note", status="delivered"))
    db.add(
        AnalystAnnotation(
            exercise_id=ex.id,
            user_id=student.id,
            user_display_name="Cpl Avery Student",
            content="Beacon interval is 60s",
        )
    )
    db.commit()
    return ex


def _stored_aar(db, ex, report: dict | str):
    from app.models import AfterActionReport

    body = report if isinstance(report, str) else json.dumps(report)
    db.add(AfterActionReport(exercise_id=ex.id, report_json=body, report_html="<p>stale</p>"))
    db.commit()


# -- the report and its page ---------------------------------------------------------------
class TestGenerateAndRender:
    def test_generated_report_carries_every_section(self, client, db_session):
        ex = _world(db_session)
        resp = client.post(f"/exercises/{ex.id}/aar/generate")
        assert resp.status_code == 201, resp.text
        report = json.loads(resp.json()["report_json"])

        assert report["exercise"]["name"] == "Op Northern Watch"
        assert report["scenario"]["name"] == "aar-scenario"
        assert report["range"]["name"] == "aar-range"
        assert report["scores"] == {"total": 60, "max": 100, "pct": 60.0}
        assert report["summary"] == {"total_objectives": 2, "achieved": 1, "score_pct": 60.0}
        assert {o["ref_id"]: o["achieved"] for o in report["objectives"]} == {"det-c2": True, "report": False}
        assert [i["title"] for i in report["injects"]] == ["email_phish", "http_burst", "Ransom note"]
        assert report["injects"][0]["at"] == "T+0:00"
        titles = [t["title"] for t in report["timeline"]]
        assert titles[0] == "Exercise started" and "Objective det-c2 achieved" in titles
        assert titles.index("Objective det-c2 achieved") < titles.index("Exercise completed")
        assert report["participants"] == [{"name": "Cpl Avery Student", "team": "Blue Cell", "role": "analyst"}]

    def test_html_shows_summary_objectives_timeline_detections_participants(self, client, db_session):
        ex = _world(db_session)
        client.post(f"/exercises/{ex.id}/aar/generate")
        resp = client.get(f"/exercises/{ex.id}/aar/html")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        page = resp.text
        assert "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none';" in page
        for needle in (
            "Op Northern Watch",
            'id="summary"',
            "60.0%",
            "Detect the C2 beacon",
            "Submit the incident report",
            "Not achieved",
            "email_phish",
            "Ransom note",
            "Exercise completed",
            "Detection points: 60 of 60",
            "Cpl Avery Student",
            "Blue Cell",
        ):
            assert needle in page, needle

    def test_values_are_escaped(self, client, db_session):
        ex = _world(db_session, name='<script>alert("x")</script>')
        client.post(f"/exercises/{ex.id}/aar/generate")
        page = client.get(f"/exercises/{ex.id}/aar/html").text
        assert "<script>" not in page
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page

    def test_html_is_rendered_from_the_stored_json_not_the_stored_html(self, client, db_session):
        """Rows written by the worker or before this renderer get the full, escaped page."""
        ex = _world(db_session)
        _stored_aar(
            db_session,
            ex,
            {"exercise_name": "Worker <b>flat</b>", "total_score": 5, "max_score": 10, "objectives": []},
        )
        page = client.get(f"/exercises/{ex.id}/aar/html").text
        assert "stale" not in page
        assert "Worker &lt;b&gt;flat&lt;/b&gt;" in page and "50.0%" in page

    def test_unparseable_json_still_renders(self, client, db_session):
        ex = _world(db_session)
        _stored_aar(db_session, ex, "{not json")
        resp = client.get(f"/exercises/{ex.id}/aar/html")
        assert resp.status_code == 200 and "Unknown exercise" in resp.text

    def test_ai_analysis_is_escaped(self, client, db_session, monkeypatch):
        import httpx

        ex = _world(db_session)
        client.post(f"/exercises/{ex.id}/aar/generate")

        class _Resp:
            def json(self):
                return {"output": "## Summary\n**Good** <img src=x onerror=alert(1)>\n- contain faster", "model_used": "m"}

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
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        stored = client.post(f"/exercises/{ex.id}/aar/ai-enhance").json()["report_html"]
        served = client.get(f"/exercises/{ex.id}/aar/html").text
        for page in (stored, served):
            assert "<img" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
            assert "<strong>Good</strong>" in page and "<li>contain faster</li>" in page


# -- error paths and tenancy ------------------------------------------------------------
class TestErrorPaths:
    @pytest.mark.parametrize("suffix", ["", "/html", "/pdf"])
    def test_not_generated_is_404(self, client, db_session, suffix):
        ex = _world(db_session)
        resp = client.get(f"/exercises/{ex.id}/aar{suffix}")
        assert resp.status_code == 404
        assert "generate it first" in resp.json()["detail"]

    @pytest.mark.parametrize("suffix", ["", "/html", "/pdf"])
    def test_unknown_exercise_is_404(self, client, suffix):
        assert client.get(f"/exercises/{uuid.uuid4()}/aar{suffix}").status_code == 404

    @pytest.mark.parametrize("suffix", ["", "/html", "/pdf"])
    def test_another_tenants_aar_is_404(self, client, db_session, suffix):
        theirs = _world(db_session, tenant=OTHER_TENANT, name="theirs-must-not-leak")
        _stored_aar(db_session, theirs, {"exercise": {"name": "theirs-must-not-leak"}})
        resp = client.get(f"/exercises/{theirs.id}/aar{suffix}")
        assert resp.status_code == 404
        assert "theirs-must-not-leak" not in resp.text

    def test_another_tenants_aar_cannot_be_generated_or_enhanced(self, client, db_session):
        theirs = _world(db_session, tenant=OTHER_TENANT)
        _stored_aar(db_session, theirs, {"exercise": {"name": "x"}})
        assert client.post(f"/exercises/{theirs.id}/aar/generate").status_code == 404
        assert client.post(f"/exercises/{theirs.id}/aar/ai-enhance").status_code == 404


class TestPdf:
    def test_pdf_of_a_generated_report(self, client, db_session):
        ex = _world(db_session)
        client.post(f"/exercises/{ex.id}/aar/generate")
        resp = client.get(f"/exercises/{ex.id}/aar/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert f'filename="aar-{ex.id}.pdf"' in resp.headers["content-disposition"]
        assert resp.content.startswith(b"%PDF") and len(resp.content) > 1500

    def test_text_outside_latin1_does_not_crash(self, client, db_session):
        ex = _world(db_session, name="Łódź — 攻撃 🚨 “quoted” …")
        client.post(f"/exercises/{ex.id}/aar/generate")
        resp = client.get(f"/exercises/{ex.id}/aar/pdf")
        assert resp.status_code == 200 and resp.content.startswith(b"%PDF")

    @pytest.mark.parametrize(
        "report",
        [
            "{not json",
            "[1, 2]",
            {"exercise": "not-a-dict", "scores": None, "objectives": "nope", "timeline": [1, None]},
            {"objectives": [{"ref_id": "o", "description": "x" * 3000, "evidence": "ü" * 500}]},
        ],
    )
    def test_malformed_or_odd_reports_still_render(self, client, db_session, report):
        ex = _world(db_session)
        _stored_aar(db_session, ex, report)
        resp = client.get(f"/exercises/{ex.id}/aar/pdf")
        assert resp.status_code == 200 and resp.content.startswith(b"%PDF")


# -- pure helpers --------------------------------------------------------------------------
class TestHelpers:
    def test_pdf_text_keeps_latin1_and_transliterates_the_rest(self):
        assert pdf_text("Café ü") == "Café ü"
        assert pdf_text("Łódź") == "Lódz"  # ó is latin-1; ź loses its accent; Ł has no decomposition
        assert pdf_text("ﬁle") == "file"
        assert pdf_text("a — b – c “d” ‘e’ …") == "a - b - c \"d\" 'e' ..."
        assert pdf_text("攻撃🚨") == "???"
        assert pdf_text(None) == ""
        pdf_text("Ωmega ﬁ").encode("latin-1")  # never raises

    def test_detection_summary(self):
        objs = [
            {"type": "detection", "achieved": True, "points": 10},
            {"type": "detection", "achieved": False, "points": 5},
            {"type": "response", "achieved": True, "points": 99},
        ]
        s = detection_summary(objs)
        assert (s["total"], s["detected"], s["points_earned"], s["points_available"]) == (2, 1, 10, 15)

    def test_render_html_of_an_empty_report(self):
        page = render_html({})
        assert "Unknown exercise" in page
        assert "No objectives were recorded" in page and "No participants were recorded" in page
