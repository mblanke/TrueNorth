"""DELETE of a parent row that other rows reference: 409 or the children go, never 500.

PostgreSQL refuses to delete a row a foreign key still points at. The shared test
engine is SQLite without ``PRAGMA foreign_keys``, so these handlers passed their tests
and returned an unhandled 500 in production the first time the parent was in use.

This module runs on its own SQLite engine with foreign keys enforced, so a handler
that forgets a child table fails here the way it would on PostgreSQL. Each site has an
in-use case (409, or the dependents deleted with the parent) and a plain case (204).
The decision per child table and the reason are in each handler's docstring;
``app/delete_guard.py`` explains the rule.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import models as m
from app.course_publishing.models import CoursePublication
from app.course_releases.models import CourseRelease, CourseReleaseBlob
from app.db import Base, get_db
from app.main import app as fastapi_app
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")  # what AUTH_DISABLED signs in as
DEV_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def fk_db():
    """A fresh in-memory database that enforces foreign keys, seeded with the dev
    tenant and user (audit rows and memberships reference them)."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _enforce_fks(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=eng)
    # expire_on_commit=False: the tests read ids off rows the handler has just deleted.
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    db.add(m.Tenant(id=DEV_TENANT, name="Dev", slug="dev"))
    db.add(m.User(id=DEV_USER, keycloak_id="dev-admin", email="admin@truenorth.local", display_name="Dev Admin", tenant_id=DEV_TENANT))
    db.commit()
    yield db
    db.close()
    eng.dispose()


@pytest.fixture
def fk_client(fk_db):
    fastapi_app.dependency_overrides[get_db] = lambda: fk_db
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()


def _add(db, *rows):
    db.add_all(rows)
    db.commit()
    return rows[0]


def _gone(db, model, row_id) -> bool:
    """Asks the database, not the identity map (bulk deletes leave stale objects there)."""
    return db.query(model).filter(model.id == row_id).count() == 0


def _reloaded(db, model, row_id):
    db.expire_all()
    return db.get(model, row_id)


def test_fk_enforcement_is_on(fk_db):
    """Guard for the rest of this module: without it every case below is vacuous."""
    fk_db.add(m.StorageVolume(appliance_id=uuid.uuid4(), volume_name="orphan"))
    with pytest.raises(IntegrityError):
        fk_db.commit()
    fk_db.rollback()


# -- AI backends: fleet nodes and model routes go with the backend -----------------


def test_ai_backend_delete_takes_nodes_and_routes(fk_client, fk_db):
    b = _add(fk_db, m.AIBackendConfig(name="b", backend_type="ollama", base_url="http://x"))
    node = _add(fk_db, m.AIFleetNode(backend_id=b.id, node_name="n", url="http://n"))
    route = _add(fk_db, m.AIModelRoute(model_pattern="qwen*", backend_id=b.id))
    assert fk_client.delete(f"/ai-config/backends/{b.id}").status_code == 204
    assert _gone(fk_db, m.AIBackendConfig, b.id)
    assert _gone(fk_db, m.AIFleetNode, node.id)
    assert _gone(fk_db, m.AIModelRoute, route.id)


def test_ai_backend_delete_plain(fk_client, fk_db):
    b = _add(fk_db, m.AIBackendConfig(name="b", backend_type="ollama", base_url="http://x"))
    assert fk_client.delete(f"/ai-config/backends/{b.id}").status_code == 204


# -- Hypervisor connections: discovered hosts and pools go with the connection ------


def _connection(db):
    return _add(db, m.HypervisorConnection(name="vc", hypervisor_type="vsphere", host="vc", username="u", tenant_id=DEV_TENANT))


