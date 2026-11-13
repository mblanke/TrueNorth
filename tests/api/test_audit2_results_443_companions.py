"""Companion oracles for the audit #443 results-area findings (card #468, item 2).

One companion per strict-xfail oracle in ``test_audit2_results_443.py``: a second input
that pins the same secure behaviour from another angle — a second subject or second
tenant, a boundary value, or the same hole reached through a different code path. Each
companion fails at base for the same reason as its oracle (``xfail(strict=True)``) and
turns green with the same fix. Fixtures are imported from, or copied after, the
neighbouring test modules; the app itself is untouched.

The ``audit443`` mark is deliberately unregistered (integrator decision on #444–#450);
the PytestUnknownMarkWarning it emits is expected.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
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
from test_cmi5_lms import AU_CRED, SERVER_CRED
from test_cmi5_lms import _launch as cmi5_launch
from test_cmi5_lms import _user as cmi5_user
from test_cmi5_lms import acting_as as cmi5_acting_as
from test_content_feeds_api import DEV, SITE_KEY, _b64_public
from test_content_feeds_api import acting_as as feeds_acting_as
from test_content_feeds_api import bundle as feed_bundle
from test_content_feeds_api import sign as feed_sign
from test_content_feeds_api import upload as feed_upload
from test_lti13 import _launch as lti_launch
from test_lti13 import _platform as lti_platform
from test_result_transfer_service import TENANT, USER
from test_result_transfer_service import claim as rt_claim
from test_result_transfer_service import release as rt_release

LAUNCHED_VERB = "http://adlnet.gov/expapi/verbs/launched"
DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


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


# ---- f30 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: cmi5 AU LRS proxy runs blocking httpx on the event loop while holding row "
           "locks (control-plane/api/app/cmi5/router.py:303) — companion",
)
def test_f30_companion_an_independent_request_is_not_starved_behind_the_lrs_proxy(client, lrs, release, student):
    """Same finding, user-visible angle: while the proxy's backend LRS call is held in
    flight, a second, unrelated request on the same event loop must still complete."""
    out = cmi5_launch(client, student, release)
    au = AU(client, out["url"], student)
    au.fetch()
    entered, release_call = threading.Event(), threading.Event()
    real_request = lrs.xapi_request

    def probe(*args, **kwargs):
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
    assert entered.wait(timeout=10), "the proxy never reached its LRS backend call"

    second: dict = {}

    def independent():
        second["response"] = client.get("/openapi.json")

    bystander = threading.Thread(target=independent)
    bystander.start()
    bystander.join(timeout=2.0)
    starved = bystander.is_alive()

    release_call.set()
    worker.join(timeout=10)
    bystander.join(timeout=10)
    assert not starved, "an unrelated request starved behind the proxy's in-flight LRS call"
    assert "error" not in box, box.get("error")
    assert second.get("response") is not None and second["response"].status_code == 200
    assert box.get("response") is not None and box["response"].status_code == 200


# ---- f32 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: unguarded server-side fetches to tenant-admin-controlled platform URLs "
           "bypass net_guard (control-plane/api/app/lti13.py:233) — companion",
)
def test_f32_companion_a_second_tenants_loopback_jwks_url_is_refused_before_any_fetch(db_session, monkeypatch):
    """Same finding, two-tenant matrix and boundary value: a platform registered by another
    tenant points its JWKS URL at loopback — refused always, even with private destinations
    allowed — and the guard must refuse it before any request is made."""
    other_tenant = uuid.uuid4()
    db_session.add(Tenant(id=other_tenant, name="Other enclave tenant", slug=f"other-{other_tenant.hex[:8]}"))
    db_session.flush()
    platform = lti_platform(db_session, tenant=other_tenant)
    platform.lti_jwks_url = "http://127.0.0.1:9/jwks.json"
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


# ---- f33 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: a withdrawn Student keeps launch/content/grade; only the LRS proxy checks "
           "withdrawal (control-plane/api/app/cmi5/lms.py:211) — companion",
)
def test_f33_companion_a_student_withdrawn_after_their_first_launch_cannot_relaunch(
    client, db_session, lrs, release, student
):
    """Same finding, second input: the withdrawal comes after a registration already exists
    (the realistic mid-course case). The relaunch must still be refused."""
    cmi5_launch(client, student, release)  # the first launch succeeds and creates the registration
    enrolled = db_session.query(Enrollment).filter(Enrollment.user_id == student.id).one()
    enrolled.status = EnrollmentStatus.withdrawn
    db_session.commit()
    with cmi5_acting_as(student):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={})
    assert r.status_code == 403, r.text


# ---- f34 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: two concurrent first results for the same attempt both land; no uniqueness "
           "on attempt (control-plane/api/app/result_transfer/service.py:285) — companion",
)
def test_f34_companion_the_database_itself_refuses_a_second_first_result_for_one_attempt(db_session, monkeypatch):
    """Same finding, behavioural angle: the schema introspection of the oracle says a unique
    constraint must exist; here the database must actually refuse the second insert for one
    (source_enclave, attempt_id)."""
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    rel = rt_release(db_session)
    binding = bind_subject(
        db_session, tenant_id=TENANT, source_enclave="unclass", opaque_subject_id=uuid.uuid4(), user_id=USER
    )
    common = {
        "tenant_id": TENANT,
        "subject_binding_id": binding.id,
        "user_id": USER,
        "source_enclave": "unclass",
        "course_release_id": rel.id,
        "assessment_id": "final",
        "attempt_id": uuid.uuid4(),
        "raw_score": 88,
        "max_score": 100,
        "outcome": "pass",
        "scorer_version": "scenario-v4",
        "occurred_at": datetime(2026, 10, 23, 12, tzinfo=UTC),
    }
    db_session.add(ImportedResult(package_id=uuid.uuid4(), package_sha256="a" * 64, **common))
    db_session.add(ImportedResult(package_id=uuid.uuid4(), package_sha256="b" * 64, **common))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---- f36 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: no AU launch limit; one student can flood the LRS "
           "(control-plane/api/app/cmi5/lms.py:340) — companion",
)
def test_f36_companion_a_student_cannot_flood_the_lrs_across_two_aus(client, lrs, release, student):
    """Same finding, boundary of the cap's scope: the limit has to be per student, not per
    AU — one student spreading a burst over AU 0 and AU 1 of one release must still be
    capped."""
    codes = []
    for au_index in (0, 0, 0, 1, 1):
        with cmi5_acting_as(student):
            codes.append(client.post(f"/cmi5/releases/{release['id']}/aus/{au_index}/launch", json={}).status_code)
    launched = len(lrs.by_verb(LAUNCHED_VERB))
    assert launched < len(codes) or any(code != 200 for code in codes), (
        f"all {len(codes)} launches across two AUs succeeded and each wrote a `launched` statement ({codes})"
    )


# ---- f38 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: cross-tenant package_id reuse ends in an unhandled IntegrityError "
           "(control-plane/api/app/result_transfer/service.py:252) — companion",
)
def test_f38_companion_the_reversed_cross_tenant_package_id_reuse_is_a_mapped_refusal(db_session, monkeypatch):
    """Same finding, the other diagonal of the two-tenant matrix: the other enclave imports
    the shared package_id first and the home enclave second — the second import must still
    be refused, not blow up on the primary key."""
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

    import_result(
        db_session,
        decrypted(rt_claim(theirs.id, other_token, uuid.uuid4(), tenant_id=other_tenant)),
        package_sha256="5" * 64,
    )
    try:
        import_result(db_session, decrypted(rt_claim(mine.id, token, uuid.uuid4())), package_sha256="4" * 64)
    except ResultTransferError:
        pass  # the mapped refusal the second import must get
    except IntegrityError as exc:
        raise AssertionError(f"package_id reuse hit the primary key as a raw IntegrityError: {exc}") from exc


# ---- f39 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: concurrent bundle imports race the check-then-insert store in _store; unhandled "
           "IntegrityError (control-plane/api/app/content_feeds/service.py:235) — companion",
)
def test_f39_companion_three_concurrent_bundle_imports_neither_crash_nor_lose(monkeypatch, tmp_path):
    """Same finding, boundary value: three racers instead of two — the loser(s) of the
    check-then-insert race must come back as a mapped refusal, never a raw IntegrityError."""
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
    barrier = threading.Barrier(3)

    def meet_at_first_flush(session, flush_context, instances):
        """All sessions reach _store's flush having seen no existing row: the race window."""
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
        workers = [threading.Thread(target=run, args=(i,)) for i in range(3)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=60)
    finally:
        event.remove(SASession, "before_flush", meet_at_first_flush)
        eng.dispose()

    assert set(outcomes) == {0, 1, 2}, outcomes
    assert set(outcomes.values()) <= {"imported", "refused"}, (
        f"a concurrent import surfaced a raw database error instead of a refusal: {outcomes}"
    )


