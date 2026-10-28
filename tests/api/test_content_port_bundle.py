"""The content port's export and import (app/content_port; docs/content-port.md).

Export: the platform administrator only; only proven content, with everything it needs
(which must be proven too), signed under the content port's own key and context; no
Student data, credentials or tenant data. Import: the signature is checked against the
configured trusted keys first (fail closed); a preview shows what would change; items land
not yet proven; a replay is refused; a conflict (changed here, a same-named item here, an
older export) is overwritten only when confirmed.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _content_port_kit import (
    DEV,
    acting_as,
    b64_public,
    course,
    golden_build,
    prove_template,
    proven_set,
    scenario,
    template,
)
from app.content_feeds import bundle_signing as feed_signing
from app.content_port import ContentPortImport, ContentPortItem, signing
from app.content_port import bundle as bnd
from app.models import AuditLog, Course, ModuleContent, Scenario, Template, Tenant, UserRole
from app.range_certification import signing as cert_signing
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

OTHER_KEY = Ed25519PrivateKey.generate()


@pytest.fixture
def port(monkeypatch):
    """This site trusts its own content-port key (the tests play both sites) and is the
    platform tenant."""
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
    monkeypatch.setenv(signing.TRUSTED_ENV, signing.public_info()["public_key"])


@pytest.fixture
def new_site(db_session):
    """The tenant an import lands in, standing in for the new site."""
    t = Tenant(id=uuid.uuid4(), name="New site", slug=f"new-{uuid.uuid4().hex[:6]}")
    db_session.add(t)
    db_session.flush()
    return t.id


def export(client, *refs):
    return client.post("/content-port/export",
                       json={"items": [{"item_type": t, "item_id": str(r.id)} for t, r in refs]})


def upload(client, doc, path="preview", **form):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    data = {k: (str(v) if not isinstance(v, list) else v) for k, v in form.items()}
    return client.post(f"/content-port/{path}", files={"file": ("bundle.json", raw, "application/json")}, data=data)


def resign(doc: dict, mutate=None, *, key=None, context=signing.CONTEXT, rehash=True) -> dict:
    """``doc`` changed by ``mutate`` and signed again (by this site's key unless ``key``)."""
    body = copy.deepcopy({k: v for k, v in doc.items() if k != "signature"})
    if mutate:
        mutate(body)
    if rehash:
        for i in body.get("items", []):
            if isinstance(i, dict) and isinstance(i.get("content"), dict):
                i["content_sha256"] = bnd.sha256(i["content"])
    return feed_signing.sign_for(context, body, key or signing._signing_key())


def _exported(client, db_session, *refs) -> dict:
    with acting_as(db_session, UserRole.admin):
        res = export(client, *refs)
    assert res.status_code == 200, res.text
    return res.json()


def _item(doc, item_type):
    return next(i for i in doc["items"] if i["item_type"] == item_type)


# ── permissions ──────────────────────────────────────────────────────────────────
class TestPermissions:
    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops, UserRole.observer, UserRole.student])
    def test_only_the_platform_admin_ports(self, client, db_session, port, role):
        _t, _s, c = proven_set(db_session)
        with acting_as(db_session, role):
            assert export(client, ("course", c)).status_code == 403
            assert upload(client, b"{}").status_code == 403
            assert upload(client, b"{}", "import").status_code == 403
            assert client.get("/content-port/items").status_code == 403
            assert client.get("/content-port/signing-key").status_code == 403

    def test_a_tenant_admin_who_is_not_the_platform_admin_cannot(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin, new_site):
            assert export(client, ("course", c)).status_code == 403
            assert upload(client, doc).status_code == 403
            assert upload(client, doc, "import").status_code == 403
            assert client.get("/content-port/items").status_code == 403

    def test_the_platform_admin_lists_portable_content_and_the_signing_key(self, client, db_session, port):
        t, _s, _c = proven_set(db_session)
        with acting_as(db_session, UserRole.admin):
            items = client.get("/content-port/items").json()
            key = client.get("/content-port/signing-key").json()
        assert {(i["item_type"], i["item_id"], i["proven"]) for i in items} >= {("range_template", str(t.id), True)}
        assert key["key_id"] == cert_signing.key_id(signing._signing_key().public_key())
        assert key["public_key"] == b64_public(signing._signing_key())


