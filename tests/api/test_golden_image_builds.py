"""Golden-image builds through the API (ADR 0013 §2): the kit registers, an admin promotes.

The registry serves every tenant, so recording and promoting builds is the PLATFORM
administrator's (an admin of PLATFORM_TENANT_ID's tenant), never another tenant's admin.

* PATCH /golden-images/{id} with ``build`` records a build: idempotent per build id; more
  copies may be added; copies must be of the build's template, named for their host; the
  checksum of a build id never changes; a promoted build never changes.
* POST /golden-images/{id}/builds/{build_id}/promote keeps exactly one promoted build per
  image, refuses a build with no host copy (422) and a concurrent collision (409), and
  writes an audit entry each time.
* GET /golden-images/{id}/builds lists them with the image's audit trail (infra:read), and
  GET /golden-images shows the promoted build.
"""

from __future__ import annotations

import json

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import AuditLog, GoldenImage, UserRole

SUM_A = "a" * 64
SUM_B = "b" * 64


@pytest.fixture
def tenant(db_session, monkeypatch):
    """The operator's own tenant (PLATFORM_TENANT_ID): its admin is the platform administrator."""
    row = real_tenant(db_session, "golden-builds")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(row.id))
    return row


@pytest.fixture
def other_tenant(db_session):
    return real_tenant(db_session, "golden-builds-other")


@pytest.fixture
def image(db_session):
    img = GoldenImage(catalogue_id="ubuntu-2404", hypervisor="vsphere", template_name="tmpl-ubuntu-2404",
                      enabled=True, build_status="built")
    db_session.add(img)
    db_session.commit()
    return img


def _as(db_session, tenant, role):
    act_as(real_user(db_session, role, tenant.id))


def _build(build_id="b1", checksum=SUM_A, hosts=("esx02", "esx03"), **extra):
    return {"build_id": build_id, "checksum": checksum, "build_date": "2026-10-19",
            "iso_checksum": "sha256:" + "c" * 64, "copies": {h: f"tmpl-ubuntu-2404@{h}-20261019" for h in hosts},
            **extra}


def _register(client, image, **kw):
    return client.patch(f"/golden-images/{image.id}", json={"build": _build(**kw)})


def _audit(db_session, image, action):
    db_session.expire_all()
    return db_session.query(AuditLog).filter_by(resource_type="golden_image", resource_id=str(image.id),
                                                action=action).all()


