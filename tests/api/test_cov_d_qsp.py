# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""QSP qualification-spine router (``app.routers.qsp``): round-2 coverage.

Complements the existing qsp tests with the behaviours they reach only incidentally:
what the qualification detail actually surfaces (EOs, their lessons, competency tags,
the delivering course/module and the exercise/scenario/range refs), the broken
``critical_events`` JSON degrading to an empty list instead of a 500, the crosswalk
import's error contract (413 oversized, 422 non-UTF-8, 422 failure + rollback),
attach idempotency and detach, the developmental-progression split, curriculum-map
columns beyond the configured ladder, and the ``po_coverage`` report shape.

No network, no live services: everything stops at the router's decision over the
shared in-memory database.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.models import (
    Competency,
    CompetencyFramework,
    ContentKind,
    Course,
    CourseModule,
    EnablingObjective,
    Exercise,
    Lesson,
    LessonObjective,
    ModuleContent,
    ModuleContentType,
    ObjectiveCompetencyMap,
    PerformanceObjective,
    POTier,
    Qualification,
    Range,
    RangeObjectiveMap,
    Scenario,
    Template,
)

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")

CSV_HEAD = "qsp_code,po_id,nqual,po_title,duration_min,critical_events\n"


# ── Seeding helpers ───────────────────────────────────────────────────────


@pytest.fixture
def spine(db_session):
    """A shared (NULL-tenant) qualification with the two PO shapes under test.

    PO_001 carries broken `critical_events` JSON and an 8-hour duration (the
    `duration_long` verification flag); PO_002 is the well-formed contrast.
    """
    qual = Qualification(
        qsp_code="COVDQ",
        nqual="COVDQ",
        title="COV-D qualification",
        dp_order=1,
        track="progression",
        rank_level="Pte",
        tenant_id=None,
    )
    db_session.add(qual)
    db_session.flush()
    po_broken = PerformanceObjective(
        qualification_id=qual.id,
        po_code="PO_001",
        title="Broken events objective",
        tier=POTier.gate,
        duration_min=480,
        critical_events="not json",
    )
    po_good = PerformanceObjective(
        qualification_id=qual.id,
        po_code="PO_002",
        title="Good events objective",
        tier=POTier.core,
        duration_min=90,
        critical_events='["CE-1"]',
    )
    db_session.add_all([po_broken, po_good])
    db_session.flush()
    return {"qual": qual, "pos": {p.po_code: p for p in (po_broken, po_good)}}


def _eo(db, po, code, ce="CE-1"):
    eo = EnablingObjective(po_id=po.id, eo_code=code, title=f"EO {code}", maps_to_critical_event=ce)
    db.add(eo)
    db.flush()
    return eo


def _lesson(db, eo, title="Phishing fundamentals", published=True, minutes=45):
    lesson = Lesson(title=title, duration_minutes=minutes, is_published=published, tenant_id=DEV_TENANT)
    db.add(lesson)
    db.flush()
    db.add(LessonObjective(lesson_id=lesson.id, eo_id=eo.id))
    db.flush()
    return lesson


def _competency(db, po, code, relation="primary"):
    comp = Competency(code=code, name=f"Comp {code}", framework=CompetencyFramework.nice)
    db.add(comp)
    db.flush()
    db.add(ObjectiveCompetencyMap(po_id=po.id, competency_id=comp.id, relation_type=relation))
    db.flush()
    return comp


def _delivering_course(db, po, meta):
    """The course + module that deliver `po` via the only link progress walks."""
    course = Course(name=f"Course for {po.po_code}", tenant_id=DEV_TENANT, course_meta=json.dumps(meta))
    db.add(course)
    db.flush()
    module = CourseModule(
        course_id=course.id,
        ordinal=0,
        title=po.title,
        content_type=ModuleContentType.scenario,
        po_id=po.id,
    )
    db.add(module)
    db.flush()
    return course, module


def _assess_scenario(db, module, with_exercise):
    """An assess content row on `module`, optionally wired to a real exercise on a range."""
    scenario = Scenario(name=f"Scenario {module.title}", yaml="name: s\n", tenant_id=DEV_TENANT)
    db.add(scenario)
    db.flush()
    db.add(ModuleContent(module_id=module.id, ordinal=0, content_kind=ContentKind.assess, scenario_id=scenario.id))
    if with_exercise:
        template = Template(
            name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml="name: t\nnodes: []\n", tenant_id=DEV_TENANT
        )
        db.add(template)
        db.flush()
        rng = Range(name=f"range-{uuid.uuid4().hex[:6]}", template_id=template.id, tenant_id=DEV_TENANT)
        db.add(rng)
        db.flush()
        ex = Exercise(name=f"ex-{uuid.uuid4().hex[:6]}", range_id=rng.id, scenario_id=scenario.id, tenant_id=DEV_TENANT)
        db.add(ex)
        db.flush()
    db.flush()
    return scenario