# ── export ───────────────────────────────────────────────────────────────────────
class TestExport:
    def test_a_course_pulls_in_its_range_template_and_scenario(self, client, db_session, port):
        t, s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        assert [i["key"] for i in doc["items"]] == [f"range_template:{t.id}", f"scenario:{s.id}", f"course:{c.id}"]
        assert _item(doc, "course")["depends_on"] == sorted([f"range_template:{t.id}", f"scenario:{s.id}"])
        assert _item(doc, "scenario")["depends_on"] == [f"range_template:{t.id}"]
        assert _item(doc, "scenario")["content"]["range_template"] == f"range_template:{t.id}"
        assert all(i["proof"]["proven"] for i in doc["items"])
        assert doc["golden_builds"] == [{"catalogue_id": "ubuntu-2404", "build_id": "b-2026-10-01",
                                         "checksum": "a" * 64}]
        assert _item(doc, "range_template")["golden_builds"] == doc["golden_builds"]

    def test_it_is_signed_under_the_content_port_context_and_key(self, client, db_session, port):
        _t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        assert signing.verify(doc) == doc["origin"]["key_id"]
        assert doc["signature"]["key_id"] == doc["origin"]["key_id"]
        # not the range certification export's key or construction
        assert doc["signature"]["key_id"] != cert_signing.current_public()["key_id"]
        assert not cert_signing.verify(doc)

    def test_no_student_data_credentials_or_tenant_data(self, client, db_session, port):
        _t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        text = json.dumps(doc)
        assert "Winter2024!" not in text  # the scenario password, in the account and in the notes
        assert "tnsec:" not in text
        assert str(DEV) not in text
        assert "@x.test" not in text and "@example.test" not in text  # roll call, staff
        assert "C9-" not in text  # the class's name
        assert _item(doc, "range_template")["removed_passwords"] == 1
        assert _item(doc, "course")["content"].get("is_published") is None

    def test_an_unproven_dependency_refuses_the_export_and_lists_it(self, client, db_session, port):
        t = template(db_session)  # not proven
        s = scenario(db_session, t)
        from _content_port_kit import prove_course, prove_scenario

        prove_scenario(db_session, s, t)
        c = prove_course(db_session, course(db_session, s))
        with acting_as(db_session, UserRole.admin):
            res = export(client, ("course", c))
        assert res.status_code == 409
        detail = res.json()["detail"]
        listed = {(i["item_type"], i["item_id"]): i for i in detail["items"]}
        assert listed[("range_template", str(t.id))]["dependency"] is True
        assert listed[("range_template", str(t.id))]["reasons"][0].startswith("No range built from it")
        assert db_session.query(AuditLog).filter_by(action="content_port.export").count() == 0

    def test_an_unproven_item_is_refused(self, client, db_session, port):
        t = template(db_session)
        with acting_as(db_session, UserRole.admin):
            res = export(client, ("range_template", t))
        assert res.status_code == 409
        assert res.json()["detail"]["items"][0]["dependency"] is False

    def test_another_tenants_private_content_is_not_found(self, client, db_session, port, new_site):
        t = prove_template(db_session, template(db_session, tenant=new_site))
        with acting_as(db_session, UserRole.admin):
            assert export(client, ("range_template", t)).status_code == 404

    def test_the_export_is_audited(self, client, db_session, port):
        t = prove_template(db_session, template(db_session))
        doc = _exported(client, db_session, ("range_template", t))
        row = db_session.query(AuditLog).filter_by(action="content_port.export").one()
        assert row.resource_id == doc["bundle_id"] and signing.signed_sha256(doc) in row.detail


