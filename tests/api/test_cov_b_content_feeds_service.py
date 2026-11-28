# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""The content feeds' service side (app/content_feeds/service.py, ADR 0014 decision 25).

Mode and key fail closed: anything but a known mode is ``off``, and a missing or malformed
bundle public key refuses every import. An import verifies before it stores, records every
refusal on the bundle source row and in the audit log, and replaces the last bundle's
entries outright — an entry the new bundle lacks is removed, and what the online pull
stored is never touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser
from app.content_feeds import ContentFeedEntry, ContentFeedSource, service
from app.content_feeds.service import BundleRejectedError, bundle_key, can_import, import_bundle, mode, status
from app.models import AuditLog
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


# What a feed bundle's signature covers (scripts/build_feed_bundle.py): the context, then
# the canonical JSON of the bundle without its signature.
FEED_CONTEXT = b"truenorth/content-feed-bundle/v1\n"


def _sign(doc: dict, key: Ed25519PrivateKey = SITE_KEY) -> dict:
    body = FEED_CONTEXT + signing.canonical({k: v for k, v in doc.items() if k != "signature"})
    pub = key.public_key()
    return {
        **doc,
        "signature": {
            "alg": "Ed25519",
            "key_id": signing.key_id(pub),
            "public_key": _b64_public(key),
            "signed_sha256": hashlib.sha256(body).hexdigest(),
            "value": base64.b64encode(key.sign(body)).decode(),
        },
    }


def _bundle(generated: datetime | None = None, entries: list | None = None) -> dict:
    return {
        "format": "truenorth-feed-bundle/1",
        "generated_at": (generated or NOW).isoformat(),
        "entries": entries
        if entries is not None
        else [
            {
                "key": "eol:ubuntu:20.04",
                "source": "endoflife",
                "kind": "release",
                "products": ["eol:ubuntu:20.04"],
                "at": "2025-05-31T00:00:00+00:00",
                "title": "ubuntu 20.04 end of life",
                "data": {"product": "ubuntu", "cycle": "20.04", "eol": "2025-05-31"},
            },
        ],
    }


def _who(uid: object = DEV, role="admin", tenant: uuid.UUID = DEV) -> CurrentUser:
    return CurrentUser(
        id=str(uid),
        email="feeds@example.test",
        display_name="Feeds Admin",
        role=role,
        tenant_id=str(tenant),
        keycloak_id="kc-feeds",
    )


@pytest.fixture
def bundle_mode(monkeypatch):
    monkeypatch.setenv("TN_FEEDS_MODE", "bundle")
    monkeypatch.setenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", _b64_public(SITE_KEY))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))


# ── mode and key fail closed ─────────────────────────────────────────────────
def test_mode_is_off_unless_the_setting_names_a_known_mode(monkeypatch):
    monkeypatch.delenv("TN_FEEDS_MODE", raising=False)
    assert mode() == "off"
    monkeypatch.setenv("TN_FEEDS_MODE", "not-a-mode")
    assert mode() == "off"  # an unknown value is off, not an error and not a fallback mode
    monkeypatch.setenv("TN_FEEDS_MODE", "  BUNDLE ")  # case and surrounding blanks are tolerated
    assert mode() == "bundle"


def test_bundle_key_is_none_until_a_real_public_key_is_configured(monkeypatch):
    monkeypatch.delenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", raising=False)
    key, why = bundle_key()
    assert key is None and "No feed bundle public key is configured" in why


def test_bundle_key_fails_closed_on_a_value_that_is_not_a_32_byte_public_key(monkeypatch):
    for raw in ("!!not base64!!", base64.b64encode(b"short").decode()):  # undecodable, then 5 bytes
        monkeypatch.setenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", raw)
        key, why = bundle_key()
        assert key is None and "not a base64 Ed25519 public key" in why
    monkeypatch.setenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", _b64_public(SITE_KEY))
    key, why = bundle_key()
    assert why is None and signing.key_id(key) == signing.key_id(SITE_KEY.public_key())


# ── who may import ───────────────────────────────────────────────────────────
def test_only_the_platform_admin_may_import(db_session, bundle_mode):
    assert can_import(db_session, _who()) is True
    assert can_import(db_session, _who(role="instructor")) is False
    other = uuid.uuid4()
    assert can_import(db_session, _who(tenant=other)) is False  # another tenant's admin is not it


