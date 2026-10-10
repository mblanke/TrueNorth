"""Re-issuing pre-switch statements (app/xapi_reissue.py) and the LRS resource capability.

The live round trip against a real lrsql is tests/integration/test_cmi5_lrs.py; here the
LRS is a fake, so these pin the rules: a copy never reuses the original id, a re-run
stores nothing new, the original is referenced and never edited, voiding is opt-in and
needs --apply, and a backend with no LRS refuses rather than reporting success.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx
from app import xapi, xapi_reissue
from app.lms import LRSResponse, LRSUnavailableError, LRSUnsupportedError, NullLMSBackend, XAPILRSBackend
from app.models import Tenant, User, UserRole
from app.xapi_identity import XapiLegacyIdentity

LEGACY = {
    "id": "5b0a1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d",
    "actor": {"objectType": "Agent", "name": "Pat Student", "mbox": "mailto:pat@example.mil"},
    "verb": {"id": "http://adlnet.gov/expapi/verbs/completed", "display": {"en-US": "completed"}},
    "object": {
        "objectType": "Activity",
        "id": "http://truenorthrange.local/exercise/ex-1",
        "definition": {"type": "http://truenorthrange.local/activity-types/exercise", "name": {"en-US": "Drill"}},
    },
    "result": {"completion": True},
    "context": {"platform": "TrueNorth Range", "extensions": {"http://truenorthrange.local/extensions/range_id": "r"}},
    "timestamp": "2026-09-01T10:00:00+00:00",
    "stored": "2026-09-01T10:00:01+00:00",
    "authority": {"objectType": "Agent", "account": {"homePage": "http://lrs", "name": "key"}},
    "version": "1.0.0",
}


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("XAPI_IRI_BASE", "https://range.example.mil/xapi")
    monkeypatch.setenv("XAPI_ACCOUNT_HOMEPAGE", "https://range.example.mil")


def test_reissue_maps_identity_and_iris_and_points_back():
    user = uuid.uuid4()
    copy = xapi_reissue.reissue(LEGACY, user)
    assert copy["id"] == xapi_reissue.reissued_id(LEGACY["id"]) != LEGACY["id"]
    assert copy["actor"] == {
        "objectType": "Agent",
        "account": {"homePage": "https://range.example.mil", "name": str(user)},
    }
    assert copy["object"]["id"] == "https://range.example.mil/xapi/activities/exercise/ex-1"
    assert copy["object"]["definition"]["type"] == "https://range.example.mil/xapi/activity-types/exercise"
    assert copy["context"]["statement"] == {"objectType": "StatementRef", "id": LEGACY["id"]}
    exts = copy["context"]["extensions"]
    assert exts["https://range.example.mil/xapi/extensions/range_id"] == "r"
    assert exts[xapi.extension_iri("reissued-from")] == LEGACY["id"]
    assert copy["timestamp"] == LEGACY["timestamp"]  # when it happened, not when it was copied
    for server_set in ("stored", "authority", "version"):
        assert server_set not in copy
    assert "mailto:" not in json.dumps(copy)
    assert LEGACY["actor"]["mbox"] == "mailto:pat@example.mil"  # the original is never edited


def test_reissue_is_deterministic():
    user = uuid.uuid4()
    assert xapi_reissue.reissue(LEGACY, user) == xapi_reissue.reissue(LEGACY, user)


class FakeLRS(XAPILRSBackend):
    """Pages a legacy query in two, and remembers every PUT."""

    def __init__(self, statements):
        super().__init__(lrs_url="http://lrs.invalid", lrs_auth="")
        self.statements = statements
        self.stored: dict[str, dict] = {}

    def xapi_request(self, method, resource, *, params=None, body=None, headers=None, timeout=10.0):
        if method == "GET" and resource == "statements" and "more" not in (params or {}):
            page = {"statements": self.statements[:1], "more": "/xapi/statements?more=page2"}
            return LRSResponse(200, json.dumps(page).encode())
        if method == "GET" and resource == "statements":
            return LRSResponse(200, json.dumps({"statements": self.statements[1:], "more": ""}).encode())
        if method == "PUT" and resource == "statements":
            stmt = json.loads(body)
            if stmt["id"] in self.stored and self.stored[stmt["id"]] != stmt:
                return LRSResponse(409)
            self.stored[stmt["id"]] = stmt
            return LRSResponse(204)
        raise AssertionError(f"unexpected {method} {resource}")


@pytest.fixture
def legacy_user(db_session):
    tenant = Tenant(id=uuid.uuid4(), name="reissue", slug=f"re-{uuid.uuid4().hex[:6]}")
    user = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email="pat@example.mil",
        display_name="Pat",
        role=UserRole.student,
        tenant_id=tenant.id,
    )
    db_session.add_all([tenant, user])
    db_session.flush()
    db_session.add(XapiLegacyIdentity(user_id=user.id, legacy_mbox="mailto:pat@example.mil"))
    db_session.flush()
    return user


def _two_legacy():
    second = {**LEGACY, "id": "6c1b2d3e-4f5a-4b6c-9d7e-8f9a0b1c2d3e"}
    return [LEGACY, second]


def test_dry_run_counts_and_sends_nothing(db_session, legacy_user):
    lrs = FakeLRS(_two_legacy())
    report = xapi_reissue.run(db_session, lrs)
    assert (report.users, report.found, report.reissued, report.voided) == (1, 2, 0, 0)
    assert lrs.stored == {}


def test_apply_reissues_once_and_a_rerun_adds_nothing(db_session, legacy_user):
    lrs = FakeLRS(_two_legacy())
    first = xapi_reissue.run(db_session, lrs, apply=True, authority="key")
    assert (first.reissued, first.voided, first.errors) == (2, 0, [])
    stored = dict(lrs.stored)
    assert {s["actor"]["account"]["name"] for s in stored.values()} == {str(legacy_user.id)}
    again = xapi_reissue.run(db_session, lrs, apply=True, authority="key")
    assert again.errors == [] and lrs.stored == stored


def test_void_is_opt_in_and_needs_apply(db_session, legacy_user):
    lrs = FakeLRS(_two_legacy())
    with pytest.raises(ValueError):
        xapi_reissue.run(db_session, lrs, void=True)
    report = xapi_reissue.run(db_session, lrs, apply=True, void=True, authority="key")
    assert report.voided == 2
    voids = [s for s in lrs.stored.values() if s["verb"]["id"] == xapi_reissue.VOIDED]
    assert sorted(v["object"]["id"] for v in voids) == sorted(s["id"] for s in _two_legacy())


def test_only_truenorths_own_legacy_statements_are_touched(db_session, legacy_user):
    """Review low 7: the old mbox alone proves nothing (anyone with an LRS credential could
    have used it): only statements under the legacy IRI prefix and written by TrueNorth's
    own credential (the authority named) are re-issued or voided."""
    foreign_authority = {
        **LEGACY,
        "id": str(uuid.uuid4()),
        "authority": {"account": {"homePage": "http://lrs", "name": "other"}},
    }
    foreign_object = {
        **LEGACY,
        "id": str(uuid.uuid4()),
        "object": {"objectType": "Activity", "id": "https://elsewhere/x"},
    }
    lrs = FakeLRS([LEGACY, foreign_authority, foreign_object])
    dry = xapi_reissue.run(db_session, lrs)
    assert dry.authorities == {"key": 1, "other": 1} and dry.skipped == 1  # the operator sees who wrote what
    with pytest.raises(ValueError):
        xapi_reissue.run(db_session, lrs, apply=True)  # must name TrueNorth's authority
    report = xapi_reissue.run(db_session, lrs, apply=True, void=True, authority="key")
    assert (report.reissued, report.voided, report.skipped) == (1, 1, 2)
    voided = {s["object"]["id"] for s in lrs.stored.values() if s["verb"]["id"] == xapi_reissue.VOIDED}
    assert voided == {LEGACY["id"]}


def test_a_backend_without_an_lrs_refuses(db_session, legacy_user):
    with pytest.raises(RuntimeError):
        xapi_reissue.run(db_session, NullLMSBackend())
    with pytest.raises(LRSUnsupportedError):
        NullLMSBackend().xapi_request("GET", "statements")


@respx.mock
def test_xapi_lrs_request_sends_its_own_credential_only():
    route = respx.get("http://lrs.test/xapi/activities/state").mock(
        return_value=httpx.Response(200, json={"a": 1}, headers={"ETag": '"abc"', "Set-Cookie": "x=1"})
    )
    backend = XAPILRSBackend(lrs_url="http://lrs.test/", lrs_auth="c2VydmVyOnNlY3JldA==")
    resp = backend.xapi_request(
        "GET",
        "activities/state",
        params={"stateId": "LMS.LaunchData"},
        headers={"Authorization": "Basic stolen", "If-None-Match": "*", "Cookie": "s=1"},
    )
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Basic c2VydmVyOnNlY3JldA=="
    assert sent.headers["X-Experience-API-Version"] == "1.0.3"
    assert sent.headers["If-None-Match"] == "*" and "cookie" not in sent.headers
    assert resp.status == 200 and resp.json() == {"a": 1}
    assert resp.headers == {"content-type": "application/json", "etag": '"abc"'}


@respx.mock
def test_xapi_lrs_request_unreachable_is_an_error_not_a_status():
    respx.get("http://lrs.test/xapi/about").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LRSUnavailableError):
        XAPILRSBackend(lrs_url="http://lrs.test", lrs_auth="").xapi_request("GET", "about")


def test_xapi_lrs_request_refuses_path_tricks():
    with pytest.raises(ValueError):
        XAPILRSBackend(lrs_url="http://lrs.test", lrs_auth="").xapi_request("GET", "../admin")