# ── the signature and the bundle's shape ────────────────────────────────────────
class TestSignature:
    def test_no_trusted_key_fails_closed(self, client, db_session, port, monkeypatch):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        monkeypatch.delenv(signing.TRUSTED_ENV)
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc)
        assert res.status_code == 409 and "No trusted content-port key" in res.json()["detail"]

    def test_a_malformed_trusted_key_fails_closed(self, client, db_session, port, monkeypatch):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        monkeypatch.setenv(signing.TRUSTED_ENV, signing.public_info()["public_key"] + ",not-a-key")
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc).status_code == 409

    def _refused(self, client, db_session, doc, status=422, text=""):
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc)
            res2 = upload(client, doc, "import")
        assert res.status_code == status, res.text
        assert res2.status_code == status
        assert text in json.dumps(res.json())
        assert db_session.query(ContentPortImport).count() == 0
        assert db_session.query(AuditLog).filter_by(action="content_port.bundle_rejected").count() >= 1

    def test_a_changed_bundle_is_refused(self, client, db_session, port):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        doc["items"][0]["content"]["yaml"] += "\n# changed\n"
        doc["items"][0]["content_sha256"] = bnd.sha256(doc["items"][0]["content"])
        self._refused(client, db_session, doc, text="does not verify")

    def test_an_untrusted_key_is_refused(self, client, db_session, port):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        self._refused(client, db_session, resign(doc, key=OTHER_KEY), text="does not verify")

    def test_a_feed_bundle_signature_by_the_same_key_is_refused(self, client, db_session, port):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        self._refused(client, db_session, resign(doc, context=feed_signing.CONTEXT), text="does not verify")

    def test_the_certification_construction_by_the_same_key_is_refused(self, client, db_session, port):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        body = {k: v for k, v in doc.items() if k != "signature"}
        key = signing._signing_key()
        raw = cert_signing.canonical(body)
        forged = {**body, "signature": {**doc["signature"], "value": base64.b64encode(key.sign(raw)).decode(),
                                        "signed_sha256": hashlib.sha256(raw).hexdigest()}}
        self._refused(client, db_session, forged, text="does not verify")

    def test_an_unsigned_bundle_is_refused(self, client, db_session, port):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        doc.pop("signature")
        self._refused(client, db_session, doc, text="not signed")

    @pytest.mark.parametrize("raw", [b'{"a": 1, "a": 2}', b'{"a": NaN}', b'{"a": 1.5}', b"not json", b"[]"])
    def test_anything_but_strict_json_is_refused(self, client, db_session, port, raw):
        with acting_as(db_session, UserRole.admin):
            assert upload(client, raw).status_code == 422

    def test_an_oversize_bundle_is_refused(self, client, db_session, port, monkeypatch):
        monkeypatch.setattr(bnd, "MAX_BUNDLE_BYTES", 100)
        with acting_as(db_session, UserRole.admin):
            assert upload(client, b"{" + b" " * 200 + b"}").status_code == 413

    def test_a_bundle_from_the_future_is_refused(self, client, db_session, port):
        doc = _exported(client, db_session, ("range_template", prove_template(db_session, template(db_session))))
        later = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        self._refused(client, db_session, resign(doc, lambda b: b.update(exported_at=later)), text="future")


