"""The content clock's API (ADR 0014 decisions 22-24, issue #286): RBAC per role, tenant
isolation, shared catalogue content, audit, the window list, the stale list, and the
``flag_stale`` hook the feeds (issue #287) will call.

The review rows are made here directly; in production the worker's sync makes them
(tests/worker/test_content_review_sync.py).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.content_review import ContentReviewEdge, ContentReviewItem, ContentReviewTheme, service
from app.main import app as fastapi_app
from app.models import AuditLog, Tenant, User, UserRole

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime.now(UTC)


# ── helpers ──────────────────────────────────────────────────────────────────
def _tenant(db, slug: str) -> uuid.UUID:
    t = Tenant(id=uuid.uuid4(), name=f"T {slug}", slug=slug)
    db.add(t)
    db.flush()
    return t.id


def _user(db, role: UserRole, tenant: uuid.UUID = DEV) -> User:
    u = User(id=uuid.uuid4(), email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test", display_name=role.value,
             role=role, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(db, role: UserRole, tenant: uuid.UUID = DEV, *, user: User | None = None):
    u = user or _user(db, role, tenant)
    who = CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                      tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield u
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _item(db, item_type: str, title: str, tenant: uuid.UUID | None, *, due: datetime | None = None,
          owner: User | None = None, themes: list[str] | None = None) -> ContentReviewItem:
    row = ContentReviewItem(
        id=uuid.uuid4(), tenant_id=tenant, item_type=item_type, item_id=str(uuid.uuid4()), title=title,
        owner_id=owner.id if owner else None, theme_ids=themes or [], due_at=due or NOW + timedelta(days=40),
        stale_reasons=[], created_at=NOW - timedelta(days=50),
    )
    db.add(row)
    db.flush()
    return row


def _edge(db, item: ContentReviewItem, depends_on: ContentReviewItem) -> None:
    db.add(ContentReviewEdge(id=uuid.uuid4(), item_id=item.id, depends_on_id=depends_on.id))
    db.flush()


@pytest.fixture
def world(db_session):
    """A shared golden template, this tenant's range template and course built on it, and
    another tenant's course on the same golden template."""
    db = db_session
    other = _tenant(db, f"other-{uuid.uuid4().hex[:6]}")
    golden = _item(db, "golden_template", "Ubuntu 24.04", None, due=NOW + timedelta(days=30))
    tpl = _item(db, "range_template", "Medium Enterprise", DEV, due=NOW + timedelta(days=10))
    course = _item(db, "course", "C201 Network Defence", DEV, due=NOW + timedelta(days=60))
    theirs = _item(db, "course", "Their course", other, due=NOW + timedelta(days=20))
    _edge(db, tpl, golden)
    _edge(db, course, tpl)
    _edge(db, theirs, golden)
    db.commit()
    return {"db": db, "other": other, "golden": golden, "tpl": tpl, "course": course, "theirs": theirs}


def _window(client, days_before=5, days_after=90, **params):
    start = (NOW - timedelta(days=days_before)).isoformat()
    end = (NOW + timedelta(days=days_after)).isoformat()
    return client.get("/content-review/items", params={"start": start, "end": end, **params})


def _audits(db, action: str) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.action == action).all()


@pytest.fixture
def platform_admin_is_dev(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))


# ── RBAC per role ────────────────────────────────────────────────────────────
READS = ["/content-review/stale", "/content-review/themes", "/content-review/cycles"]


