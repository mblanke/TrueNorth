"""The content feeds' API (ADR 0014 decision 25, issue #287): the feeds status and the
signed feed bundle import. Admins only; a platform administrator imports. A bundle is
applied only when its signature verifies against the configured public key; an unsigned,
foreign-signed or altered one is refused, recorded and audit-logged, and stores nothing.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.content_feeds import ContentFeedEntry, ContentFeedFlag, ContentFeedSource
from app.content_review import ContentReviewItem
from app.main import app as fastapi_app
from app.models import AuditLog, Tenant, User, UserRole
from app.range_certification import signing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime.now(UTC)
SITE_KEY = Ed25519PrivateKey.generate()
OTHER_KEY = Ed25519PrivateKey.generate()


def _b64_public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


# What a feed bundle's signature covers: this context, then the canonical JSON of the
# bundle without its signature (app/content_feeds/bundle_signing.py). Spelled out here so
# a change to the construction fails these tests.
FEED_CONTEXT = b"truenorth/content-feed-bundle/v1\n"


def sign(doc: dict, key: Ed25519PrivateKey = SITE_KEY, *, context: bytes = FEED_CONTEXT) -> dict:
    """What scripts/build_feed_bundle.py does. ``context=b""`` is the range certification
    export's construction, which a feed bundle must not accept."""
    body = context + signing.canonical({k: v for k, v in doc.items() if k != "signature"})
    pub = key.public_key()
    return {**doc, "signature": {"alg": "Ed25519", "key_id": signing.key_id(pub), "public_key": _b64_public(key),
                                 "signed_sha256": hashlib.sha256(body).hexdigest(),
                                 "value": base64.b64encode(key.sign(body)).decode()}}


def bundle(generated: datetime | None = None, entries: list | None = None) -> dict:
    return {
        "format": "truenorth-feed-bundle/1",
        "generated_at": (generated or NOW).isoformat(),
        "entries": entries if entries is not None else [
            {"key": "eol:ubuntu:20.04", "source": "endoflife", "kind": "release", "products": ["eol:ubuntu:20.04"],
             "at": "2025-05-31T00:00:00+00:00", "title": "ubuntu 20.04 end of life",
             "data": {"product": "ubuntu", "cycle": "20.04", "eol": "2025-05-31"}},
            {"key": "kev:CVE-2026-21001", "source": "cisa_kev", "kind": "vulnerability",
             "products": ["kev:microsoft:exchange server"], "at": "2026-10-23T00:00:00+00:00",
             "title": "Microsoft Exchange Server Remote Code Execution Vulnerability",
             "data": {"cve": "CVE-2026-21001"}},
        ],
    }