class TestShape:
    def _doc(self, client, db_session):
        _t, _s, c = proven_set(db_session)
        return _exported(client, db_session, ("course", c))

    def _refused(self, client, db_session, doc, text):
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc)
        assert res.status_code == 422, res.text
        assert text in res.json()["detail"]

    @pytest.mark.parametrize("bad", ["course:../../etc/passwd", "../course:x", "course:/etc/passwd",
                                     "file:///etc/passwd"])
    def test_a_key_that_is_not_type_and_uuid_is_refused(self, client, db_session, port, bad):
        doc = self._doc(client, db_session)
        self._refused(client, db_session, resign(doc, lambda b: b["items"][0].update(key=bad)), "not a content-port")

    def test_a_key_that_is_not_its_own_origin_is_refused(self, client, db_session, port):
        doc = self._doc(client, db_session)
        other = f"range_template:{uuid.uuid4()}"
        self._refused(client, db_session, resign(doc, lambda b: b["items"][0].update(key=other)),
                      "not its type and origin id")

    def test_an_unknown_field_is_refused(self, client, db_session, port):
        doc = self._doc(client, db_session)
        self._refused(client, db_session, resign(doc, lambda b: b["items"][0]["content"].update(path="/etc")),
                      "not a range_template")

    def test_a_content_hash_mismatch_is_refused(self, client, db_session, port):
        doc = self._doc(client, db_session)
        self._refused(client, db_session,
                      resign(doc, lambda b: b["items"][0]["content"].update(name="x"), rehash=False),
                      "does not match its content_sha256")

    def test_a_missing_dependency_is_refused(self, client, db_session, port):
        doc = self._doc(client, db_session)
        self._refused(client, db_session, resign(doc, lambda b: b["items"].pop(0)), "not in the bundle")

    def test_an_unlisted_reference_is_refused(self, client, db_session, port):
        doc = self._doc(client, db_session)

        def drop(b):
            next(i for i in b["items"] if i["item_type"] == "scenario")["depends_on"] = []
        self._refused(client, db_session, resign(doc, drop), "does not list as dependencies")

    def test_a_name_with_control_characters_is_refused(self, client, db_session, port):
        doc = self._doc(client, db_session)
        self._refused(client, db_session, resign(doc, lambda b: b["items"][0]["content"].update(name="a\x00b")),
                      "not a range_template")


