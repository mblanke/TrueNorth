"""Staff deep linking: an LMS account becomes a staff account only by an explicit, one-time
link the staff member confirms signed in to TrueNorth (app/lti_identity/links.py).

Threat cases written down here:
- email collision: an LMS asserting a staff email never signs in as that staff member and
  never binds by itself (a resource launch is still 403; a deep-linking launch only gets a
  link request);
- someone else confirming: another staff member (email differs), a Student, an LTI-only
  session, a staff member of another tenant, another browser than the launch's;
- a replayed, expired or unknown link code;
- cross-tenant subject: a link on one tenant's platform says nothing about another's;
- a second link for either side; unlinking by anyone but the holder or a tenant admin.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app import lti13
from app.auth import CurrentUser
from app.lti_identity import links
from app.lti_identity.models import LTIHandoff, LTILinkRequest, LTIUserLink
from app.models import ExternalPlatform, IntegrationAuthType, LTILaunch, User, UserRole
from app.routers.integrations import lti_state_cookie_name

ISSUER = "https://moodle.example.test"
RETURN = f"{ISSUER}/mod/lti/contentitem_return.php"


def _platform(db, tenant_id, issuer=ISSUER, client="client-1") -> ExternalPlatform:
    p = ExternalPlatform(id=uuid.uuid4(), name="Moodle (default)", slug=f"moodle-{uuid.uuid4().hex[:6]}",
                         platform_type="moodle", base_url=issuer, auth_type=IntegrationAuthType.lti13,
                         tenant_id=tenant_id, lti_issuer=issuer, lti_client_id=client, lti_deployment_id="dep-1",
                         lti_auth_login_url=f"{issuer}/mod/lti/auth.php", lti_jwks_url=f"{issuer}/mod/lti/certs.php")
    db.add(p)
    db.flush()
    return p


@pytest.fixture
def tenant(db_session):
    return real_tenant(db_session, f"link-{uuid.uuid4().hex[:6]}")


@pytest.fixture
def platform(db_session, tenant):
    return _platform(db_session, tenant.id)


@pytest.fixture
def staff(db_session, tenant) -> CurrentUser:
    who = real_user(db_session, UserRole.instructor, tenant.id)
    db_session.commit()
    return who


@pytest.fixture
def launch(monkeypatch, client):
    """POST /lti/launch from ``platform`` with these claims, from the browser that began it."""
    box: dict = {}

    async def fake_validate(db, id_token, state):
        return box["platform"], dict(box["claims"])

    monkeypatch.setattr(lti13, "validate_launch", fake_validate)

    def go(platform, *, email, sub="moodle-42", deep=True, name="Ada Lovelace"):
        claims = {"sub": sub, "email": email, "name": name}
        if deep:
            claims[lti13.CLAIM_MESSAGE_TYPE] = "LtiDeepLinkingRequest"
            claims[lti13.CLAIM_DL_SETTINGS] = {"deep_link_return_url": RETURN}
        else:
            claims[lti13.CLAIM_CUSTOM] = {"resource": f"quiz:{uuid.uuid4()}"}
        box.update(platform=platform, claims=claims)
        state = uuid.uuid4().hex
        client.cookies.set(lti_state_cookie_name(state), state)
        return client.post("/lti/launch", data={"id_token": "t", "state": state}, follow_redirects=False)

    return go


def _request(resp) -> tuple[str, str]:
    """(code, browser binding) from the link-request page."""
    assert resp.status_code == 200, resp.text
    assert "Link this learning-platform account" in resp.text
    code = re.search(r"/lti/link#code=([A-Za-z0-9_-]+)", resp.text).group(1)
    cookie = next(c for c in resp.headers.get_list("set-cookie") if c.startswith(f"{links.LINK_COOKIE}="))
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    return code, cookie.split(";", 1)[0].split("=", 1)[1]


def _confirm(client, who: CurrentUser, code: str, bind: str | None, path="/lti/links/confirm"):
    act_as(who)
    client.cookies.delete(links.LINK_COOKIE)
    if bind is not None:
        client.cookies.set(links.LINK_COOKIE, bind)
    return client.post(path, json={"code": code})


class TestEmailNeverBindsByItself:
    def test_a_resource_launch_asserting_a_staff_email_is_still_refused(self, launch, platform, staff, db_session):
        resp = launch(platform, email=staff.email, deep=False)
        assert resp.status_code == 403
        assert db_session.query(LTIUserLink).count() == 0 and db_session.query(LTILinkRequest).count() == 0

    def test_a_deep_linking_launch_gets_a_link_request_not_a_picker(self, launch, platform, staff, db_session):
        resp = launch(platform, email=staff.email)
        _request(resp)
        assert "add activities to your course" not in resp.text  # no content picker yet
        assert db_session.query(LTIUserLink).count() == 0
        row = db_session.query(LTILinkRequest).one()
        assert staff.email not in (row.email_hash, row.lms_name)  # a hash of the email, not the email
        assert resp.headers["cache-control"] == "no-store"


class TestConfirmation:
    def test_the_staff_member_confirms_once_and_then_deep_links_as_themselves(self, client, launch, platform, staff,
                                                                              db_session):
        code, bind = _request(launch(platform, email=staff.email))
        preview = _confirm(client, staff, code, bind, "/lti/links/preview")
        assert preview.status_code == 200 and preview.json()["lms_name"] == "Ada Lovelace"
        resp = _confirm(client, staff, code, bind)
        assert resp.status_code == 201, resp.text
        assert resp.json()["platform_name"] == "Moodle (default)"
        link = db_session.query(LTIUserLink).one()
        assert str(link.user_id) == staff.id and link.lti_sub == "moodle-42"

        picker = launch(platform, email=staff.email)
        assert picker.status_code == 200 and "add activities to your course" in picker.text
        resource = launch(platform, email=staff.email, deep=False)
        assert resource.status_code == 302 and "/lti/session" not in resource.headers["location"]
        assert db_session.query(LTIHandoff).count() == 0  # staff sign in with Keycloak, never a hand-off
        assert str(db_session.query(LTILaunch).one().user_id) == staff.id

    def test_the_link_follows_the_subject_not_the_email(self, client, launch, platform, staff, db_session):
        code, bind = _request(launch(platform, email=staff.email))
        assert _confirm(client, staff, code, bind).status_code == 201
        picker = launch(platform, email="changed-in-moodle@example.test")
        assert "add activities to your course" in picker.text

    def test_a_code_works_once(self, client, launch, platform, staff):
        code, bind = _request(launch(platform, email=staff.email))
        assert _confirm(client, staff, code, bind).status_code == 201
        assert _confirm(client, staff, code, bind).status_code == 404

    def test_another_browser_cannot_confirm(self, client, launch, platform, staff, db_session):
        code, _ = _request(launch(platform, email=staff.email))
        assert _confirm(client, staff, code, None).status_code == 403
        assert _confirm(client, staff, code, "someone-elses-cookie").status_code == 403
        assert db_session.query(LTIUserLink).count() == 0

    def test_an_expired_request_cannot_be_confirmed(self, client, launch, platform, staff, db_session):
        code, bind = _request(launch(platform, email=staff.email))
        db_session.query(LTILinkRequest).update({"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        db_session.commit()
        assert _confirm(client, staff, code, bind).status_code == 410

    def test_without_a_browser_binding_no_staff_link_is_offered(self, launch, platform, staff, db_session,
                                                                 monkeypatch):
        """LTI_REQUIRE_STATE_COOKIE=false (iframe deployments): an unbound code could be
        phished (sent to the staff member to confirm), so staff linking is off there."""
        monkeypatch.setenv("LTI_REQUIRE_STATE_COOKIE", "false")
        assert launch(platform, email=staff.email).status_code == 403
        assert db_session.query(LTILinkRequest).count() == 0

    def test_an_unbound_request_can_never_be_confirmed(self, client, platform, staff, db_session):
        db_session.add(LTILinkRequest(code_hash=links._hash("unbound-code"), bind_hash="", platform_id=platform.id,
                                      lti_sub="m-1", email_hash=links.email_hash(staff.email),
                                      expires_at=datetime.now(UTC) + timedelta(minutes=5)))
        db_session.commit()
        assert _confirm(client, staff, "unbound-code", "").status_code == 403
        assert _confirm(client, staff, "unbound-code", None).status_code == 403
        assert db_session.query(LTIUserLink).count() == 0
        with pytest.raises(ValueError):
            links.request_link(db_session, platform, {"sub": "x", "email": staff.email}, bind="")

    def test_an_unknown_code_is_404(self, client, staff):
        assert _confirm(client, staff, "made-up", "x").status_code == 404


class TestOnlyTheRightPersonCanConfirm:
    def test_another_staff_member_cannot_take_the_link(self, client, launch, platform, staff, db_session, tenant):
        code, bind = _request(launch(platform, email=staff.email))
        colleague = real_user(db_session, UserRole.admin, tenant.id)
        db_session.commit()
        resp = _confirm(client, colleague, code, bind)
        assert resp.status_code == 403 and "different email" in resp.json()["detail"]
        assert db_session.query(LTIUserLink).count() == 0

    def test_a_student_cannot_confirm(self, client, launch, platform, staff, db_session, tenant):
        code, bind = _request(launch(platform, email=staff.email))
        student = real_user(db_session, UserRole.student, tenant.id)
        db_session.commit()
        assert _confirm(client, student, code, bind).status_code == 403

    def test_an_lti_only_account_cannot_confirm(self, client, launch, platform, staff, db_session, tenant):
        code, bind = _request(launch(platform, email=staff.email))
        row = db_session.get(User, uuid.UUID(staff.id))
        row.keycloak_id = f"lti:{platform.id}:x"  # as if this account came only from an LMS
        db_session.commit()
        who = CurrentUser(**{**staff.model_dump(), "keycloak_id": row.keycloak_id})
        assert _confirm(client, who, code, bind).status_code == 403

    def test_staff_of_another_tenant_cannot_confirm(self, client, launch, platform, staff, db_session):
        code, bind = _request(launch(platform, email=staff.email))
        other = real_tenant(db_session, f"other-{uuid.uuid4().hex[:6]}")
        outsider = real_user(db_session, UserRole.admin, other.id)
        db_session.commit()
        assert _confirm(client, outsider, code, bind).status_code == 404


class TestScope:
    def test_a_link_says_nothing_about_another_tenants_platform_with_the_same_subject(
        self, client, launch, platform, staff, db_session
    ):
        code, bind = _request(launch(platform, email=staff.email))
        assert _confirm(client, staff, code, bind).status_code == 201
        theirs = _platform(db_session, real_tenant(db_session, f"b-{uuid.uuid4().hex[:6]}").id,
                           issuer="https://moodle-b.example.test", client="client-b")
        assert links.linked_user(db_session, theirs, "moodle-42") is None
        resp = launch(theirs, email="someone@b.example.test", sub="moodle-42")  # same sub, other site
        assert resp.status_code == 200 and "add activities" in resp.text
        jit = db_session.query(User).filter(User.keycloak_id == f"lti:{theirs.id}:moodle-42").one()
        assert jit.role == UserRole.student and str(jit.id) != staff.id

    def test_either_side_links_once_per_platform(self, client, launch, platform, staff):
        code, bind = _request(launch(platform, email=staff.email))
        assert _confirm(client, staff, code, bind).status_code == 201
        code2, bind2 = _request(launch(platform, email=staff.email, sub="moodle-99"))
        assert _confirm(client, staff, code2, bind2).status_code == 409

    def test_students_are_unchanged(self, launch, platform, db_session, tenant):
        student = real_user(db_session, UserRole.student, tenant.id)
        db_session.commit()
        resp = launch(platform, email=student.email, sub="moodle-7")
        assert resp.status_code == 200 and "add activities" in resp.text
        assert db_session.query(LTILinkRequest).count() == 0


class TestUnlink:
    def _linked(self, client, launch, platform, staff, db_session) -> str:
        code, bind = _request(launch(platform, email=staff.email))
        return _confirm(client, staff, code, bind).json()["id"]

    def test_the_holder_lists_and_removes_their_link(self, client, launch, platform, staff, db_session):
        link_id = self._linked(client, launch, platform, staff, db_session)
        act_as(staff)
        assert [x["id"] for x in client.get("/lti/links").json()] == [link_id]
        assert client.delete(f"/lti/links/{link_id}").status_code == 204
        assert launch(platform, email=staff.email, deep=False).status_code == 403  # back to refused

    def test_someone_else_gets_404_and_a_tenant_admin_may_remove_it(self, client, launch, platform, staff,
                                                                     db_session, tenant):
        link_id = self._linked(client, launch, platform, staff, db_session)
        colleague = real_user(db_session, UserRole.instructor, tenant.id)
        admin = real_user(db_session, UserRole.admin, tenant.id)
        outsider = real_user(db_session, UserRole.admin, real_tenant(db_session, f"o-{uuid.uuid4().hex[:6]}").id)
        db_session.commit()
        for who in (colleague, outsider):
            act_as(who)
            assert client.delete(f"/lti/links/{link_id}").status_code == 404
        act_as(admin)
        assert client.delete(f"/lti/links/{link_id}").status_code == 204

    def test_deregistering_the_platform_removes_its_links(self, client, launch, platform, staff, db_session):
        self._linked(client, launch, platform, staff, db_session)
        db_session.query(LTILaunch).delete()
        db_session.commit()
        act_as(real_user(db_session, UserRole.admin, platform.tenant_id))
        assert client.delete(f"/integrations/platforms/{platform.id}").status_code == 204
        assert db_session.query(LTIUserLink).count() == 0