def test_hypervisor_connection_delete_takes_nodes_and_pools(fk_client, fk_db):
    c = _connection(fk_db)
    node = _add(fk_db, m.HypervisorNode(connection_id=c.id, node_name="esx1"))
    pool = _add(fk_db, m.HypervisorPool(connection_id=c.id, pool_name="p"))
    assert fk_client.delete(f"/hypervisors/connections/{c.id}").status_code == 204
    assert _gone(fk_db, m.HypervisorConnection, c.id)
    assert _gone(fk_db, m.HypervisorNode, node.id)
    assert _gone(fk_db, m.HypervisorPool, pool.id)


def test_hypervisor_connection_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/hypervisors/connections/{_connection(fk_db).id}").status_code == 204


# -- Storage appliances: 409 while volumes are recorded on them ---------------------


def _appliance(db):
    return _add(db, m.StorageAppliance(name="a", vendor="NetApp", model="x", management_ip="10.0.0.1", tenant_id=DEV_TENANT))


def test_storage_appliance_with_volume_is_409(fk_client, fk_db):
    a = _appliance(fk_db)
    _add(fk_db, m.StorageVolume(appliance_id=a.id, volume_name="v", tenant_id=DEV_TENANT))
    resp = fk_client.delete(f"/storage/appliances/{a.id}")
    assert resp.status_code == 409
    assert "volume" in resp.json()["detail"]
    assert not _gone(fk_db, m.StorageAppliance, a.id)


def test_storage_appliance_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/storage/appliances/{_appliance(fk_db).id}").status_code == 204


# -- Directory: OUs refuse while in use; groups take their memberships --------------


def _ou(db, **kw):
    return _add(db, m.OrganizationalUnit(name=f"ou-{uuid.uuid4().hex[:6]}", slug=uuid.uuid4().hex[:8], tenant_id=DEV_TENANT, **kw))


def _group(db, **kw):
    return _add(db, m.SecurityGroup(name="g", slug=uuid.uuid4().hex[:8], tenant_id=DEV_TENANT, **kw))


@pytest.mark.parametrize("child", ["sub-OU", "security group", "team"])
def test_ou_in_use_is_409(fk_client, fk_db, child):
    ou = _ou(fk_db)
    if child == "sub-OU":
        _ou(fk_db, parent_id=ou.id)
    elif child == "security group":
        _group(fk_db, ou_id=ou.id)
    else:
        _add(fk_db, m.Team(name="t", tenant_id=DEV_TENANT, ou_id=ou.id))
    resp = fk_client.delete(f"/directory/ous/{ou.id}")
    assert resp.status_code == 409
    assert child in resp.json()["detail"]
    assert not _gone(fk_db, m.OrganizationalUnit, ou.id)


def test_ou_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/directory/ous/{_ou(fk_db).id}").status_code == 204


def test_group_delete_takes_memberships(fk_client, fk_db):
    g = _group(fk_db)
    _add(fk_db, m.SecurityGroupMembership(user_id=DEV_USER, group_id=g.id))
    assert fk_client.delete(f"/directory/groups/{g.id}").status_code == 204
    assert _gone(fk_db, m.SecurityGroup, g.id)
    assert fk_db.query(m.SecurityGroupMembership).filter_by(group_id=g.id).count() == 0
    assert fk_db.get(m.User, DEV_USER) is not None  # the person stays


def test_group_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/directory/groups/{_group(fk_db).id}").status_code == 204


# -- Integrations: LTI plumbing goes with the platform; history refuses -------------


def _platform(db):
    return _add(
        db,
        m.ExternalPlatform(
            name="Moodle",
            slug=uuid.uuid4().hex[:8],
            platform_type="moodle",
            base_url="https://moodle.test",
            auth_type=m.IntegrationAuthType.lti13,
            tenant_id=DEV_TENANT,
        ),
    )


def _course(db):
    return _add(db, m.Course(name=f"course-{uuid.uuid4().hex[:6]}", tenant_id=DEV_TENANT))