# ── preview and import ───────────────────────────────────────────────────────────
class TestImport:
    def test_the_preview_shows_every_item_and_changes_nothing(self, client, db_session, port, new_site):
        t, s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc, target_tenant_id=new_site)
        assert res.status_code == 200, res.text
        body = res.json()
        assert [i["action"] for i in body["items"]] == ["create", "create", "create"]
        assert body["can_import"] is True and body["already_imported_at"] is None
        tmpl = body["items"][0]
        assert "+name: " in tmpl["diff"] and any("password(s) were removed" in n for n in tmpl["notes"])
        assert body["missing_golden_builds"] == [{"catalogue_id": "ubuntu-2404", "build_id": "b-2026-10-01",
                                                  "checksum": "a" * 64,
                                                  "needed_by": sorted([f"range_template:{t.id}", f"scenario:{s.id}",
                                                                       f"course:{c.id}"])}]
        assert db_session.query(Template).filter_by(tenant_id=new_site).count() == 0
        assert db_session.query(ContentPortImport).count() == 0

    def test_a_build_this_site_has_is_not_missing(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        golden_build(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, target_tenant_id=new_site).json()["missing_golden_builds"] == []

    def test_import_lands_everything_not_yet_proven(self, client, db_session, port, new_site):
        t, s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc, "import", target_tenant_id=new_site)
            assert res.status_code == 200, res.text
            out = res.json()
            assert (out["created"], out["updated"], out["skipped"]) == (3, 0, 0)
            new_t = db_session.query(Template).filter_by(tenant_id=new_site).one()
            new_s = db_session.query(Scenario).filter_by(tenant_id=new_site).one()
            new_c = db_session.query(Course).filter_by(tenant_id=new_site).one()
        with acting_as(db_session, UserRole.admin, new_site):
            # each must be proven here: nothing of the origin's proof counts
            st = client.get(f"/content-port/items/range_template/{new_t.id}/proven").json()
        assert st["proven"] is False and st["imported"]["proven_at_origin"] is True
        assert {c_["name"] for c_ in st["conditions"] if not c_["met"]} == {"certified", "used", "fresh"}
        # relinked to the local copies
        assert f"range_template: {new_t.id}" in new_s.yaml and str(t.id) not in new_s.yaml
        mc = db_session.query(ModuleContent).filter(ModuleContent.scenario_id.is_not(None)).all()
        assert {m.scenario_id for m in mc} >= {new_s.id}
        assert new_c.is_published is False and new_c.tenant_id == new_site
        assert new_t.is_public is False
        assert db_session.query(ContentPortItem).count() == 3

    def test_a_bundle_is_imported_once(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, "import", target_tenant_id=new_site).status_code == 200
            again = upload(client, doc, "import", target_tenant_id=new_site)
            preview = upload(client, doc, target_tenant_id=new_site).json()
        assert again.status_code == 409 and "already imported" in again.json()["detail"]
        assert preview["already_imported_at"] is not None and preview["can_import"] is False

    def test_shared_lands_as_shared_content(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, "import", target_tenant_id=new_site, shared="true").status_code == 200
        assert db_session.query(Template).filter_by(tenant_id=new_site).one().is_public is True
        assert db_session.query(Course).filter(Course.tenant_id.is_(None), Course.name == c.name).count() == 1

    def test_an_unknown_target_tenant_is_not_found(self, client, db_session, port):
        _t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, target_tenant_id=uuid.uuid4()).status_code == 404

    def test_a_refused_template_is_not_imported_nor_what_needs_it(self, client, db_session, port, new_site):
        t, _s, c = proven_set(db_session)
        doc = _exported(client, db_session, ("course", c))

        def pin(b):
            _item(b, "range_template")["content"]["yaml"] += "port_group: dPG-TN-MGMT\n"
        bad = resign(doc, pin)
        with acting_as(db_session, UserRole.admin):
            out = upload(client, bad, "import", target_tenant_id=new_site).json()
        actions = {i["item_type"]: i["action"] for i in out["items"]}
        assert actions == {"range_template": "refused", "scenario": "skipped", "course": "skipped"}
        assert db_session.query(Template).filter_by(tenant_id=new_site).count() == 0