class TestRoles:
    @pytest.mark.parametrize("role", [UserRole.admin, UserRole.instructor, UserRole.range_ops])
    def test_staff_read_everything(self, client, world, role):
        with acting_as(world["db"], role):
            for path in READS:
                assert client.get(path).status_code == 200, path
            assert _window(client).status_code == 200
            assert client.get(f"/content-review/items/{world['course'].id}").status_code == 200

    @pytest.mark.parametrize("role", [UserRole.observer, UserRole.student])
    def test_observers_and_students_have_no_access(self, client, world, role):
        item = world["course"].id
        with acting_as(world["db"], role):
            for path in READS:
                assert client.get(path).status_code == 403, path
            assert _window(client).status_code == 403
            assert client.get(f"/content-review/items/{item}").status_code == 403
            assert client.post(f"/content-review/items/{item}/review", json={}).status_code == 403
            assert client.put(f"/content-review/items/{item}/reviewer", json={"reviewer_id": None}).status_code == 403
            assert client.post(f"/content-review/items/{item}/retire", json={"reason": "x"}).status_code == 403

    def test_range_ops_reads_but_cannot_review(self, client, world):
        with acting_as(world["db"], UserRole.range_ops):
            assert client.post(f"/content-review/items/{world['tpl'].id}/review", json={}).status_code == 403
            assert client.put(f"/content-review/themes/{uuid.uuid4()}",
                              json={"name": "x", "colour": "#000000"}).status_code == 403

    def test_instructor_reviews_only_what_they_own(self, client, world):
        db = world["db"]
        me = _user(db, UserRole.instructor)
        mine = _item(db, "module", "Mine", DEV, owner=me)
        db.commit()
        with acting_as(db, UserRole.instructor, user=me):
            assert client.post(f"/content-review/items/{world['course'].id}/review", json={}).status_code == 403
            assert client.put(f"/content-review/items/{world['course'].id}/reviewer",
                              json={"reviewer_id": str(me.id)}).status_code == 403
            got = client.post(f"/content-review/items/{mine.id}/review", json={})
            assert got.status_code == 200, got.text
            assert got.json()["last_reviewed_by_name"] == "instructor"
            assert got.json()["can_review"] is True and got.json()["can_admin"] is False
            other = _user(db, UserRole.instructor)
            db.commit()
            got = client.put(f"/content-review/items/{mine.id}/reviewer", json={"reviewer_id": str(other.id)})
            assert got.status_code == 200 and got.json()["reviewer_id"] == str(other.id)
            # retiring is an admin's
            assert client.post(f"/content-review/items/{mine.id}/retire", json={"reason": "x"}).status_code == 403

    def test_the_assigned_reviewer_may_mark_it_reviewed(self, client, world):
        db = world["db"]
        reviewer = _user(db, UserRole.instructor)
        world["course"].reviewer_id = reviewer.id
        db.commit()
        with acting_as(db, UserRole.instructor, user=reviewer):
            got = client.get(f"/content-review/items/{world['course'].id}").json()
            assert got["can_review"] is True and got["can_assign"] is False
            assert client.post(f"/content-review/items/{world['course'].id}/review", json={}).status_code == 200

    def test_the_assigned_reviewer_cannot_reassign_it(self, client, world):
        db = world["db"]
        owner, reviewer, other = (_user(db, UserRole.instructor) for _ in range(3))
        world["course"].owner_id, world["course"].reviewer_id = owner.id, reviewer.id
        db.commit()
        with acting_as(db, UserRole.instructor, user=reviewer):
            for target in (str(other.id), None):
                got = client.put(f"/content-review/items/{world['course'].id}/reviewer", json={"reviewer_id": target})
                assert got.status_code == 403
        db.refresh(world["course"])
        assert world["course"].reviewer_id == reviewer.id
        with acting_as(db, UserRole.instructor, user=owner):
            assert client.get(f"/content-review/items/{world['course'].id}").json()["can_assign"] is True
            got = client.put(f"/content-review/items/{world['course'].id}/reviewer", json={"reviewer_id": str(other.id)})
            assert got.status_code == 200

    def test_instructor_cannot_review_shared_content_even_as_owner(self, client, world):
        db = world["db"]
        me = _user(db, UserRole.instructor)
        world["golden"].owner_id = me.id
        db.commit()
        with acting_as(db, UserRole.instructor, user=me):
            got = client.post(f"/content-review/items/{world['golden'].id}/review", json={})
            assert got.status_code == 403 and "platform administrator" in got.json()["detail"]

    def test_tenant_admin_does_everything_to_the_tenants_content(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            item = world["tpl"].id
            assert client.post(f"/content-review/items/{item}/review", json={"note": "checked"}).status_code == 200
            assert client.post(f"/content-review/items/{item}/retire", json={"reason": "obsolete"}).status_code == 200

    def test_only_a_platform_admin_changes_shared_content(self, client, world, monkeypatch):
        golden = world["golden"].id
        with acting_as(world["db"], UserRole.admin):  # two tenants, PLATFORM_TENANT_ID unset: nobody is
            monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
            assert client.post(f"/content-review/items/{golden}/review", json={}).status_code == 403
            assert client.post(f"/content-review/items/{golden}/retire", json={"reason": "x"}).status_code == 403
            assert client.get(f"/content-review/items/{golden}").json()["can_admin"] is False
            monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
            assert client.post(f"/content-review/items/{golden}/review", json={}).status_code == 200

    def test_another_tenants_platform_admin_setting_does_not_help(self, client, world, monkeypatch):
        monkeypatch.setenv("PLATFORM_TENANT_ID", str(world["other"]))
        with acting_as(world["db"], UserRole.admin):
            assert client.post(f"/content-review/items/{world['golden'].id}/review", json={}).status_code == 403


# ── tenant isolation ─────────────────────────────────────────────────────────
class TestTenantIsolation:
    def test_list_has_own_and_shared_never_another_tenants(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            titles = {i["title"] for i in _window(client).json()["items"]}
        assert titles == {"Ubuntu 24.04", "Medium Enterprise", "C201 Network Defence"}

    def test_another_tenants_item_is_not_found_for_every_verb(self, client, world):
        theirs = world["theirs"].id
        with acting_as(world["db"], UserRole.admin):
            assert client.get(f"/content-review/items/{theirs}").status_code == 404
            assert client.post(f"/content-review/items/{theirs}/review", json={}).status_code == 404
            assert client.put(f"/content-review/items/{theirs}/reviewer", json={"reviewer_id": None}).status_code == 404
            assert client.post(f"/content-review/items/{theirs}/retire", json={"reason": "x"}).status_code == 404
            assert client.patch(f"/content-review/items/{theirs}", json={"cycle_days": 100}).status_code == 404

    def test_shared_items_used_by_lists_only_the_callers_content(self, client, world):
        with acting_as(world["db"], UserRole.instructor):
            got = client.get(f"/content-review/items/{world['golden'].id}").json()
        assert [u["title"] for u in got["used_by"]] == ["Medium Enterprise"]
        assert got["shared"] is True

    def test_a_shared_items_people_from_another_tenant_are_not_shown(self, client, world):
        db = world["db"]
        theirs = _user(db, UserRole.instructor, world["other"])
        world["golden"].reviewer_id = theirs.id
        world["golden"].owner_id = theirs.id
        db.commit()
        with acting_as(db, UserRole.admin):
            got = client.get(f"/content-review/items/{world['golden'].id}").json()
        assert got["reviewer_id"] is None and got["reviewer_name"] is None
        assert got["owner_id"] is None and got["owner_name"] is None
        with acting_as(db, UserRole.admin, world["other"]):  # their own tenant sees them
            got = client.get(f"/content-review/items/{world['golden'].id}").json()
        assert got["reviewer_id"] == str(theirs.id)

    def test_depends_on_and_used_by(self, client, world):
        with acting_as(world["db"], UserRole.instructor):
            got = client.get(f"/content-review/items/{world['tpl'].id}").json()
        assert [d["title"] for d in got["depends_on"]] == ["Ubuntu 24.04"]
        assert [u["title"] for u in got["used_by"]] == ["C201 Network Defence"]

    def test_a_reviewer_from_another_tenant_is_not_found(self, client, world):
        db = world["db"]
        stranger = _user(db, UserRole.instructor, world["other"])
        db.commit()
        with acting_as(db, UserRole.admin):
            got = client.put(f"/content-review/items/{world['tpl'].id}/reviewer", json={"reviewer_id": str(stranger.id)})
        assert got.status_code == 404

    def test_a_student_cannot_be_the_reviewer(self, client, world):
        db = world["db"]
        student = _user(db, UserRole.student)
        db.commit()
        with acting_as(db, UserRole.admin):
            got = client.put(f"/content-review/items/{world['tpl'].id}/reviewer", json={"reviewer_id": str(student.id)})
        assert got.status_code == 422

    def test_the_stale_list_never_shows_another_tenants_content(self, client, world):
        db = world["db"]
        service.flag_stale(db, world["golden"], "New Ubuntu 24.04 point release", "feed:test")
        db.commit()
        db.refresh(world["theirs"])
        assert world["theirs"].stale  # it is stale in its own tenant ...
        with acting_as(db, UserRole.admin):
            titles = [i["title"] for i in client.get("/content-review/stale").json()]
        assert sorted(titles) == ["C201 Network Defence", "Medium Enterprise", "Ubuntu 24.04"]  # ... not here


# ── the flag_stale hook and Mark reviewed, through the API ──────────────────
class TestStaleness:
    def test_flagging_the_golden_template_reaches_the_course(self, client, world):
        db = world["db"]
        service.flag_stale(db, world["golden"], "New Ubuntu 24.04 point release", "feed:test")
        db.commit()
        with acting_as(db, UserRole.instructor):
            course = client.get(f"/content-review/items/{world['course'].id}").json()
        assert course["stale"] is True and course["clock"]["state"] == "stale"
        assert course["stale_reasons"][0]["text"] == \
            "Its golden template Ubuntu 24.04 is stale (through range template Medium Enterprise)"
        assert course["stale_reasons"][0]["origin_id"] == str(world["golden"].id)

    def test_reviewing_the_template_keeps_the_inherited_reason(self, client, world, platform_admin_is_dev):
        db = world["db"]
        service.flag_stale(db, world["golden"], "release", "feed:a")
        service.flag_stale(db, world["tpl"], "KEV", "kev:1", kind="vulnerability")
        db.commit()
        with acting_as(db, UserRole.admin):
            tpl = client.post(f"/content-review/items/{world['tpl'].id}/review", json={}).json()
            assert [r["kind"] for r in tpl["stale_reasons"]] == ["upstream"]
            course = client.get(f"/content-review/items/{world['course'].id}").json()
            assert [r["origin_id"] for r in course["stale_reasons"]] == [str(world["golden"].id)]
            # the golden template's review clears everything it caused
            client.post(f"/content-review/items/{world['golden'].id}/review", json={})
            assert client.get("/content-review/stale").json() == []
        db.refresh(world["theirs"])
        assert not world["theirs"].stale

    def test_mark_reviewed_resets_the_clock(self, client, world):
        db = world["db"]
        world["course"].due_at = NOW - timedelta(days=3)
        world["course"].stale_reasons = [{"origin": str(world["course"].id), "kind": "time",
                                          "text": "Review overdue by 3 days", "source": "clock", "at": NOW.isoformat()}]
        world["course"].stale = True
        db.commit()
        with acting_as(db, UserRole.admin):
            got = client.post(f"/content-review/items/{world['course'].id}/review", json={}).json()
        assert got["stale"] is False and got["clock"]["state"] == "green"
        due = datetime.fromisoformat(got["due_at"])
        due = due if due.tzinfo else due.replace(tzinfo=UTC)
        assert abs((due - NOW) - timedelta(days=90)) < timedelta(minutes=5)

    def test_two_flags_in_one_transaction_both_stick(self, world):
        """A feed run flags many items before it commits; each flag re-reads the rows locked."""
        db = world["db"]
        service.flag_stale(db, world["golden"], "release", "feed:a")
        service.flag_stale(db, world["tpl"], "KEV", "kev:1", kind="vulnerability")
        db.commit()
        db.refresh(world["course"])
        assert sorted(r["origin"] for r in world["course"].stale_reasons) == sorted(
            [str(world["golden"].id), str(world["tpl"].id)])

    def test_retiring_a_flagged_item_clears_what_it_caused(self, client, world):
        db = world["db"]
        service.flag_stale(db, world["tpl"], "KEV", "kev:1", kind="vulnerability")
        db.commit()
        db.refresh(world["course"])
        assert world["course"].stale
        with acting_as(db, UserRole.admin):
            assert client.post(f"/content-review/items/{world['tpl'].id}/retire",
                               json={"reason": "replaced"}).status_code == 200
            course = client.get(f"/content-review/items/{world['course'].id}").json()
        assert course["stale"] is False and course["stale_reasons"] == []
        # and a later flag on it reaches nothing either
        service.flag_stale(db, world["tpl"], "KEV", "kev:2", kind="vulnerability")
        db.commit()
        db.refresh(world["course"])
        assert not world["course"].stale

    def test_mark_reviewed_loads_and_locks_only_what_is_downstream(self, client, world, monkeypatch):
        """Not every review row of every tenant: the item and what depends on it, in id order."""
        db = world["db"]
        locked: list[list[str]] = []
        real = service.lock_rows
        monkeypatch.setattr(service, "lock_rows", lambda d, ids: locked.append(sorted(ids)) or real(d, ids))
        tpl, course, theirs = (str(world[k].id) for k in ("tpl", "course", "theirs"))
        with acting_as(db, UserRole.admin):
            db.expunge_all()  # nothing loaded before the request
            assert client.post(f"/content-review/items/{tpl}/review", json={}).status_code == 200
        assert locked == [sorted([tpl, course])]
        loaded = {str(o.id) for o in db.identity_map.values() if isinstance(o, ContentReviewItem)}
        assert theirs not in loaded  # another tenant's row is never read

    def test_flag_stale_locks_only_what_is_downstream(self, world, monkeypatch):
        db = world["db"]
        locked: list[list[str]] = []
        real = service.lock_rows
        monkeypatch.setattr(service, "lock_rows", lambda d, ids: locked.append(sorted(ids)) or real(d, ids))
        service.flag_stale(db, world["tpl"], "KEV", "kev:1", kind="vulnerability")
        assert locked == [sorted([str(world["tpl"].id), str(world["course"].id)])]

    def test_the_lock_is_a_row_lock_in_id_order_on_postgresql(self, db_session):
        from sqlalchemy.dialects import postgresql

        sql = str(service.lock_query(db_session, [str(uuid.uuid4())]).statement.compile(dialect=postgresql.dialect()))
        # NO KEY UPDATE: a foreign key check (the sync inserting an edge) does not block it.
        assert "FOR NO KEY UPDATE" in sql and "ORDER BY content_review_items.id" in sql

    def test_flag_stale_refuses_an_unknown_item(self, db_session):
        with pytest.raises(KeyError):
            service.flag_stale(db_session, uuid.uuid4(), "x", "y")

    def test_retired_content_leaves_the_view_and_cannot_be_reviewed(self, client, world):
        db = world["db"]
        service.flag_stale(db, world["tpl"], "KEV", "kev:1", kind="vulnerability")
        db.commit()
        with acting_as(db, UserRole.admin):
            assert client.post(f"/content-review/items/{world['tpl'].id}/retire",
                               json={"reason": "replaced"}).status_code == 200
            assert "Medium Enterprise" not in {i["title"] for i in _window(client).json()["items"]}
            assert "Medium Enterprise" not in {i["title"] for i in client.get("/content-review/stale").json()}
            assert "Medium Enterprise" in {i["title"] for i in _window(client, include_retired=True).json()["items"]}
            assert client.post(f"/content-review/items/{world['tpl'].id}/review", json={}).status_code == 409
            assert client.post(f"/content-review/items/{world['tpl'].id}/retire",
                               json={"reason": "again"}).status_code == 409


# ── audit ────────────────────────────────────────────────────────────────────
class TestAudit:
    def test_review_assign_and_retire_are_audited(self, client, world):
        db = world["db"]
        reviewer = _user(db, UserRole.instructor)
        db.commit()
        item = world["tpl"].id
        with acting_as(db, UserRole.admin) as admin:
            client.post(f"/content-review/items/{item}/review", json={"note": "looked at the VMs"})
            client.put(f"/content-review/items/{item}/reviewer", json={"reviewer_id": str(reviewer.id)})
            client.post(f"/content-review/items/{item}/retire", json={"reason": "superseded"})
        reviewed, assigned, retired = (_audits(db, a) for a in (
            "content_review.mark_reviewed", "content_review.assign_reviewer", "content_review.retire"))
        assert len(reviewed) == len(assigned) == len(retired) == 1
        for row in (reviewed[0], assigned[0], retired[0]):
            assert row.resource_type == "content_review_item" and row.resource_id == str(item)
            assert row.user_id == admin.id and row.tenant_id == DEV
        assert "looked at the VMs" in reviewed[0].detail and "Medium Enterprise" in reviewed[0].detail
        assert str(reviewer.id) in assigned[0].detail
        assert "superseded" in retired[0].detail

    def test_a_refused_write_is_not_audited(self, client, world):
        with acting_as(world["db"], UserRole.instructor):
            client.post(f"/content-review/items/{world['tpl'].id}/review", json={})
        assert _audits(world["db"], "content_review.mark_reviewed") == []


# ── the window list and theme chips ──────────────────────────────────────────
class TestWindow:
    def test_items_sit_on_their_due_dates_inside_the_window_only(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            got = _window(client, days_before=0, days_after=35).json()
        assert [i["title"] for i in got["items"]] == ["Medium Enterprise", "Ubuntu 24.04"]  # due +10, +30
        assert got["items"][0]["clock"]["state"] == "amber"
        assert got["items"][1]["clock"]["state"] == "green"

    def test_theme_filter_and_counts(self, client, world):
        db = world["db"]
        blue = ContentReviewTheme(id=uuid.uuid4(), tenant_id=None, name="Blue team", colour="#3B5BDB")
        db.add(blue)
        world["course"].theme_ids = [str(blue.id)]
        db.commit()
        with acting_as(db, UserRole.admin):
            got = _window(client).json()
            assert got["theme_counts"] == {str(blue.id): 1, "none": 2}
            only = _window(client, theme_id=[str(blue.id)]).json()
            assert [i["title"] for i in only["items"]] == ["C201 Network Defence"]
            assert only["theme_counts"] == got["theme_counts"]
            untagged = _window(client, theme_id=["none"]).json()
            assert {i["title"] for i in untagged["items"]} == {"Ubuntu 24.04", "Medium Enterprise"}

    def test_the_stale_list_filters_by_theme_before_its_limit(self, client, world):
        db = world["db"]
        blue = ContentReviewTheme(id=uuid.uuid4(), tenant_id=None, name="Blue team", colour="#3B5BDB")
        db.add(blue)
        for i in range(3):  # due earlier, untagged
            _item(db, "module", f"Old {i}", DEV, due=NOW - timedelta(days=10 + i)).stale = True
        tagged = _item(db, "module", "Blue one", DEV, due=NOW + timedelta(days=5), themes=[str(blue.id)])
        tagged.stale = True
        db.commit()
        with acting_as(db, UserRole.admin):
            got = client.get("/content-review/stale", params={"theme_id": str(blue.id), "limit": 1}).json()
        assert [i["title"] for i in got] == ["Blue one"]

    def test_a_backwards_or_huge_window_is_refused(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            assert _window(client, days_before=-5, days_after=-10).status_code == 422
            assert _window(client, days_before=300, days_after=300).status_code == 422


# ── themes and cycles (admin) ────────────────────────────────────────────────
class TestThemesAndCycles:
    def test_the_default_themes_are_seeded_once_when_the_schema_comes_from_create_all(self, db_session):
        """DB_AUTO_CREATE builds the tables without the migration, so startup seeds them too."""
        import inspect

        from app import main

        db = db_session
        assert db.query(ContentReviewTheme).count() == 0
        service.seed_default_themes(db)
        service.seed_default_themes(db)
        names = [t.name for t in db.query(ContentReviewTheme).order_by(ContentReviewTheme.ordinal)]
        assert names == ["Blue team", "Red team", "DFIR", "Network", "ICS / OT", "Cloud", "Malware", "Platform"]
        assert all(t.tenant_id is None for t in db.query(ContentReviewTheme))
        assert "seed_default_themes" in inspect.getsource(main._seed_dev_data)

    def test_seeding_leaves_an_admins_themes_alone(self, db_session):
        db = db_session
        db.add(ContentReviewTheme(id=uuid.uuid4(), tenant_id=None, name="Only this", colour="#000000"))
        db.flush()
        service.seed_default_themes(db)
        assert [t.name for t in db.query(ContentReviewTheme)] == ["Only this"]

    def test_an_admin_who_deletes_every_default_theme_does_not_get_them_back(self, db_session):
        """Seeded once, marked as done: an empty theme list is the admin's choice, not a fresh install."""
        db = db_session
        assert service.seed_default_themes(db) == 8
        db.query(ContentReviewTheme).delete()
        db.flush()
        assert service.seed_default_themes(db) == 0
        assert db.query(ContentReviewTheme).count() == 0

    def test_a_failing_theme_seed_does_not_stop_the_other_seeds(self):
        """It has its own try block in main._seed_dev_data."""
        import inspect

        from app import main

        src = inspect.getsource(main._seed_dev_data)
        seeds = src.index("seed_ai_backends(db)")
        themes = src.index("seed_default_themes(db)")
        assert src.count("try:") >= 2 and src.rfind("try:", 0, themes) > seeds

    def test_tenant_admin_manages_tenant_themes_not_platform_ones(self, client, world, monkeypatch):
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        with acting_as(world["db"], UserRole.admin):
            got = client.post("/content-review/themes", json={"name": "Purple team", "colour": "#7950f2"})
            assert got.status_code == 201 and got.json()["colour"] == "#7950F2"
            assert got.json()["platform_wide"] is False
            assert client.post("/content-review/themes", json={"name": "Purple team",
                                                                "colour": "#000000"}).status_code == 409
            assert client.post("/content-review/themes", json={"name": "All", "colour": "#000000",
                                                                "platform_wide": True}).status_code == 403
            tid = got.json()["id"]
            assert client.put(f"/content-review/themes/{tid}", json={"name": "Purple", "colour": "#111111"}).json()[
                "name"] == "Purple"
            assert client.post("/content-review/themes", json={"name": "x", "colour": "blue"}).status_code == 422

    def test_deleting_a_theme_untags_items(self, client, world):
        db = world["db"]
        with acting_as(db, UserRole.admin):
            tid = client.post("/content-review/themes", json={"name": "DFIR", "colour": "#7950F2"}).json()["id"]
            got = client.patch(f"/content-review/items/{world['course'].id}", json={"theme_ids": [tid]})
            assert got.status_code == 200 and got.json()["theme_ids"] == [tid]
            assert client.delete(f"/content-review/themes/{tid}").status_code == 204
            assert client.get(f"/content-review/items/{world['course'].id}").json()["theme_ids"] == []
        assert len(_audits(db, "content_review.theme_delete")) == 1

    def test_shared_content_takes_only_platform_wide_themes(self, client, world, platform_admin_is_dev):
        db = world["db"]
        shared = ContentReviewTheme(id=uuid.uuid4(), tenant_id=None, name="Platform", colour="#6B7280")
        db.add(shared)
        db.commit()
        with acting_as(db, UserRole.admin):
            mine = client.post("/content-review/themes", json={"name": "Ours", "colour": "#000000"}).json()["id"]
            golden = world["golden"].id
            assert client.patch(f"/content-review/items/{golden}", json={"theme_ids": [mine]}).status_code == 422
            got = client.patch(f"/content-review/items/{golden}", json={"theme_ids": [str(shared.id)]})
            assert got.status_code == 200 and got.json()["theme_ids"] == [str(shared.id)]

    def test_an_unknown_theme_is_refused_on_an_item(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            got = client.patch(f"/content-review/items/{world['course'].id}", json={"theme_ids": [str(uuid.uuid4())]})
        assert got.status_code == 422

    def test_another_tenants_theme_is_not_found(self, client, world):
        db = world["db"]
        theirs = ContentReviewTheme(id=uuid.uuid4(), tenant_id=world["other"], name="Theirs", colour="#000000")
        db.add(theirs)
        db.commit()
        with acting_as(db, UserRole.admin):
            assert client.put(f"/content-review/themes/{theirs.id}",
                              json={"name": "Mine", "colour": "#000000"}).status_code == 404
            assert client.delete(f"/content-review/themes/{theirs.id}").status_code == 404
            assert "Theirs" not in {t["name"] for t in client.get("/content-review/themes").json()}

    def test_cycles_default_to_90_and_take_90_to_120(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            cycles = {c["item_type"]: c for c in client.get("/content-review/cycles").json()}
            assert cycles["course"] == {"item_type": "course", "days": 90, "source": "default"}
            assert set(cycles) == {"course", "module", "lab", "scenario", "range_template",
                              "exercise_template", "golden_template"}
            for bad in (89, 121, 30, 365):
                assert client.put("/content-review/cycles/course", json={"days": bad}).status_code == 422
            got = client.put("/content-review/cycles/course", json={"days": 120})
            assert got.status_code == 200 and got.json() == {"item_type": "course", "days": 120, "source": "tenant"}
            assert client.put("/content-review/cycles/nonsense", json={"days": 100}).status_code == 404

    def test_a_cycle_change_moves_due_dates_at_once(self, client, world):
        db = world["db"]
        world["course"].last_reviewed_at = NOW - timedelta(days=100)
        db.commit()
        with acting_as(db, UserRole.admin):
            before = client.get(f"/content-review/items/{world['course'].id}").json()
            client.put("/content-review/cycles/course", json={"days": 120})
            after = client.get(f"/content-review/items/{world['course'].id}").json()
        assert before["cycle_days"] == 90 and after["cycle_days"] == 120
        assert after["clock"]["state"] == "green" and after["clock"]["days"] in (19, 20)

    def test_platform_wide_cycle_needs_the_platform_admin(self, client, world, monkeypatch):
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        with acting_as(world["db"], UserRole.admin):
            assert client.put("/content-review/cycles/golden_template",
                              json={"days": 100, "platform_wide": True}).status_code == 403
            monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
            got = client.put("/content-review/cycles/golden_template", json={"days": 100, "platform_wide": True})
            assert got.status_code == 200 and got.json()["source"] == "platform"

    def test_item_cycle_override_is_validated(self, client, world):
        with acting_as(world["db"], UserRole.admin):
            assert client.patch(f"/content-review/items/{world['tpl'].id}", json={"cycle_days": 60}).status_code == 422
            got = client.patch(f"/content-review/items/{world['tpl'].id}", json={"cycle_days": 110}).json()
            assert got["cycle_days"] == 110 and got["cycle_override"] == 110
            got = client.patch(f"/content-review/items/{world['tpl'].id}", json={"clear_cycle": True}).json()
            assert got["cycle_days"] == 90 and got["cycle_override"] is None

    def test_instructor_cannot_change_themes_or_cycles(self, client, world):
        with acting_as(world["db"], UserRole.instructor):
            assert client.post("/content-review/themes", json={"name": "x", "colour": "#000000"}).status_code == 403
            assert client.put("/content-review/cycles/course", json={"days": 100}).status_code == 403
            assert client.patch(f"/content-review/items/{world['tpl'].id}", json={"cycle_days": 100}).status_code == 403