# ── Qualification detail: what surfaces ───────────────────────────────────


def test_the_detail_surfaces_eos_lessons_competencies_refs_and_deliverer(client, db_session, spine):
    po = spine["pos"]["PO_001"]
    eo = _eo(db_session, po, "001.01")
    _lesson(db_session, eo)
    _competency(db_session, po, "T0023")
    meta = {
        "provenance": "authored",
        "course_code": "AUTH-101",
        "dp_order": 2,
        "institution": "Algonquin College",
        "term_code": "DP2-F",
    }
    _course, module = _delivering_course(db_session, po, meta)
    _assess_scenario(db_session, module, with_exercise=True)
    db_session.commit()

    body = client.get("/qsp/qualifications/COVDQ/objectives").json()
    out = next(o for o in body if o["po_code"] == "PO_001")
    assert out["tier"] == "gate"
    assert out["course_code"] == "COVDQ-001"  # work role blank -> nqual abbreviation
    assert out["duration_long"] is True  # 8h+ -> flagged for verification
    (eo_out,) = out["enabling_objectives"]
    assert eo_out["eo_code"] == "001.01" and eo_out["maps_to_critical_event"] == "CE-1"
    (brief,) = eo_out["lessons"]
    assert brief["title"] == "Phishing fundamentals"
    assert brief["duration_minutes"] == 45 and brief["is_published"] is True
    (tag,) = out["competencies"]
    assert tag == {"framework": "nice", "code": "T0023", "name": "Comp T0023", "relation": "primary"}
    delivered = out["delivered_by"]
    assert delivered["course_id"] and delivered["module_id"] == str(module.id)
    assert delivered["course_code"] == "AUTH-101" and delivered["dp_order"] == 2
    assert delivered["institution"] == "Algonquin College" and delivered["term_code"] == "DP2-F"
    assert delivered["is_placeholder"] is False  # provenance: authored, real courseware
    assert out["exercise_id"] and out["scenario_id"] and out["range_id"]


def test_a_po_without_a_module_reports_no_deliverer_and_untouched_learner_state(client, db_session, spine):
    body = client.get("/qsp/qualifications/COVDQ/objectives").json()
    out = next(o for o in body if o["po_code"] == "PO_002")
    assert out["critical_events"] == ["CE-1"]  # well-formed JSON survives
    assert out["duration_long"] is False
    assert out["delivered_by"] is None
    assert out["exercise_id"] is None and out["scenario_id"] is None and out["range_id"] is None
    assert out["competencies"] == []
    assert out["progress_state"] == "not_started"  # the detail view resolves no enrolments


def test_broken_critical_events_json_degrades_to_an_empty_list(client, db_session, spine):
    """`critical_events` is stored JSON; unparseable text must read as no events, never a 500."""
    body = client.get("/qsp/qualifications/COVDQ/objectives").json()
    out = next(o for o in body if o["po_code"] == "PO_001")
    assert out["critical_events"] == []


def test_a_stub_module_reads_as_a_placeholder_not_real_delivery(client, db_session, spine):
    """A spine-generated stub (no `provenance`) stands in for content that does not exist."""
    _course, _module = _delivering_course(db_session, spine["pos"]["PO_001"], meta={})
    db_session.commit()

    out = next(o for o in client.get("/qsp/qualifications/COVDQ/objectives").json() if o["po_code"] == "PO_001")
    delivered = out["delivered_by"]
    assert delivered["is_placeholder"] is True
    assert delivered["course_code"] == "" and delivered["dp_order"] == 0
    assert delivered["course_name"] == "Course for PO_001"


def test_assess_content_without_an_exercise_still_names_its_scenario(client, db_session, spine):
    """The scenario ref survives even when no exercise has been cut on a range for it."""
    _course, module = _delivering_course(db_session, spine["pos"]["PO_002"], meta={"provenance": "authored"})
    scenario = _assess_scenario(db_session, module, with_exercise=False)
    db_session.commit()

    out = next(o for o in client.get("/qsp/qualifications/COVDQ/objectives").json() if o["po_code"] == "PO_002")
    assert out["exercise_id"] is None and out["range_id"] is None
    assert out["scenario_id"] == str(scenario.id)


# ── Crosswalk import: the error contract ──────────────────────────────────


def test_an_oversized_crosswalk_is_refused_before_parsing(client):
    big = CSV_HEAD.encode() + b"x" * (4 * 1024 * 1024)
    r = client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", big, "text/csv")})
    assert r.status_code == 413, r.text
    assert "too large" in r.text


