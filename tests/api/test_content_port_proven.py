"""Proven (owner decision 2026-10-27; app/content_port/proven.py): a course, range template
or scenario is proven when a range built from it reached Certified (from the template as it
is now), a closed class or a completed exercise using it has passing evidence, and the
content clock does not call it stale. Each missing condition is a reason; the evidence is
linked. Computed, never stored."""

from __future__ import annotations

import uuid

import pytest
from _content_port_kit import (
    DEV,
    acting_as,
    certify,
    closed_class,
    course,
    exercise,
    prove_course,
    prove_scenario,
    prove_template,
    proven_set,
    review,
    scenario,
    template,
)
from app.content_port import proven
from app.models import ExerciseState, Tenant, UserRole
from app.range_certification import sections as sec
from app.scheduler.models import EventState, ScheduledEvent


def _status(db, item_type, row, scope=None):
    return proven.status(db, item_type, row, scope)


def _cond(st, name):
    return next(c for c in st.conditions if c.name == name)


class TestRangeTemplate:
    def test_all_three_conditions_make_it_proven_with_evidence(self, db_session):
        t = template(db_session)
        rec = certify(db_session, t)
        ex = exercise(db_session, t)
        rv = review(db_session, "range_template", t)
        st = _status(db_session, "range_template", t)
        assert st.proven and st.reasons == []
        assert [e.id for e in _cond(st, "certified").evidence] == [str(rec.id)]
        assert [e.id for e in _cond(st, "used").evidence] == [str(ex.id)]
        assert [e.id for e in _cond(st, "fresh").evidence] == [str(rv.id)]
        assert st.golden_builds == [{"catalogue_id": "ubuntu-2404", "build_id": "b-2026-10-01", "checksum": "a" * 64}]

    def test_no_certification_record(self, db_session):
        t = template(db_session)
        exercise(db_session, t)
        review(db_session, "range_template", t)
        st = _status(db_session, "range_template", t)
        assert not st.proven
        assert st.reasons == ["No range built from it has reached Certified yet (no certification record)"]

    @pytest.mark.parametrize("status,label", [(sec.INCOMPLETE, "Ready, not certified"), (sec.FAIL, "Not safe")])
    def test_a_record_that_is_not_certified(self, db_session, status, label):
        t = template(db_session)
        certify(db_session, t, status=status)
        exercise(db_session, t)
        review(db_session, "range_template", t)
        st = _status(db_session, "range_template", t)
        assert not st.proven
        assert st.reasons[0].startswith(f"Its newest range is {label}")

    def test_certified_from_an_earlier_version_does_not_count(self, db_session):
        t = template(db_session)
        certify(db_session, t, hash_of="name: older\nnodes: []\n")
        exercise(db_session, t)
        review(db_session, "range_template", t)
        st = _status(db_session, "range_template", t)
        assert not st.proven
        assert "Certified only from an earlier version" in st.reasons[0]
        assert st.golden_builds == []

    def test_not_used(self, db_session):
        t = template(db_session)
        certify(db_session, t)
        review(db_session, "range_template", t)
        st = _status(db_session, "range_template", t)
        assert [c.name for c in st.conditions if not c.met] == ["used"]

    @pytest.mark.parametrize("kw", [{"score": (60, 100)}, {"score": (0, 0)}, {"state": ExerciseState.running}])
    def test_an_exercise_without_a_passing_score_does_not_count(self, db_session, kw):
        t = template(db_session)
        certify(db_session, t)
        exercise(db_session, t, **kw)
        review(db_session, "range_template", t)
        assert not _cond(_status(db_session, "range_template", t), "used").met

    def test_a_closed_class_with_a_passing_student_counts(self, db_session):
        t = template(db_session)
        certify(db_session, t)
        review(db_session, "range_template", t)
        cls = closed_class(db_session, course(db_session), tmpl=t)
        st = _status(db_session, "range_template", t)
        assert st.proven
        assert [e.id for e in _cond(st, "used").evidence] == [str(cls.id)]

    @pytest.mark.parametrize("kw", [{"outcome": "fail"}, {"outcome": "incomplete"}, {"closed": False}])
    def test_a_class_without_a_pass_or_not_closed_does_not_count(self, db_session, kw):
        t = template(db_session)
        closed_class(db_session, course(db_session), tmpl=t, **kw)
        assert not _cond(_status(db_session, "range_template", t), "used").met

    def test_not_on_the_content_clock(self, db_session):
        t = template(db_session)
        certify(db_session, t)
        exercise(db_session, t)
        st = _status(db_session, "range_template", t)
        assert st.reasons == ["Not on the content clock yet (its hourly sync adds new content)"]

    def test_stale_lists_why(self, db_session):
        t = template(db_session)
        certify(db_session, t)
        exercise(db_session, t)
        review(db_session, "range_template", t, stale=True, reasons=[
            {"kind": "vulnerability", "text": "Its golden template srv2022 is stale", "origin_id": "x"}])
        assert _status(db_session, "range_template", t).reasons == ["Stale: Its golden template srv2022 is stale"]

    def test_retired(self, db_session):
        t = template(db_session)
        certify(db_session, t)
        exercise(db_session, t)
        review(db_session, "range_template", t, retired=True)
        assert _status(db_session, "range_template", t).reasons == ["Retired on the content clock"]

    def test_another_tenants_evidence_is_not_counted_for_a_tenant(self, db_session):
        other = Tenant(id=uuid.uuid4(), name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        t = template(db_session, public=True)
        certify(db_session, t, tenant=other.id)
        exercise(db_session, t, tenant=other.id)
        review(db_session, "range_template", t)
        assert not _status(db_session, "range_template", t, {DEV}).proven
        assert _status(db_session, "range_template", t, None).proven


class TestScenario:
    def test_proven_through_its_range_templates_certification(self, db_session):
        t = prove_template(db_session, template(db_session))
        s = prove_scenario(db_session, scenario(db_session, t), t)
        st = _status(db_session, "scenario", s)
        assert st.proven
        assert [(d.item_type, d.item_id) for d in st.dependencies] == [("range_template", str(t.id))]

    def test_named_by_name(self, db_session):
        t = prove_template(db_session, template(db_session))
        s = prove_scenario(db_session, scenario(db_session, t, by_name=True), t)
        assert _status(db_session, "scenario", s).proven

    def test_its_template_not_certified(self, db_session):
        t = template(db_session)
        s = prove_scenario(db_session, scenario(db_session, t), t)
        st = _status(db_session, "scenario", s)
        assert [c.name for c in st.conditions if not c.met] == ["certified"]
        assert st.reasons[0].startswith(f"Range template {t.name}: No range built from it")

    def test_no_range_template(self, db_session):
        t = template(db_session)
        s = scenario(db_session, t)
        s.yaml = "name: lonely\n"
        review(db_session, "scenario", s)
        assert "names no range template" in _status(db_session, "scenario", s).reasons[0]

    def test_used_only_by_exercises_that_ran_it(self, db_session):
        t = prove_template(db_session, template(db_session))
        s = scenario(db_session, t)
        review(db_session, "scenario", s)
        exercise(db_session, t)  # the template's, not the scenario's
        assert not _cond(_status(db_session, "scenario", s), "used").met


class TestCourse:
    def test_proven_set(self, db_session):
        t, s, c = proven_set(db_session)
        st = _status(db_session, "course", c)
        assert st.proven
        assert [(d.item_type, d.item_id) for d in st.dependencies] == [("range_template", str(t.id)),
                                                                       ("scenario", str(s.id))]

    def test_needs_a_closed_class_with_a_pass(self, db_session):
        t = prove_template(db_session, template(db_session))
        s = prove_scenario(db_session, scenario(db_session, t), t)
        c = course(db_session, s)
        review(db_session, "course", c)
        st = _status(db_session, "course", c)
        assert st.reasons == ["No class of it has been closed with a passing Student yet"]

    def test_its_range_template_must_be_certified(self, db_session):
        t = template(db_session)
        s = scenario(db_session, t)
        c = prove_course(db_session, course(db_session, s))
        st = _status(db_session, "course", c)
        assert [c.name for c in st.conditions if not c.met] == ["certified"]

    def test_a_course_without_a_range_has_nothing_to_certify(self, db_session):
        c = prove_course(db_session, course(db_session))
        st = _status(db_session, "course", c)
        cert = _cond(st, "certified")
        assert st.proven and cert.not_applicable

    def test_a_booked_template_is_a_dependency(self, db_session):
        from datetime import UTC, datetime, timedelta

        t = template(db_session)
        c = course(db_session)
        db_session.add(ScheduledEvent(id=uuid.uuid4(), name="b", tenant_id=DEV, template_id=t.id, course_id=c.id,
                                      state=EventState.scheduled, start_time=datetime.now(UTC),
                                      end_time=datetime.now(UTC) + timedelta(hours=1)))
        db_session.flush()
        assert [d.item_id for d in _status(db_session, "course", c).dependencies] == [str(t.id)]


class TestApi:
    def test_the_status_endpoint(self, client, db_session):
        t, _s, _c = proven_set(db_session)
        with acting_as(db_session, UserRole.instructor):
            res = client.get(f"/content-port/items/range_template/{t.id}/proven")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["proven"] is True and body["label"] == "Proven"
        assert [c["name"] for c in body["conditions"]] == ["certified", "used", "fresh"]

    def test_not_proven_label_lists_reasons(self, client, db_session):
        t = template(db_session)
        with acting_as(db_session, UserRole.admin):
            body = client.get(f"/content-port/items/range_template/{t.id}/proven").json()
        assert body["proven"] is False
        assert body["label"].startswith("Not proven: No range built from it")

    def test_a_student_cannot_read_it(self, client, db_session):
        t = template(db_session)
        with acting_as(db_session, UserRole.student):
            assert client.get(f"/content-port/items/range_template/{t.id}/proven").status_code == 403

    def test_another_tenants_private_content_is_not_found(self, client, db_session):
        other = Tenant(id=uuid.uuid4(), name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        t = template(db_session, tenant=other.id)
        with acting_as(db_session, UserRole.admin):
            assert client.get(f"/content-port/items/range_template/{t.id}/proven").status_code == 404