def _user(db, role: UserRole, tenant: uuid.UUID = DEV) -> User:
    u = User(id=uuid.uuid4(), email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test", display_name=role.value,
             role=role, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(db, role: UserRole, tenant: uuid.UUID = DEV):
    u = _user(db, role, tenant)
    who = CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                      tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield u
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def upload(client, doc):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return client.post("/content-feeds/bundle", files={"file": ("feeds.json", raw, "application/json")})


@pytest.fixture
def bundle_mode(monkeypatch):
    monkeypatch.setenv("TN_FEEDS_MODE", "bundle")
    monkeypatch.setenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", _b64_public(SITE_KEY))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))


def _entries(db) -> set[str]:
    return {e.entry_key for e in db.query(ContentFeedEntry).all()}


# ── permissions ──────────────────────────────────────────────────────────────
class TestPermissions:
    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops, UserRole.observer, UserRole.student])
    def test_only_admins_read_the_status_or_import(self, client, db_session, bundle_mode, role):
        with acting_as(db_session, role):
            assert client.get("/content-feeds/status").status_code == 403
            assert upload(client, sign(bundle())).status_code == 403
        assert _entries(db_session) == set()

    def test_an_admin_reads_the_status(self, client, db_session, bundle_mode):
        with acting_as(db_session, UserRole.admin):
            res = client.get("/content-feeds/status")
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "bundle" and body["can_import"] is True
        assert body["bundle_key_id"] == signing.key_id(SITE_KEY.public_key())
        assert [s["source"] for s in body["sources"]] == ["bundle"]

    def test_a_tenant_admin_who_is_not_the_platform_admin_cannot_import(self, client, db_session, bundle_mode):
        other = Tenant(id=uuid.uuid4(), name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        with acting_as(db_session, UserRole.admin, other.id):
            assert client.get("/content-feeds/status").json()["can_import"] is False
            res = upload(client, sign(bundle()))
        assert res.status_code == 403
        assert _entries(db_session) == set()


# ── the signature ────────────────────────────────────────────────────────────
class TestSignature:
    def test_a_bundle_signed_by_the_site_key_is_applied(self, client, db_session, bundle_mode):
        with acting_as(db_session, UserRole.admin) as me:
            res = upload(client, sign(bundle()))
        assert res.status_code == 200, res.text
        assert res.json()["entries"] == 2 and res.json()["new"] == 2
        assert _entries(db_session) == {"eol:ubuntu:20.04", "kev:CVE-2026-21001"}
        row = db_session.query(ContentFeedEntry).filter_by(entry_key="kev:CVE-2026-21001").one()
        assert row.origin == "bundle" and row.kind == "vulnerability"
        status = db_session.get(ContentFeedSource, "bundle")
        assert status.last_success_at is not None and status.last_error == "" and status.entries == 2
        assert status.detail["key_id"] == signing.key_id(SITE_KEY.public_key())
        audit = db_session.query(AuditLog).filter_by(action="content_feeds.bundle_import").one()
        assert audit.user_id == me.id and "2 entries" in audit.detail

    @pytest.mark.parametrize("case", ["unsigned", "other_key", "altered", "key_swapped", "garbage"])
    def test_an_unsigned_or_mismatched_bundle_is_refused_and_recorded(self, client, db_session, bundle_mode, case):
        doc = sign(bundle())
        if case == "unsigned":
            doc = bundle()
        elif case == "other_key":
            doc = sign(bundle(), OTHER_KEY)
        elif case == "altered":
            doc = copy.deepcopy(doc)
            doc["entries"][0]["key"] = "eol:ubuntu:24.04"
        elif case == "key_swapped":  # the bundle carries its own key: never trusted
            doc = sign(bundle(), OTHER_KEY)
            doc["signature"]["key_id"] = signing.key_id(SITE_KEY.public_key())
        elif case == "garbage":
            doc = b"\x00not json"
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc)
        assert res.status_code == 422, res.text
        assert _entries(db_session) == set()
        status = db_session.get(ContentFeedSource, "bundle")
        assert status.last_error and status.last_success_at is None
        assert db_session.query(AuditLog).filter_by(action="content_feeds.bundle_rejected").count() == 1

    def test_the_recorded_hash_is_of_what_was_verified_not_what_the_bundle_claims(self, client, db_session,
                                                                                  bundle_mode):
        doc = sign(bundle())
        true = doc["signature"]["signed_sha256"]
        doc["signature"]["signed_sha256"] = "0" * 64  # not covered by the signature
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc).status_code == 200
        assert db_session.get(ContentFeedSource, "bundle").detail["signed_sha256"] == true

    def test_a_bundle_over_the_size_limit_is_refused(self, client, db_session, bundle_mode, monkeypatch):
        from app.content_feeds import service

        monkeypatch.setattr(service, "MAX_BUNDLE_BYTES", 100)
        with acting_as(db_session, UserRole.admin):
            res = upload(client, sign(bundle()))
        assert res.status_code == 413
        assert _entries(db_session) == set()

    def test_no_configured_key_no_import(self, client, db_session, bundle_mode, monkeypatch):
        monkeypatch.delenv("TN_FEEDS_BUNDLE_PUBLIC_KEY")
        with acting_as(db_session, UserRole.admin):
            assert client.get("/content-feeds/status").json()["can_import"] is False
            res = upload(client, sign(bundle()))
        assert res.status_code == 409 and "public key" in res.json()["detail"]
        assert _entries(db_session) == set()

    @pytest.mark.parametrize("mode", ["", "off", "online"])
    def test_bundles_are_imported_only_in_bundle_mode(self, client, db_session, bundle_mode, monkeypatch, mode):
        monkeypatch.setenv("TN_FEEDS_MODE", mode)
        with acting_as(db_session, UserRole.admin):
            res = upload(client, sign(bundle()))
        assert res.status_code == 409
        assert _entries(db_session) == set()

    def test_a_signed_document_that_is_not_a_feed_bundle_is_refused(self, client, db_session, bundle_mode):
        bad = bundle(entries=[{"key": "nvd:CVE-1", "source": "cisa_kev", "kind": "vulnerability",
                               "products": ["x"], "at": "2026-10-01T00:00:00+00:00"}])
        with acting_as(db_session, UserRole.admin):
            res = upload(client, sign(bad))
        assert res.status_code == 422 and "not a feed bundle" in res.json()["detail"]
        assert _entries(db_session) == set()

    def test_an_older_bundle_than_the_last_one_imported_is_refused(self, client, db_session, bundle_mode):
        with acting_as(db_session, UserRole.admin):
            assert upload(client, sign(bundle(NOW))).status_code == 200
            res = upload(client, sign(bundle(NOW - timedelta(days=3), entries=[
                {"key": "nvd:CVE-2026-9", "source": "nvd", "kind": "vulnerability", "products": ["cpe:x"],
                 "at": "2026-10-01T00:00:00+00:00"}])))
        assert res.status_code == 409
        assert "nvd:CVE-2026-9" not in _entries(db_session)

    def test_a_newer_bundle_replaces_the_last_ones_entries_outright(self, client, db_session, bundle_mode):
        """The undo: a corrected bundle drops what the bad one said, products included."""
        first = bundle(NOW)
        first["entries"][1]["products"] = ["kev:microsoft:exchange server", "kev:linux:kernel"]
        second = bundle(NOW + timedelta(days=1), entries=[bundle()["entries"][1]])  # no EOL entry, no Linux
        with acting_as(db_session, UserRole.admin):
            upload(client, sign(first))
            res = upload(client, sign(second))
        assert res.json() | {"generated_at": None} == {
            "entries": 1, "new": 0, "updated": 1, "removed": 1, "generated_at": None,
            "key_id": signing.key_id(SITE_KEY.public_key())}
        assert _entries(db_session) == {"kev:CVE-2026-21001"}
        row = db_session.query(ContentFeedEntry).filter_by(entry_key="kev:CVE-2026-21001").one()
        assert row.products == ["kev:microsoft:exchange server"]

    def test_a_bundle_never_removes_what_the_online_pull_stored(self, client, db_session, bundle_mode):
        now = datetime.now(UTC)
        db_session.add(ContentFeedEntry(id=uuid.uuid4(), entry_key="nvd:CVE-2026-1", source="nvd",
                                        kind="vulnerability", products=["cpe:x"], at=now, title="", data={},
                                        origin="online", first_seen_at=now, updated_at=now))
        db_session.commit()
        with acting_as(db_session, UserRole.admin):
            assert upload(client, sign(bundle(entries=[]))).status_code == 200
        assert _entries(db_session) == {"nvd:CVE-2026-1"}

    def test_a_signature_without_the_feed_bundle_context_is_refused(self, client, db_session, bundle_mode):
        """Domain separation: a document signed the range-certification way (no context) by
        the same key is not a feed bundle."""
        with acting_as(db_session, UserRole.admin):
            res = upload(client, sign(bundle(), context=b""))
        assert res.status_code == 422
        assert _entries(db_session) == set()

    @pytest.mark.parametrize("case", ["deep", "surrogate", "trailing"])
    def test_a_file_that_breaks_the_parser_is_a_recorded_refusal(self, client, db_session, bundle_mode, case):
        raw = {"deep": b"[" * 100_000 + b"]" * 100_000,
               "surrogate": json.dumps({**sign(bundle()), "x": "\ud800"}).encode().replace(b"\\ud800", b"\ud800"),
               "trailing": json.dumps(sign(bundle())).encode() + b" garbage"}[case]
        with acting_as(db_session, UserRole.admin):
            res = upload(client, raw)
        assert res.status_code == 422, res.text
        assert _entries(db_session) == set()
        assert db_session.get(ContentFeedSource, "bundle").last_error

    @pytest.mark.parametrize("where", ["bundle", "entry"])
    def test_unknown_fields_are_refused_even_when_signed(self, client, db_session, bundle_mode, where):
        doc = bundle()
        if where == "bundle":
            doc["applies_to"] = "everything"
        else:
            doc["entries"][0]["severity"] = "critical"
        with acting_as(db_session, UserRole.admin):
            res = upload(client, sign(doc))
        assert res.status_code == 422 and "not a feed bundle" in res.json()["detail"]
        assert _entries(db_session) == set()

    def test_a_bundle_dated_more_than_a_day_ahead_is_refused(self, client, db_session, bundle_mode):
        """Else it would lock out every honest bundle until that date."""
        with acting_as(db_session, UserRole.admin):
            res = upload(client, sign(bundle(datetime.now(UTC) + timedelta(days=2))))
            assert res.status_code == 422 and "future" in res.json()["detail"]
            assert upload(client, sign(bundle(datetime.now(UTC) + timedelta(hours=23)))).status_code == 200
        assert db_session.query(AuditLog).filter_by(action="content_feeds.bundle_rejected").count() == 1