# ---- f41 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: first cmi5 launch races to a PK-collision 500 "
           "(control-plane/api/app/cmi5/lms.py:228) — companion",
)
def test_f41_companion_another_students_first_launch_racing_another_does_not_500(
    client, db_session, lrs, release, student, monkeypatch
):
    """Same finding, second subject: a second student's first launch loses the same race and
    must also get the existing/created registration, not a raw IntegrityError (a 500)."""
    second = cmi5_user(db_session)
    ensure_enrollment(db_session, user_id=second.id, course_id=uuid.UUID(release["course_id"]), tenant_id=DEV_TENANT)
    db_session.commit()
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
        out = cmi5_launch(client, second, release)
    except IntegrityError as exc:
        raise AssertionError(f"another student's first launch surfaced a raw IntegrityError (a 500): {exc}") from exc
    enrolled = db_session.query(Enrollment).filter(Enrollment.user_id == second.id).one()
    assert out["registration"] == str(enrolled.id)


# ---- f42 companion --------------------------------------------------------------------------
@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: bundle takes ownership of online-origin entries and the next bundle deletes "
           "them (control-plane/api/app/content_feeds/service.py:243) — companion",
)
def test_f42_companion_a_bundle_refresh_must_not_rewrite_an_online_entrys_origin(client, db_session, bundle_mode):
    """Same finding, the mechanism itself rather than its delete consequence: a bundle may
    refresh an online-origin entry's content but must not rewrite its origin to 'bundle'."""
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
    with feeds_acting_as(db_session, UserRole.admin):
        r = feed_upload(
            client, feed_sign(feed_bundle(datetime.now(UTC), entries=[feed_bundle()["entries"][0]]))
        )
        assert r.status_code == 200, r.text
    row = db_session.query(ContentFeedEntry).filter(ContentFeedEntry.entry_key == "eol:ubuntu:20.04").one()
    assert row.origin == "online", (
        f"the bundle took ownership of the online-origin entry (origin is now {row.origin!r}); "
        f"the next bundle without that key would delete online-sourced knowledge"
    )