def test_a_non_utf8_crosswalk_is_a_422(client):
    r = client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", b"\xff\xfe\x00bad", "text/csv")})
    assert r.status_code == 422, r.text
    assert "UTF-8" in r.text


def test_a_failed_import_rolls_back_its_partial_writes_and_is_a_422(client, db_session, monkeypatch):
    """Whatever the ingest raises, the caller sees 422 and nothing partial is kept.

    The ingest writes a row of its own before failing, the way a mid-file parse
    error would; the 422 handler's rollback must take that row with it. (Whether
    *pre-existing* rows survive the same rollback cannot be observed in this
    shared-session fixture — the rollback reverts the test's own uncommitted
    seed too, exactly as documented in test_cov_d_course_publications.py.)
    """
    from app import qsp_ingest

    def explode(db, csv_text, tenant_id=None):
        # Write something before failing, the way a mid-file parse error would.
        db.add(Qualification(qsp_code="COVDPARTIAL", nqual="partial", title="partial"))
        db.flush()
        raise ValueError("bad row 7")

    monkeypatch.setattr(qsp_ingest, "import_crosswalk", explode)
    r = client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CSV_HEAD.encode(), "text/csv")})
    assert r.status_code == 422, r.text
    assert "crosswalk import failed" in r.text and "bad row 7" in r.text
    assert db_session.query(Qualification).filter_by(qsp_code="COVDPARTIAL").one_or_none() is None


def test_a_small_crosswalk_upserts_the_spine_it_names(client, db_session):
    csv_text = (
        CSV_HEAD + "COVDX,PO_001,COVDX,Import objective,60,CE-9\nCOVDX,PO_001,COVDX,Import objective v2,60,CE-9\n"
    )
    r = client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] is True and body["rows"] == 2
    assert body["qualifications"] == 1 and body["performance_objectives"] == 1
    assert body["updated_pos"] == 1  # the second row upserted the same PO
    listed = client.get("/qsp/qualifications").json()
    dx = next(q for q in listed if q["qsp_code"] == "COVDX")
    assert dx["po_count"] == 1 and dx["nqual"] == "COVDX"


def test_a_body_with_no_usable_rows_imports_nothing_but_is_not_an_error(client, db_session):
    """Prose that is not CSV parses as a header with no qsp_code/po_id columns: zero rows."""
    r = client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", b"this is not a crosswalk", "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 0
    assert db_session.query(Qualification).filter_by(qsp_code="COVDX").one_or_none() is None


def test_non_utf8_competency_seed_files_are_a_422(client):
    r = client.post(
        "/qsp/import-competency-crosswalk",
        files={"taxonomy": ("tax.csv", b"\xff\xfe", "text/csv"), "crosswalk": ("x.csv", b"a,b\n", "text/csv")},
    )
    assert r.status_code == 422, r.text
    assert "UTF-8" in r.text


# ── Attach / detach: the range-template link ─────────────────────────────


@pytest.fixture
def template(db_session):
    t = Template(name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml="name: t\nnodes: []\n", tenant_id=DEV_TENANT)
    db_session.add(t)
    db_session.commit()
    return t


def test_attaching_the_same_po_twice_links_it_once(client, db_session, spine, template):
    po_id = str(spine["pos"]["PO_001"].id)
    first = client.post(f"/qsp/templates/{template.id}/objectives", json={"po_ids": [po_id], "source": "forge"})
    assert first.status_code == 200, first.text
    assert first.json() == {"template_id": str(template.id), "linked": 1}

    again = client.post(f"/qsp/templates/{template.id}/objectives", json={"po_ids": [po_id]})
    assert again.status_code == 200, again.text
    assert again.json()["linked"] == 0  # idempotent upsert: nothing new
    (row,) = db_session.query(RangeObjectiveMap).filter_by(template_id=template.id).all()
    assert str(row.po_id) == po_id and row.source == "forge"  # the first source stands


def test_attaching_an_unknown_or_malformed_po_id_is_a_422(client, db_session, spine, template):
    for po_id in (str(uuid.uuid4()), "not-a-uuid"):
        r = client.post(f"/qsp/templates/{template.id}/objectives", json={"po_ids": [po_id]})
        assert r.status_code == 422, r.text
        assert f"unknown po_id {po_id}" in r.text
    assert db_session.query(RangeObjectiveMap).filter_by(template_id=template.id).count() == 0