def _release(db, course):
    sha = uuid.uuid4().hex * 2
    _add(db, CourseReleaseBlob(sha256=sha, size=1, data=b"x"))
    d = uuid.uuid4().hex
    return _add(
        db,
        CourseRelease(
            tenant_id=DEV_TENANT, course_id=course.id, catalogue_code="C", arc2_code="A", run_id="r", slug="s",
            title="t", version=1, release_digest=d, learner_digest=d, platform_digest=d, instructor_digest=d,
            blob_sha256=sha, meta="{}",
        ),
    )


def _publication(db, course, platform):
    rel = _release(db, course)
    return _add(
        db,
        CoursePublication(
            tenant_id=DEV_TENANT, release_id=rel.id, course_id=course.id, platform_id=platform.id,
            stage_idnumber="stage", live_idnumber="live",
        ),
    )


def test_platform_delete_takes_nonces_and_launches(fk_client, fk_db):
    p = _platform(fk_db)
    nonce = _add(fk_db, m.LTINonce(nonce="n1", state="s", platform_id=p.id, expires_at=datetime.now(UTC) + timedelta(minutes=5)))
    launch = _add(fk_db, m.LTILaunch(platform_id=p.id, user_id=DEV_USER, lti_user_sub="sub"))
    assert fk_client.delete(f"/integrations/platforms/{p.id}").status_code == 204
    assert _gone(fk_db, m.ExternalPlatform, p.id)
    assert _gone(fk_db, m.LTINonce, nonce.id)
    assert _gone(fk_db, m.LTILaunch, launch.id)


def test_platform_with_student_activity_is_409(fk_client, fk_db):
    p = _platform(fk_db)
    _add(fk_db, m.ExternalActivity(platform_id=p.id, user_id=DEV_USER, external_ref="r", activity_type="course", title="t"))
    resp = fk_client.delete(f"/integrations/platforms/{p.id}")
    assert resp.status_code == 409
    assert "activity" in resp.json()["detail"]
    assert not _gone(fk_db, m.ExternalPlatform, p.id)


def test_platform_with_publication_is_409(fk_client, fk_db):
    p = _platform(fk_db)
    _publication(fk_db, _course(fk_db), p)
    resp = fk_client.delete(f"/integrations/platforms/{p.id}")
    assert resp.status_code == 409
    assert "publication" in resp.json()["detail"]


def test_platform_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/integrations/platforms/{_platform(fk_db).id}").status_code == 204


# -- Templates: ranges and live reservations refuse; PO map goes, history detaches --


def _template(db):
    return _add(db, m.Template(name=f"tmpl-{uuid.uuid4().hex[:6]}", yaml="vms: []", tenant_id=DEV_TENANT))


def _event(db, template, state):
    start = datetime.now(UTC)
    return _add(
        db,
        m.ScheduledEvent(
            name="ev", state=m.EventState(state), tenant_id=DEV_TENANT, template_id=template.id,
            start_time=start, end_time=start + timedelta(hours=1),
        ),
    )


def test_template_with_range_is_409(fk_client, fk_db):
    t = _template(fk_db)
    _add(fk_db, m.Range(name="r", template_id=t.id, tenant_id=DEV_TENANT))
    resp = fk_client.delete(f"/templates/{t.id}")
    assert resp.status_code == 409
    assert "range" in resp.json()["detail"]
    assert not _gone(fk_db, m.Template, t.id)


def test_template_reserved_by_event_is_409(fk_client, fk_db):
    t = _template(fk_db)
    _event(fk_db, t, "scheduled")
    resp = fk_client.delete(f"/templates/{t.id}")
    assert resp.status_code == 409
    assert "scheduled event" in resp.json()["detail"]