class TestRegister:
    def test_records_a_build_and_lists_it(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        r = _register(client, image)
        assert r.status_code == 200, r.text
        assert r.json()["promoted_build"] is None  # recorded, never promoted, by registering
        got = client.get(f"/golden-images/{image.id}/builds").json()
        assert got["promoted_build_id"] is None
        (b,) = got["builds"]
        assert (b["build_id"], b["checksum"], b["build_date"], b["template_name"]) == (
            "b1", SUM_A, "2026-10-19", "tmpl-ubuntu-2404")
        assert b["copies"] == {"esx02": "tmpl-ubuntu-2404@esx02-20261019", "esx03": "tmpl-ubuntu-2404@esx03-20261019"}
        assert len(_audit(db_session, image, "golden_image.build_register")) == 1

    def test_re_registering_is_idempotent_and_may_add_hosts(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        assert _register(client, image).status_code == 200
        assert _register(client, image).status_code == 200
        assert _register(client, image, hosts=("esx04",)).status_code == 200
        (b,) = client.get(f"/golden-images/{image.id}/builds").json()["builds"]
        assert sorted(b["copies"]) == ["esx02", "esx03", "esx04"]

    def test_a_build_id_never_changes_its_checksum(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        r = _register(client, image, checksum=SUM_B)
        assert r.status_code == 409 and "different build needs a different build id" in r.json()["detail"]

    def test_a_copy_must_be_named_for_its_host(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        r = client.patch(f"/golden-images/{image.id}", json={"build": {
            **_build(), "copies": {"esx02": "tmpl-ubuntu-2404@esx03-20261019"}}})
        assert r.status_code == 409 and "not a copy for host 'esx02'" in r.json()["detail"]

    @pytest.mark.parametrize("bad", [{"checksum": "xyz"}, {"build_id": "has space"},
                                     {"copies": {"esx02": "no-at-sign"}}])
    def test_malformed_builds_are_refused(self, client, db_session, tenant, image, bad):
        _as(db_session, tenant, UserRole.admin)
        assert client.patch(f"/golden-images/{image.id}", json={"build": {**_build(), **bad}}).status_code == 422

    def test_a_student_cannot_register(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.student)
        assert _register(client, image).status_code == 403

    def test_a_copy_of_another_template_is_refused(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        r = client.patch(f"/golden-images/{image.id}", json={"build": {
            **_build(), "copies": {"esx02": "tmpl-kali@esx02-20261019"}}})
        assert r.status_code == 409 and "is a copy of 'tmpl-kali', not of this build's template" in r.json()["detail"]

    @pytest.mark.parametrize("role", [UserRole.range_ops, UserRole.instructor])
    def test_only_the_platform_admin_records_a_build_not_infra_write(self, client, db_session, tenant, image, role):
        _as(db_session, tenant, role)
        r = _register(client, image)
        assert r.status_code == 403
        assert client.get(f"/golden-images/{image.id}/builds").json()["builds"] == [] \
            if role == UserRole.range_ops else True

    def test_range_ops_still_patches_the_image_without_a_build(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.range_ops)
        assert client.patch(f"/golden-images/{image.id}", json={"datastore": "esx02-local"}).status_code == 200

    def test_another_tenants_admin_cannot_record_a_build(self, client, db_session, tenant, other_tenant, image):
        _as(db_session, other_tenant, UserRole.admin)
        assert _register(client, image).status_code == 403
        _as(db_session, tenant, UserRole.admin)
        assert client.get(f"/golden-images/{image.id}/builds").json()["builds"] == []


class TestPromote:
    def _promote(self, client, image, build_id="b1", reason=""):
        return client.post(f"/golden-images/{image.id}/builds/{build_id}/promote", json={"reason": reason})

    @pytest.mark.parametrize("role", [UserRole.range_ops, UserRole.instructor, UserRole.student, UserRole.observer])
    def test_only_an_admin_may_promote(self, client, db_session, tenant, image, role):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        _as(db_session, tenant, role)
        assert self._promote(client, image).status_code == 403
        assert _audit(db_session, image, "golden_image.promote") == []

    def test_another_tenants_admin_may_not_promote(self, client, db_session, tenant, other_tenant, image):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        _as(db_session, other_tenant, UserRole.admin)
        assert self._promote(client, image).status_code == 403
        assert client.get(f"/golden-images/{image.id}/builds").json()["can_promote"] is False
        assert _audit(db_session, image, "golden_image.promote") == []
        _as(db_session, tenant, UserRole.admin)
        assert client.get(f"/golden-images/{image.id}/builds").json()["can_promote"] is True

    def test_a_build_with_no_host_copies_cannot_be_promoted(self, client, db_session, tenant, image):
        """build.sh manifest / a fresh build: recorded before replicate, so not promotable."""
        _as(db_session, tenant, UserRole.admin)
        _register(client, image, hosts=())
        (b,) = client.get(f"/golden-images/{image.id}/builds").json()["builds"]
        assert b["promotable"] is False and "no host copies" in b["not_promotable_reason"]
        r = self._promote(client, image)
        assert r.status_code == 422 and "no host copies" in r.json()["detail"]
        assert _audit(db_session, image, "golden_image.promote") == []
        _register(client, image, hosts=("esx02",))  # replicate added a copy
        assert self._promote(client, image).status_code == 200

    def test_hosts_without_a_copy_are_listed(self, client, db_session, tenant, image):
        from app.models import HypervisorConnection, HypervisorNode

        conn = HypervisorConnection(name="vc", hypervisor_type="vsphere", host="vc", port=443, username="u",
                                    is_active=True)
        db_session.add(conn)
        db_session.flush()
        for n in ("esx02.truenorth.lab", "esx03.truenorth.lab", "esx04.truenorth.lab"):
            db_session.add(HypervisorNode(connection_id=conn.id, node_name=n))
        db_session.commit()
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)  # copies on esx02 and esx03
        (b,) = client.get(f"/golden-images/{image.id}/builds").json()["builds"]
        assert b["missing_hosts"] == ["esx04"]

    def test_a_concurrent_promotion_is_409_never_500(self, client, db_session, tenant, image, monkeypatch):
        from app.golden_image_builds import service
        from sqlalchemy.exc import IntegrityError

        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        real = service.promoted_build

        def collide(db, img):  # another request promoted between our read and our write
            got = real(db, img)
            monkeypatch.setattr(db, "flush", lambda *a, **k: (_ for _ in ()).throw(
                IntegrityError("UPDATE", {}, Exception("uq_golden_image_builds_promoted"))))
            monkeypatch.setattr(db, "rollback", lambda: None)  # the test's shared session stays usable
            return got

        monkeypatch.setattr(service, "promoted_build", collide)
        r = self._promote(client, image)
        assert r.status_code == 409 and "another promotion" in r.json()["detail"]

    def test_promote_is_audited_and_shown(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        admin = real_user(db_session, UserRole.admin, tenant.id)
        act_as(admin)
        r = self._promote(client, image, reason="passed image acceptance")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["promoted_build_id"] == "b1"
        (b,) = body["builds"]
        assert b["promoted"] and b["promoted_by"] == admin.email and b["promoted_at"]
        (entry,) = _audit(db_session, image, "golden_image.promote")
        assert str(entry.user_id) == admin.id
        detail = json.loads(entry.detail)
        assert (detail["build_id"], detail["checksum"], detail["previous_build_id"], detail["reason"]) == (
            "b1", SUM_A, None, "passed image acceptance")
        assert body["audit"][0]["action"] == "golden_image.promote" and body["audit"][0]["actor"] == admin.email
        listed = next(i for i in client.get("/golden-images").json() if i["id"] == str(image.id))
        assert listed["promoted_build"]["build_id"] == "b1"

    def test_promoting_another_build_replaces_the_first_and_says_so(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        _register(client, image, build_id="b2", checksum=SUM_B)
        assert self._promote(client, image, "b1").status_code == 200
        body = self._promote(client, image, "b2").json()
        assert body["promoted_build_id"] == "b2"
        assert [b["build_id"] for b in body["builds"] if b["promoted"]] == ["b2"]
        details = [json.loads(e.detail) for e in _audit(db_session, image, "golden_image.promote")]
        assert sorted((d["build_id"], d["previous_build_id"]) for d in details) == [("b1", None), ("b2", "b1")]

    def test_promoting_the_promoted_build_again_changes_nothing(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        self._promote(client, image)
        assert self._promote(client, image).status_code == 200
        assert len(_audit(db_session, image, "golden_image.promote")) == 1

    def test_a_promoted_build_cannot_change(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        self._promote(client, image)
        r = _register(client, image, hosts=("esx04",))
        assert r.status_code == 409 and "is promoted and cannot change" in r.json()["detail"]
        assert _register(client, image).status_code == 200  # the same registration again is fine

    def test_an_unknown_build_or_image_is_404(self, client, db_session, tenant, image):
        _as(db_session, tenant, UserRole.admin)
        assert self._promote(client, image, "nope").status_code == 404
        assert client.post("/golden-images/00000000-0000-0000-0000-000000000000/builds/b1/promote",
                           json={}).status_code == 404

    def test_promoting_marks_a_not_yet_built_image_built(self, client, db_session, tenant, image):
        image.build_status = "planned"
        db_session.commit()
        _as(db_session, tenant, UserRole.admin)
        _register(client, image)
        self._promote(client, image)
        db_session.expire_all()
        assert db_session.get(GoldenImage, image.id).build_status == "built"
        (entry,) = _audit(db_session, image, "golden_image.promote")
        assert json.loads(entry.detail)["build_status_was"] == "planned"


def test_builds_need_infra_read(client, db_session, tenant, image):
    _as(db_session, tenant, UserRole.student)
    assert client.get(f"/golden-images/{image.id}/builds").status_code == 403