def test_detach_says_whether_something_was_detached(client, db_session, spine, template):
    po = spine["pos"]["PO_001"]
    assert client.post(f"/qsp/templates/{template.id}/objectives", json={"po_ids": [str(po.id)]}).status_code == 200

    r = client.delete(f"/qsp/templates/{template.id}/objectives/{po.id}")
    assert r.status_code == 200, r.text
    assert r.json() == {"detached": True}
    again = client.delete(f"/qsp/templates/{template.id}/objectives/{po.id}")
    assert again.status_code == 200 and again.json() == {"detached": False}
    missing = client.delete(f"/qsp/templates/{template.id}/objectives/{uuid.uuid4()}")
    assert missing.status_code == 200 and missing.json() == {"detached": False}
    malformed = client.delete(f"/qsp/templates/{template.id}/objectives/not-a-uuid")
    assert malformed.status_code == 404, malformed.text  # indistinguishable from no such objective


# ── Developmental progression and the career map ─────────────────────────


@pytest.fixture
def specialty(db_session, spine):
    spec = Qualification(
        qsp_code="COVDS",
        nqual="COVD-S",
        title="Red stream",
        dp_order=2,
        track="specialty",
        rank_level="Cpl",
        tenant_id=None,
    )
    db_session.add(spec)
    db_session.flush()
    db_session.add(
        PerformanceObjective(
            qualification_id=spec.id,
            po_code="PO_001",
            title="spec objective",
            tier=POTier.core,
            duration_min=30,
            critical_events="[]",
        )
    )
    db_session.flush()
    return spec


def test_the_progression_splits_the_ladder_from_the_specialty_streams(client, spine, specialty):
    body = client.get("/qsp/developmental-progression").json()
    ladder = [e["qsp_code"] for e in body["progression"]]
    streams = [e["qsp_code"] for e in body["specialty_streams"]]
    assert ladder == ["COVDQ"] and streams == ["COVDS"]
    assert body["progression"][0]["po_count"] == 2 and body["progression"][0]["rank_level"] == "Pte"
    assert body["specialty_streams"][0]["po_count"] == 1 and body["specialty_streams"][0]["track"] == "specialty"


def test_the_map_adds_a_column_for_a_dp_beyond_the_configured_ladder(client, db_session, spine, specialty):
    """A qualification ingested past DP5 still gets a stage; un-ingested rungs stay planned.

    The beyond-ladder qualification is a specialty so the core ladder ends at DP1 and
    the planned edge into the first placeholder column stays derivable.
    """
    db_session.add(
        Qualification(
            qsp_code="COVDZ",
            nqual="COVD-Z",
            title="Far stream",
            dp_order=7,
            track="specialty",
            rank_level="Sgt",
            tenant_id=None,
        )
    )
    db_session.commit()

    payload = client.get("/qsp/curriculum-map").json()
    stages = {s["dp_order"]: s for s in payload["stages"]}
    assert sorted(stages) == [1, 2, 3, 4, 5, 7]
    assert stages[7] == {"dp_order": 7, "rank_level": "Sgt", "label": "", "planned": False}
    for dp in (3, 4, 5):
        assert stages[dp]["planned"] is True and stages[dp]["rank_level"] == ""
    assert stages[1]["planned"] is False and stages[1]["rank_level"] == "Pte"
    assert stages[2]["planned"] is False  # the DP2 specialty populates that rung

    tracks = payload["tracks"]
    assert tracks[0] == {"key": "progression", "label": "Core progression", "kind": "progression"}
    assert [t["key"] for t in tracks[1:]] == ["COVD-S", "COVD-Z"]  # specialty lanes, ordered by DP
    edges = {(e["from"], e["to"], e["kind"]) for e in payload["edges"]}
    assert ("COVDQ", "COVDS", "branch") in edges  # the ladder rung below the fork
    assert ("COVDQ", "COVDZ", "branch") in edges
    assert ("COVDQ", "planned:3", "planned") in edges  # first placeholder column past the ladder


def test_po_coverage_reports_which_objectives_something_delivers(client, db_session, spine):
    """`CourseModule.po_id` is the only link progress walks: the report names both sides."""
    course, module = _delivering_course(db_session, spine["pos"]["PO_001"], meta={"provenance": "authored"})
    db_session.add(
        CourseModule(
            course_id=course.id, ordinal=1, title="unbound module", content_type=ModuleContentType.reading, po_id=None
        )
    )
    db_session.flush()

    body = client.get("/qsp/po-coverage").json()
    assert body["objective_count"] == 2 and body["covered"] == 1 and body["uncovered"] == 1
    assert body["unbound_modules"] == 1
    by_code = {o["po_code"]: o for o in body["objectives"]}
    assert by_code["PO_001"]["module_count"] == 1
    assert by_code["PO_001"]["delivered_by"] == [{"course": course.name, "module": module.title}]
    assert by_code["PO_002"]["module_count"] == 0 and by_code["PO_002"]["delivered_by"] == []
    assert by_code["PO_001"]["qsp_code"] == "COVDQ" and by_code["PO_001"]["dp_order"] == 1