def test_template_delete_takes_po_map_and_detaches_finished_events(fk_client, fk_db):
    t = _template(fk_db)
    q = _add(fk_db, m.Qualification(qsp_code="Q1", nqual="N1", tenant_id=DEV_TENANT))
    po = _add(fk_db, m.PerformanceObjective(qualification_id=q.id, po_code="PO1"))
    rom = _add(fk_db, m.RangeObjectiveMap(template_id=t.id, po_id=po.id, tenant_id=DEV_TENANT))
    ev = _event(fk_db, t, "completed")
    assert fk_client.delete(f"/templates/{t.id}").status_code == 204
    assert _gone(fk_db, m.Template, t.id)
    assert _gone(fk_db, m.RangeObjectiveMap, rom.id)
    kept = _reloaded(fk_db, m.ScheduledEvent, ev.id)
    assert kept is not None and kept.template_id is None
    assert fk_db.get(m.PerformanceObjective, po.id) is not None


def test_template_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/templates/{_template(fk_db).id}").status_code == 204


# -- Courses: Student records and releases refuse; module content goes --------------


def _module(db, course):
    return _add(db, m.CourseModule(course_id=course.id, title="m1", content_type=m.ModuleContentType.reading))


def test_course_with_enrollment_is_409(fk_client, fk_db):
    c = _course(fk_db)
    _module(fk_db, c)
    _add(fk_db, m.Enrollment(user_id=DEV_USER, course_id=c.id, tenant_id=DEV_TENANT))
    resp = fk_client.delete(f"/courses/{c.id}")
    assert resp.status_code == 409
    assert "enrollment" in resp.json()["detail"]
    assert not _gone(fk_db, m.Course, c.id)


def test_course_with_release_is_409(fk_client, fk_db):
    c = _course(fk_db)
    _release(fk_db, c)
    resp = fk_client.delete(f"/courses/{c.id}")
    assert resp.status_code == 409
    assert "release" in resp.json()["detail"]


def test_course_delete_takes_module_content_and_keeps_quizzes(fk_client, fk_db):
    c = _course(fk_db)
    mod = _module(fk_db, c)
    quiz = _add(fk_db, m.Quiz(title="q", module_id=mod.id, tenant_id=DEV_TENANT))
    content = _add(fk_db, m.ModuleContent(module_id=mod.id, quiz_id=quiz.id))
    assert fk_client.delete(f"/courses/{c.id}").status_code == 204
    assert _gone(fk_db, m.Course, c.id)
    assert _gone(fk_db, m.CourseModule, mod.id)
    assert _gone(fk_db, m.ModuleContent, content.id)
    kept = _reloaded(fk_db, m.Quiz, quiz.id)
    assert kept is not None and kept.module_id is None


def test_course_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/courses/{_course(fk_db).id}").status_code == 204


# -- Learning paths: 409 while a registration request names one ---------------------


def _path(db):
    return _add(db, m.LearningPath(name=f"lp-{uuid.uuid4().hex[:6]}", tenant_id=DEV_TENANT))


def test_learning_path_named_by_registration_is_409(fk_client, fk_db):
    lp = _path(fk_db)
    _add(fk_db, m.RegistrationRequest(keycloak_id="kc-1", email="p@x.test", display_name="P", requested_learning_path_id=lp.id))
    resp = fk_client.delete(f"/learning-paths/{lp.id}")
    assert resp.status_code == 409
    assert "registration request" in resp.json()["detail"]
    assert not _gone(fk_db, m.LearningPath, lp.id)


def test_learning_path_delete_plain(fk_client, fk_db):
    assert fk_client.delete(f"/learning-paths/{_path(fk_db).id}").status_code == 204


# -- Backstop: a reference no handler knows about yet is still 409, not 500 ---------


def test_unknown_reference_is_409_not_500(fk_client, fk_db, monkeypatch):
    a = _appliance(fk_db)

    def _fk_violation():
        raise IntegrityError("DELETE FROM storage_appliances", {}, Exception("some_new_table_fkey"))

    monkeypatch.setattr(fk_db, "commit", _fk_violation)
    resp = fk_client.delete(f"/storage/appliances/{a.id}")
    assert resp.status_code == 409
    assert "still referenced" in resp.json()["detail"]