# ── a verified bundle is stored ──────────────────────────────────────────────
def test_a_verified_bundle_is_stored_audited_and_recorded(db_session, bundle_mode):
    out = import_bundle(db_session, _who(), json.dumps(_sign(_bundle())).encode())
    db_session.flush()  # the caller commits; the session does not autoflush
    assert out.entries == 1 and out.new == 1 and out.updated == 0 and out.removed == 0
    assert out.key_id == signing.key_id(SITE_KEY.public_key())
    row = db_session.query(ContentFeedEntry).filter_by(entry_key="eol:ubuntu:20.04").one()
    assert row.origin == "bundle" and row.products == ["eol:ubuntu:20.04"] and row.kind == "release"
    recorded = db_session.get(ContentFeedSource, "bundle")
    assert recorded.entries == 1 and recorded.last_error == "" and recorded.last_success_at is not None
    assert recorded.detail["generated_at"] == NOW.isoformat()
    assert recorded.detail["key_id"] == signing.key_id(SITE_KEY.public_key())
    audit = db_session.query(AuditLog).filter_by(action="content_feeds.bundle_import").one()
    assert audit.user_id == DEV and "1 entries (1 new, 0 updated, 0 removed)" in audit.detail


def test_the_audit_row_of_a_caller_with_an_unparseable_id_names_no_user(db_session, bundle_mode):
    """A caller whose id is not a UUID (service) must not abort the import or the audit."""
    import_bundle(db_session, _who(uid="not-a-uuid"), json.dumps(_sign(_bundle())).encode())
    db_session.flush()
    audit = db_session.query(AuditLog).filter_by(action="content_feeds.bundle_import").one()
    assert audit.user_id is None and audit.resource_type == "content_feeds"


def test_a_newer_bundle_updates_its_entries_and_drops_the_ones_it_lacks(db_session, bundle_mode):
    first = _bundle(
        entries=[
            _bundle()["entries"][0],
            {
                "key": "kev:CVE-2026-1",
                "source": "cisa_kev",
                "kind": "vulnerability",
                "products": ["kev:microsoft:exchange server"],
                "at": "2026-10-23T00:00:00+00:00",
                "title": "Exchange RCE",
                "data": {"cve": "CVE-2026-1"},
            },
        ]
    )
    import_bundle(db_session, _who(), json.dumps(_sign(first)).encode())
    second = _bundle(
        NOW + timedelta(days=1),
        entries=[
            {
                **_bundle()["entries"][0],
                "title": "ubuntu 20.04 end of life (extended)",
                "products": ["eol:ubuntu:20.04", "release:ubuntu"],
            },
        ],
    )
    out = import_bundle(db_session, _who(), json.dumps(_sign(second)).encode())
    db_session.flush()
    assert (out.entries, out.new, out.updated, out.removed) == (1, 0, 1, 1)
    row = db_session.query(ContentFeedEntry).filter_by(entry_key="eol:ubuntu:20.04").one()
    assert row.title == "ubuntu 20.04 end of life (extended)"
    assert row.products == ["eol:ubuntu:20.04", "release:ubuntu"]  # the bundle's, not a union
    assert db_session.query(ContentFeedEntry).filter_by(entry_key="kev:CVE-2026-1").count() == 0


def test_a_bundle_never_removes_what_the_online_pull_stored(db_session, bundle_mode):
    now = datetime.now(UTC)
    db_session.add(
        ContentFeedEntry(
            id=uuid.uuid4(),
            entry_key="nvd:CVE-2026-1",
            source="nvd",
            kind="vulnerability",
            products=["cpe:x"],
            at=now,
            title="",
            data={},
            origin="online",
            first_seen_at=now,
            updated_at=now,
        )
    )
    db_session.flush()
    import_bundle(db_session, _who(), json.dumps(_sign(_bundle(entries=[]))).encode())
    db_session.flush()
    keys = {e.entry_key for e in db_session.query(ContentFeedEntry).all()}
    assert keys == {"nvd:CVE-2026-1"}


def test_a_naive_generated_at_is_read_as_utc_not_as_a_local_time(db_session, bundle_mode):
    out = import_bundle(db_session, _who(), json.dumps(_sign(_bundle(datetime(2026, 11, 5, 12, 0, 0)))).encode())
    db_session.flush()
    assert out.generated_at.utcoffset() == timedelta(0)
    assert db_session.get(ContentFeedSource, "bundle").detail["generated_at"] == "2026-11-05T12:00:00+00:00"


# ── every refusal is recorded and stores nothing ─────────────────────────────
def test_the_status_reads_the_last_import_back(db_session, bundle_mode):
    import_bundle(db_session, _who(), json.dumps(_sign(_bundle())).encode())
    db_session.flush()
    body = status(db_session, _who())
    assert body.mode == "bundle" and body.can_import is True
    assert body.bundle_key_id == signing.key_id(SITE_KEY.public_key()) and body.bundle_key_problem is None
    assert [s.source for s in body.sources] == ["bundle"]
    assert body.sources[0].generated_at == NOW and body.sources[0].entries == 1
    assert body.entries == 1 and body.flagged_items == 0


