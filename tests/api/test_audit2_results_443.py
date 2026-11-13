"""Strict-xfail oracle tests for audit #443 findings (write card TN10X-AUDIT2-tests-results).

One test per finding that reproduces on the current code: each fails today, is expected to
fail (``xfail(strict=True)``, so an unexpected pass errors out), and turns green once the
finding is fixed — no assertion is tied to today's broken behaviour. The card's HOLD-CAVEAT
findings get no test: f37 and f40 could not be stated as an unambiguous secure behaviour at
base, and for f45 and f48 no finding text is available in this checkout (returned UNKNOWN).
The ``audit443`` mark is deliberately unregistered (integrator decision on #444–#450:
pytest.ini is protected); the PytestUnknownMarkWarning it emits is expected.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from _cmi5_kit import AU, BUNDLE, CATALOGUE, CROSSWALK, MemoryLRS
from app import lti13
from app.auth import CurrentUser
from app.cmi5 import lms
from app.cmi5.models import Cmi5Registration
from app.content_feeds import ContentFeedEntry, ContentFeedSource
from app.content_feeds import service as feed_service
from app.db import Base, get_db
from app.enrollment import ensure_enrollment
from app.models import AuditLog, Enrollment, EnrollmentStatus, Tenant, User, UserRole
from app.net_guard import DestinationRefusedError
from app.result_transfer.contract import (
    ResultTransferError,
    create_package,
    decrypt_package,
    generate_recipient_keypair,
    generate_signing_keypair,
)
from app.result_transfer.models import ImportedResult
from app.result_transfer.service import bind_subject, import_result
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import UniqueConstraint
from test_cmi5_lms import _launch as cmi5_launch
from test_cmi5_lms import _user as cmi5_user
from test_cmi5_lms import acting_as as cmi5_acting_as
from test_content_feeds_api import DEV, SITE_KEY, _b64_public, _entries
from test_content_feeds_api import acting_as as feeds_acting_as
from test_content_feeds_api import bundle as feed_bundle
from test_content_feeds_api import sign as feed_sign
from test_content_feeds_api import upload as feed_upload
from test_lti13 import _launch as lti_launch
from test_lti13 import _platform as lti_platform
from test_result_transfer_service import TENANT, USER
from test_result_transfer_service import claim as rt_claim
from test_result_transfer_service import release as rt_release

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
LAUNCHED_VERB = "http://adlnet.gov/expapi/verbs/launched"
AU_CRED = "YXUta2V5OmF1LXNlY3JldA=="  # base64 "au-key:au-secret": the AU traffic's own credential
SERVER_CRED = "c2VydmVyOnNlY3JldA=="  # base64 "server:secret": TrueNorth's LRS_AUTH


# ---- shared fixtures (copied from tests/api/test_cmi5_lms.py / test_content_feeds_api.py) ----
@pytest.fixture(autouse=True)
def _lms_credentials(monkeypatch):
    """cmi5 launch and LRS proxy read their basic-auth credentials from the env."""
    monkeypatch.setenv("CMI5_LRS_AUTH", AU_CRED)
    monkeypatch.setenv("LRS_AUTH", SERVER_CRED)


@pytest.fixture
def lrs(monkeypatch):
    mem = MemoryLRS()
    monkeypatch.setattr(lms, "get_lms_backend", lambda: mem)
    return mem


@pytest.fixture
def release(client):
    """C105, uploaded and accepted by the dev admin (AUTH_DISABLED), then published."""
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    r = client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    assert r.status_code == 200, r.text
    r = client.post("/course-releases", files={"file": (BUNDLE.name, BUNDLE.read_bytes(), "application/gzip")})
    assert r.status_code in (200, 201), r.text
    rel = r.json()
    if rel["state"] == "candidate":
        acks = [a["id"] for a in rel["open_actions"]]
        r = client.post(f"/course-releases/{rel['id']}/accept", json={"acknowledge_actions": acks})
        assert r.status_code == 200, r.text
        rel = r.json()
    from app.models import Course

    db = client.app.dependency_overrides[get_db]().__next__()
    db.get(Course, uuid.UUID(rel["course_id"])).is_published = True
    db.commit()
    return rel


@pytest.fixture
def student(db_session, release):
    u = cmi5_user(db_session)
    ensure_enrollment(db_session, user_id=u.id, course_id=uuid.UUID(release["course_id"]), tenant_id=DEV_TENANT)
    db_session.commit()
    return u


@pytest.fixture
def bundle_mode(monkeypatch):
    monkeypatch.setenv("TN_FEEDS_MODE", "bundle")
    monkeypatch.setenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", _b64_public(SITE_KEY))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))


# ---- f30 ------------------------------------------------------------------------------------
@pytest.mark.audit443
def test_f30_lrs_proxy_keeps_the_event_loop_responsive(client, lrs, release, student):
    """While the proxy's backend LRS call is in flight, the event loop it runs on must stay
    responsive (or the call must not run on a loop at all)."""
    out = cmi5_launch(client, student, release)
    au = AU(client, out["url"], student)
    au.fetch()
    seen: dict = {"loop": "unset"}
    entered, release_call = threading.Event(), threading.Event()
    real_request = lrs.xapi_request

    def probe(*args, **kwargs):
        try:
            seen["loop"] = asyncio.get_running_loop()
        except RuntimeError:
            seen["loop"] = None
        entered.set()
        release_call.wait(timeout=10)
        return real_request(*args, **kwargs)

    lrs.xapi_request = probe
    box: dict = {}

    def call():
        try:
            box["response"] = au.lrs("GET", "activities/state", params=au.state_params("LMS.LaunchData"))
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            box["error"] = exc

    worker = threading.Thread(target=call)
    worker.start()
    try:
        assert entered.wait(timeout=10), "the proxy never reached its LRS backend call"
        if seen["loop"] is not None:
            marker: list[int] = []
            seen["loop"].call_soon_threadsafe(marker.append, 1)
            time.sleep(1.0)
            assert marker, "the event loop was blocked inside the proxy's LRS call"
    finally:
        release_call.set()
        worker.join(timeout=10)
    assert "error" not in box, box.get("error")
    assert box.get("response") is not None and box["response"].status_code == 200


# ---- f32 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: unguarded server-side fetches to tenant-admin-controlled platform URLs "
           "bypass net_guard (control-plane/api/app/lti13.py:233)",
)
def test_f32_lti13_jwks_fetches_go_through_net_guard(db_session, monkeypatch):
    """The JWKS URL is tenant-admin-controlled; a private (metadata) address must be refused
    by net_guard before any request is made, like the other platform fetches."""
    platform = lti_platform(db_session, tenant=DEV_TENANT)
    platform.lti_jwks_url = "http://169.254.169.254/latest/meta-data/"
    db_session.flush()
    token, state = lti_launch(db_session, platform)

    class NoFetch:
        """Stands in for httpx.AsyncClient: any real fetch means the guard was bypassed."""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, *args, **kwargs):
            raise AssertionError("lti13 fetched the platform JWKS URL without going through net_guard")

    monkeypatch.setattr(lti13, "httpx", SimpleNamespace(AsyncClient=NoFetch))
    with pytest.raises(DestinationRefusedError):
        asyncio.run(lti13.validate_launch(db_session, token, state))


# ---- f33 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: a withdrawn Student keeps launch/content/grade; only the LRS proxy checks "
           "withdrawal (control-plane/api/app/cmi5/lms.py:211)",
)
def test_f33_a_withdrawn_student_cannot_launch(client, db_session, lrs, release, student):
    """enrolment_for never reads the enrolment status, so a withdrawn student still gets a
    launch URL (and with it LRS access); withdrawal must refuse the launch."""
    enrolled = db_session.query(Enrollment).filter(Enrollment.user_id == student.id).one()
    enrolled.status = EnrollmentStatus.withdrawn
    db_session.commit()
    with cmi5_acting_as(student):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={})
    assert r.status_code == 403, r.text


# ---- f34 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: two concurrent first results for the same attempt both land; no uniqueness "
           "on attempt (control-plane/api/app/result_transfer/service.py:285)",
)
def test_f34_one_attempt_cannot_hold_two_first_results():
    """The schema must back the attempt dedup with a unique constraint or unique index
    covering (source_enclave, attempt_id), so the database refuses the second insert."""
    table = ImportedResult.__table__
    needed = {"source_enclave", "attempt_id"}
    uniques = [
        c.name or "unnamed"
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and needed <= {col.name for col in c.columns}
    ] + [i.name or "unnamed" for i in table.indexes if i.unique and needed <= {col.name for col in i.columns}]
    assert uniques, (
        f"no unique constraint or unique index covering (source_enclave, attempt_id) on imported_results;"
        f" indexes: {[i.name for i in table.indexes]}"
    )


# ---- f36 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: no AU launch limit; one student can flood the LRS "
           "(control-plane/api/app/cmi5/lms.py:340)",
)
def test_f36_au_launches_are_limited(client, lrs, release, student):
    """Five back-to-back launches must not all succeed and each record a `launched`
    statement: some cap or cooldown has to intervene."""
    codes = []
    for _ in range(5):
        with cmi5_acting_as(student):
            codes.append(client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={}).status_code)
    launched = len(lrs.by_verb(LAUNCHED_VERB))
    assert launched < len(codes) or any(code != 200 for code in codes), (
        f"all {len(codes)} launches succeeded and each wrote a `launched` statement ({codes})"
    )


# ---- f38 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: cross-tenant package_id reuse ends in an unhandled IntegrityError "
           "(control-plane/api/app/result_transfer/service.py:252)",
)
def test_f38_cross_tenant_package_id_reuse_is_a_mapped_refusal(db_session, monkeypatch):
    """The dedup lookup names the tenant but the primary key does not: two tenants reusing one
    package_id collide on it. The second import must be refused, not blow up."""
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    other_tenant, other_user = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=other_tenant, name="Other enclave tenant", slug=f"other-{other_tenant.hex[:8]}"))
    db_session.flush()
    db_session.add(
        User(
            id=other_user,
            keycloak_id=f"other-{other_user}",
            email=f"{other_user}@example.test",
            display_name="Other learner",
            role=UserRole.student,
            tenant_id=other_tenant,
        )
    )
    db_session.flush()
    mine = rt_release(db_session)
    theirs = rt_release(db_session, tenant_id=other_tenant, marker="9")
    token, other_token = uuid.uuid4(), uuid.uuid4()
    bind_subject(db_session, tenant_id=TENANT, source_enclave="unclass", opaque_subject_id=token, user_id=USER)
    bind_subject(
        db_session,
        tenant_id=other_tenant,
        source_enclave="unclass",
        opaque_subject_id=other_token,
        user_id=other_user,
    )
    signing_private, signing_public = generate_signing_keypair()
    recipient_private, recipient_public = generate_recipient_keypair()
    shared_package_id = uuid.uuid4()

    def decrypted(claim):
        package, _ = create_package(
            claim,
            source_enclave="unclass",
            destination_enclave="secret",
            signing_private_key=signing_private,
            recipient_public_key=recipient_public,
            package_id=shared_package_id,
        )
        return decrypt_package(
            package,
            trusted_signing_public_key=signing_public,
            recipient_private_key=recipient_private,
            destination_enclave="secret",
        )

    import_result(db_session, decrypted(rt_claim(mine.id, token, uuid.uuid4())), package_sha256="4" * 64)
    try:
        import_result(
            db_session,
            decrypted(rt_claim(theirs.id, other_token, uuid.uuid4(), tenant_id=other_tenant)),
            package_sha256="5" * 64,
        )
    except ResultTransferError:
        pass  # the mapped refusal the second tenant's import must get
    except IntegrityError as exc:
        raise AssertionError(f"package_id reuse hit the primary key as a raw IntegrityError: {exc}") from exc


# ---- f39 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: concurrent bundle imports race the check-then-insert store; unhandled "
           "IntegrityError (control-plane/api/app/content_feeds/service.py:204)",
)
def test_f39_concurrent_bundle_imports_neither_crash_nor_lose(monkeypatch, tmp_path):
    """Two first imports of the same entry, released together at _store's check-then-insert:
    the loser must come back as a mapped refusal (or wait and win), never a raw IntegrityError."""
    monkeypatch.setenv("TN_FEEDS_MODE", "bundle")
    monkeypatch.setenv("TN_FEEDS_BUNDLE_PUBLIC_KEY", _b64_public(SITE_KEY))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
    raw = json.dumps(feed_sign(feed_bundle(entries=[feed_bundle()["entries"][0]]))).encode()
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email="importer@example.test",
        display_name="Importer",
        role=UserRole.admin,
        tenant_id=str(DEV),
        keycloak_id="kc-importer",
    )

    eng = create_engine(f"sqlite:///{tmp_path}/feeds.db")
    Base.metadata.create_all(
        bind=eng, tables=[ContentFeedEntry.__table__, ContentFeedSource.__table__, AuditLog.__table__]
    )
    barrier = threading.Barrier(2)

    def meet_at_first_flush(session, flush_context, instances):
        """Both sessions reach _store's flush having seen no existing row: the race window."""
        if session.info.get("feed_race") and not session.info.get("met"):
            session.info["met"] = True
            barrier.wait(timeout=30)

    event.listen(SASession, "before_flush", meet_at_first_flush)
    outcomes: dict[int, str] = {}

    def run(i: int) -> None:
        s = sessionmaker(bind=eng)()
        s.info["feed_race"] = True
        try:
            feed_service.import_bundle(s, who, raw)
            s.commit()
            outcomes[i] = "imported"
        except feed_service.BundleRejectedError:
            s.rollback()
            outcomes[i] = "refused"
        except Exception as exc:  # noqa: BLE001 - the whole point is what escapes
            s.rollback()
            outcomes[i] = type(exc).__name__
        finally:
            s.close()

    try:
        workers = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)
    finally:
        event.remove(SASession, "before_flush", meet_at_first_flush)
        eng.dispose()

    assert set(outcomes) == {0, 1}, outcomes
    assert set(outcomes.values()) <= {"imported", "refused"}, (
        f"a concurrent import surfaced a raw database error instead of a refusal: {outcomes}"
    )