# ── the status ───────────────────────────────────────────────────────────────
class TestStatus:
    def test_online_mode_lists_the_three_sources_with_their_errors(self, client, db_session, monkeypatch):
        monkeypatch.setenv("TN_FEEDS_MODE", "online")
        db_session.add(ContentFeedSource(source="nvd", last_attempt_at=NOW, last_success_at=None,
                                         last_error="services.nvd.nist.gov answered HTTP 503", entries=0, cursor="",
                                         detail={}))
        db_session.commit()
        with acting_as(db_session, UserRole.admin):
            body = client.get("/content-feeds/status").json()
        assert body["mode"] == "online" and body["can_import"] is False
        assert [s["source"] for s in body["sources"]] == ["endoflife", "cisa_kev", "nvd"]
        nvd = body["sources"][2]
        assert nvd["last_error"] == "services.nvd.nist.gov answered HTTP 503" and nvd["last_success_at"] is None

    def test_off_by_default(self, client, db_session, monkeypatch):
        monkeypatch.delenv("TN_FEEDS_MODE", raising=False)
        with acting_as(db_session, UserRole.admin):
            body = client.get("/content-feeds/status").json()
        assert body["mode"] == "off" and body["sources"] == [] and body["can_import"] is False

    def test_flagged_items_counts_only_what_the_caller_can_see(self, client, db_session, monkeypatch):
        monkeypatch.setenv("TN_FEEDS_MODE", "online")
        other = Tenant(id=uuid.uuid4(), name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        for tenant in (None, DEV, other.id):
            item = ContentReviewItem(id=uuid.uuid4(), tenant_id=tenant, item_type="golden_template",
                                     item_id=str(uuid.uuid4()), title="g", due_at=NOW, stale_reasons=[])
            db_session.add(item)
            db_session.flush()
            db_session.add(ContentFeedFlag(id=uuid.uuid4(), item_id=item.id, entry_key="eol:ubuntu:20.04",
                                           raised_at=NOW))
        db_session.commit()
        with acting_as(db_session, UserRole.admin):
            assert client.get("/content-feeds/status").json()["flagged_items"] == 2  # shared + own, not theirs