def test_the_status_of_a_source_without_detail_shows_no_generation(db_session, monkeypatch):
    monkeypatch.setenv("TN_FEEDS_MODE", "online")
    db_session.add(
        ContentFeedSource(source="nvd", last_attempt_at=NOW, last_error="HTTP 503", entries=0, cursor="", detail={})
    )
    db_session.flush()
    body = status(db_session, _who())
    nvd = next(s for s in body.sources if s.source == "nvd")
    assert nvd.generated_at is None and nvd.key_id is None and nvd.last_error == "HTTP 503"
    assert body.can_import is False and body.bundle_key_problem.startswith("Feed bundles are imported")


@pytest.mark.parametrize(
    ("doc", "status_code", "why"),
    [
        (b"\x00 not json", 422, "not readable JSON"),
        (json.dumps({"format": "truenorth-feed-bundle/1"}).encode(), 422, "not signed"),
        (b"[" * 100_000 + b"]" * 100_000, 422, "not readable JSON"),  # too deeply nested to parse at all
    ],
)
def test_a_file_the_parser_or_the_signature_refuses_is_never_stored(db_session, bundle_mode, doc, status_code, why):
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), doc)
    db_session.flush()
    assert err.value.status == status_code and why in err.value.message
    assert db_session.query(ContentFeedEntry).count() == 0
    assert db_session.get(ContentFeedSource, "bundle").last_error  # the refusal is on the record


def test_a_bundle_that_does_not_verify_is_refused_and_recorded(db_session, bundle_mode):
    doc = _sign(_bundle(), OTHER_KEY)  # signed by a foreign key
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(doc).encode())
    db_session.flush()  # materialise the first refusal's row before the second import
    assert err.value.status == 422 and "does not verify" in err.value.message
    altered = _sign(_bundle())
    altered["entries"][0]["title"] = "changed after signing"
    with pytest.raises(BundleRejectedError):
        import_bundle(db_session, _who(), json.dumps(altered).encode())
    db_session.flush()
    assert db_session.query(ContentFeedEntry).count() == 0
    assert db_session.query(AuditLog).filter_by(action="content_feeds.bundle_rejected").count() == 2


def test_a_signed_document_that_is_not_a_feed_bundle_names_the_offending_field(db_session, bundle_mode):
    doc = _sign({**_bundle(), "applies_to": "everything"})  # an unknown field, signed
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(doc).encode())
    db_session.flush()
    assert err.value.status == 422 and "not a feed bundle (" in err.value.message
    assert db_session.query(ContentFeedEntry).count() == 0


def test_import_fails_closed_without_bundle_mode_or_a_key(db_session, bundle_mode, monkeypatch):
    monkeypatch.setenv("TN_FEEDS_MODE", "online")
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(_sign(_bundle())).encode())
    assert err.value.status == 409 and "only when feeds are in bundle mode" in err.value.message
    monkeypatch.setenv("TN_FEEDS_MODE", "bundle")
    monkeypatch.delenv("TN_FEEDS_BUNDLE_PUBLIC_KEY")
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(_sign(_bundle())).encode())
    assert err.value.status == 409 and "No feed bundle public key is configured" in err.value.message
    db_session.flush()
    assert db_session.query(ContentFeedEntry).count() == 0


def test_an_oversize_bundle_is_refused_before_it_is_parsed(db_session, bundle_mode, monkeypatch):
    monkeypatch.setattr(service, "MAX_BUNDLE_BYTES", 100)
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(_sign(_bundle())).encode())
    db_session.flush()
    assert err.value.status == 413 and "larger than" in err.value.message
    assert db_session.get(ContentFeedSource, "bundle").last_error


def test_a_bundle_dated_beyond_the_clock_skew_is_refused(db_session, bundle_mode):
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(_sign(_bundle(NOW + timedelta(days=2)))).encode())
    assert err.value.status == 422 and "more than a day in the future" in err.value.message


def test_a_bundle_older_than_the_last_import_is_refused(db_session, bundle_mode):
    import_bundle(db_session, _who(), json.dumps(_sign(_bundle(NOW))).encode())
    db_session.flush()
    newer_key = _bundle(
        NOW - timedelta(days=3),
        entries=[
            {
                "key": "nvd:CVE-2026-9",
                "source": "nvd",
                "kind": "vulnerability",
                "products": ["cpe:x"],
                "at": "2026-10-01T00:00:00+00:00",
                "title": "old news",
                "data": {},
            }
        ],
    )
    with pytest.raises(BundleRejectedError) as err:
        import_bundle(db_session, _who(), json.dumps(_sign(newer_key)).encode())
    db_session.flush()
    assert err.value.status == 409 and "older than the one last imported" in err.value.message
    assert db_session.query(ContentFeedEntry).filter_by(entry_key="nvd:CVE-2026-9").count() == 0