class TestConflicts:
    def _import(self, client, db_session, doc, site, **form):
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc, "import", target_tenant_id=site, **form)
        assert res.status_code == 200, res.text
        return {i["item_type"]: i for i in res.json()["items"]}

    def test_unchanged_on_a_second_identical_export(self, client, db_session, port, new_site):
        t = prove_template(db_session, template(db_session))
        self._import(client, db_session, _exported(client, db_session, ("range_template", t)), new_site)
        again = self._import(client, db_session, _exported(client, db_session, ("range_template", t)), new_site)
        assert again["range_template"]["action"] == "unchanged"

    def test_a_newer_origin_version_updates(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        c.description = "Ported, revised."
        db_session.flush()
        out = self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        assert out["course"]["action"] == "update" and out["range_template"]["action"] == "unchanged"
        assert db_session.query(Course).filter_by(tenant_id=new_site).one().description == "Ported, revised."

    def test_changed_here_needs_confirmation(self, client, db_session, port, new_site):
        t = prove_template(db_session, template(db_session))
        self._import(client, db_session, _exported(client, db_session, ("range_template", t)), new_site)
        here = db_session.query(Template).filter_by(tenant_id=new_site).one()
        here.yaml = here.yaml.replace("os: srv2022", "os: srv2019")
        db_session.flush()
        doc = _exported(client, db_session, ("range_template", t))
        with acting_as(db_session, UserRole.admin):
            pv = upload(client, doc, target_tenant_id=new_site).json()["items"][0]
        assert (pv["action"], pv["conflict"]) == ("conflict", "local_changed")
        assert "-  os: srv2019" in pv["diff"] and "+  os: srv2022" in pv["diff"]
        skipped = self._import(client, db_session, doc, new_site)
        assert skipped["range_template"]["action"] == "skipped"
        assert "srv2019" in here.yaml
        doc2 = _exported(client, db_session, ("range_template", t))
        done = self._import(client, db_session, doc2, new_site, overwrite=[f"range_template:{t.id}"])
        assert done["range_template"]["action"] == "update"
        db_session.refresh(here)
        assert "srv2019" not in here.yaml

    def test_a_same_named_item_here_needs_confirmation(self, client, db_session, port, new_site):
        t = prove_template(db_session, template(db_session))
        mine = template(db_session, name=t.name, tenant=new_site, yaml="name: mine\nnodes: []\n")
        doc = _exported(client, db_session, ("range_template", t))
        out = self._import(client, db_session, doc, new_site)
        assert out["range_template"]["action"] == "skipped"
        assert out["range_template"]["conflict"] == "local_name"
        db_session.refresh(mine)
        assert mine.yaml == "name: mine\nnodes: []\n"

    def test_an_older_export_is_a_downgrade(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        older = _exported(client, db_session, ("course", c))
        c.description = "Newer."
        db_session.flush()
        newer = _exported(client, db_session, ("course", c))
        newer = resign(newer, lambda b: b.update(exported_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()))
        self._import(client, db_session, newer, new_site)
        out = self._import(client, db_session, older, new_site)
        assert (out["course"]["action"], out["course"]["conflict"]) == ("skipped", "older")
        assert db_session.query(Course).filter_by(tenant_id=new_site).one().description == "Newer."

    def test_overwrite_may_name_only_conflicts(self, client, db_session, port, new_site):
        t = prove_template(db_session, template(db_session))
        doc = _exported(client, db_session, ("range_template", t))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc, "import", target_tenant_id=new_site, overwrite=[f"range_template:{t.id}"])
        assert res.status_code == 422

    def test_a_quiz_with_student_attempts_is_kept_and_replaced(self, client, db_session, port, new_site):
        from _content_port_kit import user
        from app.models import ContentKind, CourseModule, Quiz, QuizAttempt

        _t, _s, c = proven_set(db_session)
        self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        here = db_session.query(Course).filter_by(tenant_id=new_site).one()
        old_quiz = db_session.query(Quiz).filter_by(tenant_id=new_site).one()
        student = user(db_session, UserRole.student, new_site)
        db_session.add(QuizAttempt(id=uuid.uuid4(), quiz_id=old_quiz.id, user_id=student.id, tenant_id=new_site))
        c.description = "Revised."
        db_session.flush()
        out = self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        assert out["course"]["action"] == "update"
        assert any("has Student attempts" in n for n in out["course"]["notes"])
        assert db_session.query(Quiz).filter_by(tenant_id=new_site).count() == 2
        assert db_session.query(QuizAttempt).filter_by(quiz_id=old_quiz.id).count() == 1
        check = (db_session.query(ModuleContent).join(CourseModule, CourseModule.id == ModuleContent.module_id)
                 .filter(CourseModule.course_id == here.id, ModuleContent.content_kind == ContentKind.check).one())
        assert check.quiz_id not in (None, old_quiz.id)


class TestReviewFindings:
    """The adversarial review of this change (2026-10-27): each is a regression test."""

    def _import(self, client, db_session, doc, site, **form):
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc, "import", target_tenant_id=site, **form)
        assert res.status_code == 200, res.text
        return {i["item_type"]: i for i in res.json()["items"]}

    def test_a_lesson_another_course_uses_is_never_rewritten(self, client, db_session, port, new_site):
        from app.models import ContentKind, CourseModule, Lesson, ModuleContentType

        _t, _s, c = proven_set(db_session)
        shared_lesson = Lesson(id=uuid.uuid4(), title="Shared", body_markdown="original", tenant_id=None,
                               is_published=True)
        db_session.add(shared_lesson)
        mine = course(db_session, name=c.name, tenant=new_site)  # same name on the new site
        other = course(db_session, tenant=new_site)
        for crs in (mine, other):
            m = db_session.query(CourseModule).filter_by(course_id=crs.id).one()
            m.content_type = ModuleContentType.reading
            row = db_session.query(ModuleContent).filter_by(module_id=m.id, content_kind=ContentKind.teach).one()
            row.lesson_id = shared_lesson.id
        db_session.flush()
        out = self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site,
                           overwrite=[f"course:{c.id}"])
        assert out["course"]["action"] == "update"
        db_session.refresh(shared_lesson)
        assert (shared_lesson.body_markdown, shared_lesson.is_published) == ("original", True)

    def test_module_content_ref_is_carried_and_a_local_edit_is_a_conflict(self, client, db_session, port, new_site):
        from app.models import CourseModule

        _t, s, c = proven_set(db_session)
        m = db_session.query(CourseModule).filter_by(course_id=c.id).one()
        m.content_ref = json.dumps({"objectives": ["Find it"], "lab": "L", "retired": True,
                                    "scenario_id": str(s.id)})
        db_session.flush()
        doc = _exported(client, db_session, ("course", c))
        assert "scenario_id" not in _item(doc, "course")["content"]["modules"][0]["content_ref"]
        self._import(client, db_session, doc, new_site)
        here = db_session.query(Course).filter_by(tenant_id=new_site).one()
        hm = db_session.query(CourseModule).filter_by(course_id=here.id).one()
        ref = json.loads(hm.content_ref)
        new_s = db_session.query(Scenario).filter_by(tenant_id=new_site).one()
        assert ref == {"objectives": ["Find it"], "lab": "L", "retired": True, "scenario_id": str(new_s.id)}
        hm.content_ref = json.dumps({**ref, "retired": False})
        c.description = "newer"
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            pv = upload(client, _exported(client, db_session, ("course", c)), target_tenant_id=new_site).json()
        course_item = next(i for i in pv["items"] if i["item_type"] == "course")
        assert (course_item["action"], course_item["conflict"]) == ("conflict", "local_changed")

    def test_an_update_keeps_a_published_course_published(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        here = db_session.query(Course).filter_by(tenant_id=new_site).one()
        assert here.is_published is False  # landed unpublished
        here.is_published = True
        db_session.flush()
        import app.content_port.bundle as b

        link = db_session.query(ContentPortItem).filter_by(item_type="course", local_id=str(here.id)).one()
        assert link.local_fingerprint == b.fingerprint(db_session, "course", here)  # publishing is not an edit
        c.description = "newer"
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            pv = upload(client, _exported(client, db_session, ("course", c)), target_tenant_id=new_site).json()
        item = next(i for i in pv["items"] if i["item_type"] == "course")
        assert item["action"] == "update"
        assert any("published here" in n for n in item["notes"])
        out = self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        assert out["course"]["action"] == "update"
        db_session.refresh(here)
        assert here.is_published is True and here.description == "newer"

    def test_private_then_shared_is_a_visibility_conflict(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        out = self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site,
                           shared="true")
        assert (out["range_template"]["action"], out["range_template"]["conflict"]) == ("skipped", "visibility")
        assert out["course"]["action"] == "skipped"  # a shared course never depends on a private scenario
        assert db_session.query(Template).filter_by(tenant_id=new_site).one().is_public is False
        assert db_session.query(Course).filter(Course.tenant_id.is_(None), Course.name == c.name).count() == 0

    def test_an_uppercase_template_id_is_relinked(self, client, db_session, port, new_site):
        from _content_port_kit import prove_scenario

        t = prove_template(db_session, template(db_session))
        s = scenario(db_session, t)
        s.yaml = f"name: Upper\nrange_template: '{str(t.id).upper()}'\nobjectives: []\n"
        db_session.flush()
        prove_scenario(db_session, s, t)
        self._import(client, db_session, _exported(client, db_session, ("scenario", s)), new_site)
        new_t = db_session.query(Template).filter_by(tenant_id=new_site).one()
        new_s = db_session.query(Scenario).filter_by(tenant_id=new_site).one()
        from app.content_port import proven as pv

        assert pv.scenario_template(db_session, new_s).id == new_t.id

    def test_origin_only_course_meta_is_not_carried(self, client, db_session, port):
        _t, _s, c = proven_set(db_session)
        c.course_meta = json.dumps({"course_code": "C901", "superseded_by": str(uuid.uuid4()), "retired": True,
                                    "test_content": {"label": "TEST"}, "someday_key": 1})
        db_session.flush()
        meta = json.loads(_item(_exported(client, db_session, ("course", c)), "course")["content"]["course_meta"])
        assert meta == {"course_code": "C901"}  # an allowlist: unknown keys never leave either

    def test_this_sites_course_meta_state_is_kept_and_none_comes_in(self, client, db_session, port, new_site):
        _t, _s, c = proven_set(db_session)
        c.course_meta = json.dumps({"course_code": "C901", "test_content": {"label": "TEST"}})
        db_session.flush()
        self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        here = db_session.query(Course).filter_by(tenant_id=new_site).one()
        assert json.loads(here.course_meta) == {"course_code": "C901"}  # no origin test content lands
        here.course_meta = json.dumps({"course_code": "C901", "test_content": {"label": "LOCAL"}})
        db_session.flush()
        link = db_session.query(ContentPortItem).filter_by(item_type="course", local_id=str(here.id)).one()
        assert link.local_fingerprint == bnd.fingerprint(db_session, "course", here)  # site state is no edit
        c.course_meta = json.dumps({"course_code": "C902"})
        db_session.flush()
        out = self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site)
        assert out["course"]["action"] == "update"
        db_session.refresh(here)
        assert json.loads(here.course_meta) == {"course_code": "C902", "test_content": {"label": "LOCAL"}}

    def test_a_replacement_lesson_keeps_the_published_state(self, client, db_session, port, new_site):
        from app.models import ContentKind, CourseModule, Lesson

        _t, _s, c = proven_set(db_session)
        mine = course(db_session, name=c.name, tenant=new_site)
        shared_lesson = Lesson(id=uuid.uuid4(), title="Shared", body_markdown="original", tenant_id=None,
                               is_published=True)
        db_session.add(shared_lesson)
        m = db_session.query(CourseModule).filter_by(course_id=mine.id).one()
        row = db_session.query(ModuleContent).filter_by(module_id=m.id, content_kind=ContentKind.teach).one()
        row.lesson_id = shared_lesson.id
        db_session.flush()
        self._import(client, db_session, _exported(client, db_session, ("course", c)), new_site,
                     overwrite=[f"course:{c.id}"])
        db_session.refresh(row)
        assert row.lesson_id != shared_lesson.id
        assert db_session.get(Lesson, row.lesson_id).is_published is True

    def test_a_course_whose_scenario_is_out_of_scope_is_not_proven(self, client, db_session, port, new_site):
        from _content_port_kit import prove_course

        foreign_t = template(db_session, tenant=new_site)
        foreign_s = scenario(db_session, foreign_t, tenant=new_site)  # another tenant's, private
        c = prove_course(db_session, course(db_session, foreign_s))
        with acting_as(db_session, UserRole.admin):
            st = client.get(f"/content-port/items/course/{c.id}/proven").json()
        assert st["proven"] is False
        assert "not shared with its tenant" in st["reasons"][0]


def test_a_signing_context_is_one_line():
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        feed_signing.sign_for(b"no newline", {"a": 1}, key)
    with pytest.raises(ValueError):
        feed_signing.sign_for(b"two\nlines\n", {"a": 1}, key)
    assert not feed_signing.verify_for(b"two\nlines\n", feed_signing.sign_for(b"x\n", {"a": 1}, key),
                                       key.public_key())