# ---- f41 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: first cmi5 launch races to a PK-collision 500 "
           "(control-plane/api/app/cmi5/lms.py:228)",
)
def test_f41_a_first_launch_racing_another_does_not_500(client, db_session, lrs, release, student, monkeypatch):
    """When the registration insert hits its primary key (another first launch won), the
    launch must still return that registration, not surface the IntegrityError as a 500."""
    real_flush = db_session.flush
    raced = {"armed": True}

    def racing_flush(*args, **kwargs):
        if raced["armed"] and any(isinstance(o, Cmi5Registration) for o in db_session.new):
            raced["armed"] = False
            raise IntegrityError(
                "INSERT INTO cmi5_registrations (id) VALUES (?)",
                [],
                sqlite3.IntegrityError("UNIQUE constraint failed: cmi5_registrations.id"),
            )
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", racing_flush)
    try:
        out = cmi5_launch(client, student, release)
    except IntegrityError as exc:
        raise AssertionError(f"the first launch surfaced a raw IntegrityError (a 500): {exc}") from exc
    enrolled = db_session.query(Enrollment).filter(Enrollment.user_id == student.id).one()
    assert out["registration"] == str(enrolled.id)


# ---- f42 ------------------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: bundle takes ownership of online-origin entries and the next bundle deletes "
           "them (control-plane/api/app/content_feeds/service.py:243)",
)
def test_f42_a_bundle_does_not_take_ownership_of_online_entries(client, db_session, bundle_mode):
    """A bundle may update what the online pull stored, but must not rewrite its origin to
    'bundle' — the next bundle without that key then deletes online-sourced knowledge."""
    now = datetime.now(UTC)
    db_session.add(
        ContentFeedEntry(
            id=uuid.uuid4(),
            entry_key="eol:ubuntu:20.04",
            source="endoflife",
            kind="release",
            products=["eol:ubuntu:20.04"],
            at=now,
            title="",
            data={},
            origin="online",
            first_seen_at=now,
            updated_at=now,
        )
    )
    db_session.commit()
    first = datetime.now(UTC)
    with feeds_acting_as(db_session, UserRole.admin):
        assert feed_upload(client, feed_sign(feed_bundle(first, entries=[feed_bundle()["entries"][0]]))).status_code == 200
        assert feed_upload(client, feed_sign(feed_bundle(first + timedelta(minutes=1), entries=[]))).status_code == 200
    assert "eol:ubuntu:20.04" in _entries(db_session)


# ---- HOLD-CAVEAT findings f37, f40, f45, f48: no test -------------------------------------------------
# The card's four HOLD-CAVEAT findings all get no test here. f37 (cmi5/rules.py:354,
# moveOn=Passed without masteryScore) and f40 (result_transfer/service.py:377, no
# rejected->acknowledged outbox path) could not be stated as an unambiguous secure
# behaviour at base. For f45 and f48 no finding text exists in this checkout, so no
# secure behaviour can be stated for them either. See the card's return for the attempts.
