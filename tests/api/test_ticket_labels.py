"""Ticket categories (a fixed list) and labels (staff-only triage tags). Issue #376.

The reporter picks a category when filing; labels are for staff and must never reach the
reporter, whether as a field, through search, or through the ticket history.
"""

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import User, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _who(role: UserRole, tenant: str = DEV_TENANT) -> CurrentUser:
    return CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )


def _person(db, role: UserRole) -> CurrentUser:
    """A caller with a real users row: this module runs with foreign keys enforced."""
    who = _who(role)
    db.add(
        User(
            id=uuid.UUID(who.id),
            keycloak_id=who.keycloak_id,
            email=who.email,
            display_name=who.display_name,
            role=role,
            tenant_id=uuid.UUID(DEV_TENANT),
        )
    )
    db.commit()
    return who


@contextmanager
def acting_as(who: CurrentUser):
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def student(db_session):
    return _person(db_session, UserRole.student)


@pytest.fixture
def staff(db_session):
    return _person(db_session, UserRole.range_ops)


def _file(client, who, **extra):
    with acting_as(who):
        r = client.post("/tickets", json={"subject": "dc01 won't boot", **extra})
        assert r.status_code == 201, r.text
        return r.json()


class TestCategories:
    def test_the_list_is_served_to_anyone_who_can_file(self, client, student):
        with acting_as(student):
            r = client.get("/tickets/categories")
        assert r.status_code == 200
        keys = [c["key"] for c in r.json()]
        assert "range" in keys and keys[-1] == "other"
        assert all(c["label"] for c in r.json())

    def test_a_reporter_chooses_one_when_filing(self, client, student):
        t = _file(client, student, category="console")
        assert t["category"] == "console"

    def test_default_is_other_and_input_is_normalised(self, client, student):
        assert _file(client, student)["category"] == "other"
        assert _file(client, student, category="  Network ")["category"] == "network"

    def test_unknown_categories_are_refused(self, client, student):
        with acting_as(student):
            r = client.post("/tickets", json={"subject": "x", "category": "whatever"})
        assert r.status_code == 422
        assert "unknown category" in r.text

    def test_staff_can_recategorise_a_reporter_cannot(self, client, student, staff):
        t = _file(client, student, category="other")
        with acting_as(staff):
            r = client.patch(f"/tickets/{t['id']}", json={"category": "account"})
            assert r.status_code == 200 and r.json()["category"] == "account"
        with acting_as(student):
            assert client.patch(f"/tickets/{t['id']}", json={"category": "range"}).status_code == 403

    def test_filter_by_category(self, client, staff):
        _file(client, staff, category="network")
        _file(client, staff, category="scoring")
        with acting_as(staff):
            got = client.get("/tickets", params={"category": "network"}).json()
        assert [t["category"] for t in got] == ["network"]


class TestLabelNormalisation:
    def test_labels_are_lowercased_trimmed_and_deduplicated(self, client, staff):
        t = _file(client, staff, labels=" ESXi-03 , esxi-03,Golden Image,, vm.boot ")
        assert t["labels"] == "esxi-03,golden-image,vm.boot"

    @pytest.mark.parametrize("bad", ["has%percent", "semi;colon", "a" * 31, "-leading"])
    def test_odd_labels_are_refused(self, client, staff, bad):
        with acting_as(staff):
            assert client.post("/tickets", json={"subject": "x", "labels": bad}).status_code == 422

    def test_at_most_ten(self, client, staff):
        with acting_as(staff):
            ok = client.post("/tickets", json={"subject": "x", "labels": ",".join(f"l{i}" for i in range(10))})
            too_many = client.post("/tickets", json={"subject": "x", "labels": ",".join(f"l{i}" for i in range(11))})
        assert ok.status_code == 201 and too_many.status_code == 422

    def test_staff_can_clear_labels(self, client, staff):
        t = _file(client, staff, labels="a,b")
        with acting_as(staff):
            r = client.patch(f"/tickets/{t['id']}", json={"labels": ""})
        assert r.status_code == 200 and r.json()["labels"] == ""


class TestLabelsAreStaffOnly:
    def test_a_reporter_cannot_label_when_filing_or_later(self, client, student, staff):
        with acting_as(student):
            assert client.post("/tickets", json={"subject": "x", "labels": "urgent"}).status_code == 403
        t = _file(client, student)
        with acting_as(student):
            assert client.patch(f"/tickets/{t['id']}", json={"labels": "urgent"}).status_code == 403

    def test_the_reporter_never_sees_labels(self, client, student, staff):
        t = _file(client, student)
        with acting_as(staff):
            client.patch(f"/tickets/{t['id']}", json={"labels": "user-error,sev3"})
            assert client.get(f"/tickets/{t['id']}").json()["labels"] == "user-error,sev3"
        with acting_as(student):
            assert client.get(f"/tickets/{t['id']}").json()["labels"] == ""
            assert [x["labels"] for x in client.get("/tickets").json()] == [""]

    def test_labels_cannot_be_probed_through_search_or_filter(self, client, student, staff):
        t = _file(client, student, subject="VM slow")
        with acting_as(staff):
            client.patch(f"/tickets/{t['id']}", json={"labels": "user-error"})
            assert len(client.get("/tickets", params={"q": "user-error"}).json()) == 1
        with acting_as(student):
            assert client.get("/tickets", params={"q": "user-error"}).json() == []
            assert client.get("/tickets", params={"label": "user-error"}).status_code == 403

    def test_label_history_is_hidden_from_the_reporter(self, client, student, staff):
        t = _file(client, student)
        with acting_as(staff):
            client.patch(f"/tickets/{t['id']}", json={"labels": "user-error", "category": "account"})
            staff_fields = [a["field"] for a in client.get(f"/tickets/{t['id']}/activity").json()]
        with acting_as(student):
            seen = client.get(f"/tickets/{t['id']}/activity").json()
        assert "labels" in staff_fields and "category" in staff_fields
        assert "labels" not in [a["field"] for a in seen]
        assert "user-error" not in str(seen)
        assert "category" in [a["field"] for a in seen]  # the category is not secret

    def test_the_label_list_is_staff_only_and_tenant_scoped(self, client, student, staff):
        _file(client, staff, labels="esxi-03,boot")
        _file(client, staff, labels="boot")
        with acting_as(student):
            assert client.get("/tickets/labels").status_code == 403
        with acting_as(staff):
            assert client.get("/tickets/labels").json() == [
                {"label": "boot", "count": 2},
                {"label": "esxi-03", "count": 1},
            ]
        with acting_as(_who(UserRole.admin, tenant=OTHER_TENANT)):
            assert client.get("/tickets/labels").json() == []


class TestLabelFilter:
    def test_matches_whole_labels_not_substrings(self, client, staff):
        _file(client, staff, subject="a", labels="boot,esxi-03")
        _file(client, staff, subject="b", labels="reboot")
        _file(client, staff, subject="c", labels="esxi-03,boot")
        _file(client, staff, subject="d", labels="bootloader")
        with acting_as(staff):
            got = client.get("/tickets", params={"label": "boot"}).json()
        assert sorted(t["subject"] for t in got) == ["a", "c"]

    def test_wildcards_in_the_filter_are_literal(self, client, staff):
        _file(client, staff, subject="a", labels="boot")
        with acting_as(staff):
            assert client.get("/tickets", params={"label": "%"}).json() == []
            assert client.get("/tickets", params={"label": "_oot"}).json() == []
