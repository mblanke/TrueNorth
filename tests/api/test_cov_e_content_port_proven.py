# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/content_port/proven.py: the edges the port tests miss.

Everything runs on real rows built by ``_content_port_kit`` (the same builders the
content-port tests use) — loading, scoping, dependency reading, the certified/used/fresh
conditions, redaction and closure. No transport is involved, so nothing is stubbed.
"""

import uuid
from datetime import UTC, datetime, timedelta

from _content_port_kit import DEV, certify, course, ex_template, made_from, review, scenario, template
from app.auth import CurrentUser
from app.content_port import proven
from app.content_port.proven import (
    CERTIFIED,
    USED,
    Dependency,
    closure,
    course_scenario_ids,
    dependencies,
    effective_range_template,
    exercise_template_use,
    freshness,
    load,
    scope_for,
    sizing_problems,
    status,
    template_certification,
    visible,
)
from app.content_review.models import ContentReviewItem
from app.models import CourseModule, ModuleContentType, Scenario, Tenant, UserRole
from app.range_certification import sections as sec


def second_tenant(db):
    t = Tenant(id=uuid.uuid4(), name=f"Other Org {uuid.uuid4().hex[:4]}", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(t)
    db.flush()
    return t


def who(user_id, tenant, role=UserRole.admin) -> CurrentUser:
    return CurrentUser(
        id=str(user_id),
        email="who@example.test",
        display_name="who",
        role=role,
        tenant_id=str(tenant),
        keycloak_id="kc-test",
    )


# -- loading and scope --------------------------------------------------------


def test_load_returns_none_for_a_bad_id_and_an_unknown_type(db_session):
    assert load(db_session, "course", "not-a-uuid") is None
    assert load(db_session, "hovercraft", str(uuid.uuid4())) is None


def test_visible_is_own_tenant_or_shared(db_session):
    mine = template(db_session)
    other = second_tenant(db_session)
    private = template(db_session, tenant=other.id)
    public = template(db_session, tenant=other.id, public=True)
    assert visible(mine, DEV) is True
    assert visible(public, DEV) is True
    assert visible(private, DEV) is False


def test_scope_for_a_platform_admin_is_every_tenant(db_session):
    # A single-tenant install: DEV's admin is the platform administrator.
    assert scope_for(db_session, who(uuid.uuid4(), DEV)) is None


def test_scope_for_a_tenant_admin_is_their_tenant(db_session):
    other = second_tenant(db_session)  # a second tenant: nobody is the platform administrator
    assert scope_for(db_session, who(uuid.uuid4(), DEV)) == {DEV}
    assert scope_for(db_session, who(uuid.uuid4(), other.id, UserRole.instructor)) == {other.id}


# -- reading the content itself ------------------------------------------------


def test_a_document_that_names_nothing_is_an_empty_dict():
    assert proven._doc("not: [valid") == {}
    assert proven._doc("[1, 2]") == {}  # valid YAML, not an object
    assert proven._doc(None) == {}
    assert proven._doc("") == {}


def test_a_module_with_an_unreadable_content_ref_names_no_scenario(db_session):
    c = course(db_session)
    db_session.add(
        CourseModule(
            id=uuid.uuid4(),
            course_id=c.id,
            ordinal=2,
            title="Broken ref",
            content_type=ModuleContentType.scenario,
            content_ref="{oops",
        )
    )
    db_session.add(
        CourseModule(
            id=uuid.uuid4(),
            course_id=c.id,
            ordinal=3,
            title="Not an object",
            content_type=ModuleContentType.scenario,
            content_ref="[1]",
        )
    )
    db_session.flush()
    assert course_scenario_ids(db_session, c) == []


def test_an_exercise_template_whose_scenario_is_gone_keeps_the_reference_unnamed(db_session):
    et = ex_template(db_session)
    et.scenario_id = uuid.uuid4()  # names a scenario this site does not have
    assert dependencies(db_session, "exercise_template", et) == [Dependency("scenario", str(et.scenario_id), None)]


def test_an_exercise_template_lists_its_template_then_its_scenario(db_session):
    t = template(db_session)
    s = scenario(db_session, t)
    deps = dependencies(db_session, "exercise_template", ex_template(db_session, s, t))
    assert [(d.item_type, d.title) for d in deps] == [("range_template", t.name), ("scenario", s.name)]


# -- the certified condition ---------------------------------------------------


def test_certified_from_an_earlier_version_does_not_count(db_session):
    t = template(db_session)
    certify(db_session, t, hash_of="name: an older lab\n")  # certified, but not from this YAML
    cond, builds = template_certification(db_session, t, None)
    assert cond.met is False
    assert any("earlier version" in r for r in cond.reasons)
    assert builds == []


def test_a_newest_range_that_failed_is_quoted_in_the_refusal(db_session):
    t = template(db_session)
    certify(db_session, t, status=sec.FAIL)
    cond, _ = template_certification(db_session, t, None)
    assert cond.met is False
    assert any("Its newest range is" in r for r in cond.reasons)


# -- sizing an exercise template against its range template --------------------


def test_roles_and_rules_the_template_builds_are_no_problem(db_session):
    t = template(db_session)  # TEMPLATE_YAML builds ws01 (ubuntu-2404) and dc01 (srv2022)
    assert sizing_problems(ex_template(db_session), [t]) == []


def test_sizing_problems_name_the_roles_that_point_at_nothing(db_session):
    t = template(db_session)
    et = ex_template(db_session)
    et.roles[0]["vm_profile"][0]["node_type"] = "ghost-os"
    et.scaling_rules[0]["node_type"] = "haiku-os"
    problems = sizing_problems(et, [t])
    assert problems == [
        f"Role Analyst needs node type 'ghost-os', which range template {t.name} does not define",
        f"Scaling rule Sensor needs node type 'haiku-os', which range template {t.name} does not define",
    ]


def test_sizing_problems_reports_at_most_five(db_session):
    et = ex_template(db_session)
    et.roles[0]["vm_profile"] = [{"node_type": f"ghost-{i}"} for i in range(7)]
    assert len(sizing_problems(et, [template(db_session)])) == 5


# -- the used condition of an exercise template --------------------------------


def test_effective_range_template_prefers_the_named_one_then_the_scenario_s(db_session):
    t = template(db_session)
    s = scenario(db_session, t)
    assert effective_range_template(db_session, ex_template(db_session, None, t)).id == t.id
    assert effective_range_template(db_session, ex_template(db_session, s, None)).id == t.id
    assert effective_range_template(db_session, ex_template(db_session)) is None


def test_an_exercise_template_without_a_readable_range_template_was_never_used(db_session):
    et = ex_template(db_session)
    et.range_template_id = uuid.uuid4()  # names a template this site does not have
    cond = exercise_template_use(db_session, et, None)
    assert cond.name == USED and cond.met is False
    assert cond.reasons == ["It names no range template, so no exercise made from it ran on one"]


def test_only_exercises_made_from_the_current_revision_prove_use(db_session):
    t = template(db_session)
    et = ex_template(db_session, None, t)
    made_from(db_session, et)
    proven_use = exercise_template_use(db_session, et, None)
    assert proven_use.met is True and len(proven_use.evidence) == 1
    et.revision = 2  # an edit: the recorded use came from revision 1
    stale = exercise_template_use(db_session, et, None)
    assert stale.met is False
    assert stale.reasons == [
        "No exercise made from its current revision, on a range built from its range template, "
        "has completed with a passing score yet"
    ]


# -- the fresh condition -------------------------------------------------------


def _review_row(db, row, **over) -> ContentReviewItem:
    r = ContentReviewItem(
        id=uuid.uuid4(),
        tenant_id=row.tenant_id,
        item_type="scenario",
        item_id=str(row.id),
        title=row.name,
        theme_ids=[],
        due_at=datetime.now(UTC) + timedelta(days=60),
        **over,
    )
    db.add(r)
    db.flush()
    return r


def test_a_row_never_reviewed_is_fresh_on_the_clock_without_a_reviewed_date(db_session):
    s = scenario(db_session, template(db_session))
    _review_row(db_session, s, last_reviewed_at=None)
    cond = freshness(db_session, "scenario", str(s.id))
    assert cond.met is True
    assert cond.evidence[0].label.startswith("On the content clock, review due ")


def test_a_retired_row_is_not_fresh(db_session):
    s = scenario(db_session, template(db_session))
    review(db_session, "scenario", s, retired=True)
    cond = freshness(db_session, "scenario", str(s.id))
    assert cond.met is False
    assert cond.reasons == ["Retired on the content clock"]


# -- status: the per-item branches ---------------------------------------------


def test_a_scenario_that_names_no_range_template_is_not_certified(db_session):
    s = Scenario(
        id=uuid.uuid4(),
        name="Unattached scenario",
        version="1.0",
        yaml="name: Unattached\nobjectives: []\n",
        tenant_id=DEV,
        is_public=False,
    )
    db_session.add(s)
    db_session.flush()
    cert = status(db_session, "scenario", s, None).conditions[0]
    assert cert.name == CERTIFIED and cert.met is False
    assert "names no range template" in cert.reasons[0]


def test_a_course_that_uses_no_range_has_nothing_to_certify(db_session):
    cert = status(db_session, "course", course(db_session), None).conditions[0]
    assert cert.name == CERTIFIED and cert.met is True and cert.not_applicable is True
    assert cert.reasons == ["It uses no range: there is nothing to certify"]


def test_a_private_dependency_is_redacted_for_another_tenant(db_session):
    other = second_tenant(db_session)
    t = template(db_session, tenant=other.id)
    s = scenario(db_session, t, tenant=other.id)
    st = status(db_session, "scenario", s, None, viewer=DEV)
    assert st.dependencies[0].title == "a range template that is not shared with you"
    assert any("a private item" in r for c in st.conditions for r in c.reasons)
    # The platform administrator (no viewer) reads every name.
    admin_view = status(db_session, "scenario", s, None)
    assert admin_view.dependencies[0].title == t.name
    assert any(t.name in r for c in admin_view.conditions for r in c.reasons)


# -- closure --------------------------------------------------------------------


def test_closure_orders_range_templates_first_and_reports_broken_refs(db_session):
    t = template(db_session)
    s = scenario(db_session, t)
    ghost = scenario(db_session, t, name="Ghost lab")
    ghost.yaml = f"name: Ghost lab\nrange_template: {uuid.uuid4()}\nobjectives: []\n"  # a template that is not here
    db_session.flush()
    items, broken = closure(db_session, [("scenario", s), ("scenario", ghost)])
    assert [kind for kind, _ in items] == ["range_template", "scenario", "scenario"]
    assert items[0][1].id == t.id
    assert [(d.item_type, d.title) for d in broken] == [("range_template", None)]
    assert {str(s.id), str(ghost.id)} == {str(row.id) for kind, row in items if kind == "scenario"}
