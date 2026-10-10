"""cmi5 against the real stack: TrueNorth's LMS side and the xAPI it writes to a real lrsql.

The cmi5 lane (CI job `cmi5`, ``ITEST_LRS=1 scripts/itest.sh``) runs these against the
itest API (AUTH_DISABLED: the caller is the dev admin, enrolled here on C105) and reads the
statements back from the LRS itself (lrsql on 127.0.0.1:18001), so nothing is taken on the
API's word. They are the LMS-side checks of ADL's CATAPULT LMS test suite that apply to an
LMS which launches only its own courses (docs/cmi5.md, "Conformance"): the launched
statement and its extensions, LMS.LaunchData, the one-time fetch, the AU's checked endpoint
(a refused statement never reaches the LRS), satisfied for the block with a runtime id and
the publisher id in grouping, abandoned on relaunch; and the legacy re-issue round trip.

Without an LRS they skip, unless ``CMI5_REQUIRE_LRS=1`` (the cmi5 lane), which makes a
missing LRS a failure.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytestmark = [pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "content/releases/c105-foundations-d0e83b9f6c1f.tar.gz"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
CROSSWALK = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"
LRS_URL = os.getenv("LRS_TEST_URL", "http://127.0.0.1:18001").rstrip("/")
LRS_AUTH = os.getenv("LRS_TEST_AUTH", "truenorth-lrs-key:truenorth-lrs-secret")
CMI5 = "https://w3id.org/xapi/cmi5/context/categories/cmi5"
MOVEON = "https://w3id.org/xapi/cmi5/context/categories/moveon"
EXT = "https://w3id.org/xapi/cmi5/context/extensions/"
ADL = "http://adlnet.gov/expapi/verbs/"


def _lrs_up() -> bool:
    try:
        return httpx.get(f"{LRS_URL}/health", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def lrs():
    if not _lrs_up():
        if os.getenv("CMI5_REQUIRE_LRS", "").lower() in ("1", "true", "yes"):
            pytest.fail(f"CMI5_REQUIRE_LRS is set but no LRS answers at {LRS_URL} (ITEST_LRS=1 scripts/itest.sh up)")
        pytest.skip(f"no LRS at {LRS_URL}; start it with ITEST_LRS=1 scripts/itest.sh up")
    auth = base64.b64encode(LRS_AUTH.encode()).decode()
    with httpx.Client(
        base_url=f"{LRS_URL}/xapi/",
        headers={"Authorization": f"Basic {auth}", "X-Experience-API-Version": "1.0.3"},
        timeout=30.0,
    ) as client:
        yield client


def _post_file(client, path: str, src: Path, content_type: str):
    with src.open("rb") as fh:
        return client.post(path, files={"file": (src.name, fh, content_type)})


@pytest.fixture(scope="module")
def release(api_client, lrs):
    """C105, imported and accepted (or already there on a kept stack), with the dev admin
    enrolled on it (the enrolment pins the release)."""
    for path, src in (("/qsp/import-crosswalk", CROSSWALK), ("/courses/import-programme", CATALOGUE)):
        r = _post_file(api_client, path, src, "text/csv")
        assert r.status_code == 200, f"{path} => {r.status_code} {r.text}"
    r = _post_file(api_client, "/course-releases", BUNDLE, "application/gzip")
    assert r.status_code in (200, 201), r.text
    rel = r.json()
    if rel["state"] == "candidate":
        acks = [a["id"] for a in rel["open_actions"]]
        r = api_client.post(f"/course-releases/{rel['id']}/accept", json={"acknowledge_actions": acks})
        assert r.status_code == 200, r.text
        rel = r.json()
    me = api_client.get("/users/me")
    assert me.status_code == 200, me.text
    r = api_client.post(
        f"/courses/{rel['course_id']}/enroll", json={"user_id": me.json()["id"], "course_id": rel["course_id"]}
    )
    assert r.status_code in (201, 409), r.text
    return rel


def _fresh_au(api_client, release) -> int:
    """An AU not yet satisfied in the dev admin's registration (a kept stack remembers)."""
    s = api_client.get(f"/cmi5/releases/{release['id']}/structure")
    assert s.status_code == 200, s.text
    assert s.json()["enrolled"] is True, s.json()
    free = [a["index"] for a in s.json()["aus"] if not a["satisfied"] and not a["completed"]]
    if not free:
        pytest.skip("every C105 module is already done in this kept stack; scripts/itest.sh down, then up")
    return free[0]


class AU:
    """A conformant AU over HTTP. Launch URLs name the web origin (/api behind its nginx);
    the requests go to the API directly, at the same paths."""

    def __init__(self, api_base: str, launch_url: str):
        q = {k: v[0] for k, v in parse_qs(urlsplit(launch_url).query).items()}
        self.query = q
        self.http = httpx.Client(base_url=api_base, timeout=30.0)
        self.endpoint = urlsplit(q["endpoint"]).path.removeprefix("/api")
        self.fetch_path = urlsplit(q["fetch"]).path.removeprefix("/api")
        self.actor = json.loads(q["actor"])
        self.registration = q["registration"]
        self.activity_id = q["activityId"]
        self.token = ""
        self.launch_data: dict = {}

    def fetch(self) -> dict:
        r = self.http.post(self.fetch_path)
        assert r.status_code == 200, r.text
        body = r.json()
        self.token = body.get("auth-token", self.token)
        return body

    def lrs(self, method: str, resource: str, *, params=None, body=None, headers=None) -> httpx.Response:
        h = {"Authorization": f"Basic {self.token}", "X-Experience-API-Version": "1.0.3", **(headers or {})}
        if body is not None:
            h["Content-Type"] = "application/json"
        return self.http.request(
            method,
            f"{self.endpoint}{resource}",
            params=params,
            content=None if body is None else json.dumps(body),
            headers=h,
        )

    def start(self) -> AU:
        assert "auth-token" in self.fetch()
        r = self.lrs(
            "GET",
            "activities/state",
            params={
                "stateId": "LMS.LaunchData",
                "activityId": self.activity_id,
                "agent": json.dumps(self.actor),
                "registration": self.registration,
            },
        )
        assert r.status_code == 200, r.text
        self.launch_data = r.json()
        prefs = self.lrs(
            "GET", "agents/profile", params={"agent": json.dumps(self.actor), "profileId": "cmi5LearnerPreferences"}
        )
        assert prefs.status_code in (200, 404), prefs.text
        return self

    def statement(self, verb: str, *, moveon=False, result=None, ext=None, defined=True) -> dict:
        ctx = json.loads(json.dumps(self.launch_data["contextTemplate"]))
        ctx["registration"] = self.registration
        if defined:
            ctx["contextActivities"]["category"] = [{"id": CMI5}] + ([{"id": MOVEON}] if moveon else [])
        ctx["extensions"] = {**(ext or {}), **ctx["extensions"]}
        st = {
            "id": str(uuid.uuid4()),
            "actor": self.actor,
            "verb": {"id": ADL + verb, "display": {"en": verb}},
            "object": {"objectType": "Activity", "id": self.activity_id},
            "context": ctx,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        if result is not None:
            st["result"] = result
        return st

    def send(self, st: dict) -> httpx.Response:
        return self.lrs("POST", "statements", body=st)


def _launch(api_client, api_base_url, release, index, **body) -> tuple[dict, AU]:
    r = api_client.post(f"/cmi5/releases/{release['id']}/aus/{index}/launch", json=body)
    assert r.status_code == 200, r.text
    return r.json(), AU(api_base_url, r.json()["url"])


def _mark(api_client, release, index: int, scaled: float) -> None:
    """Have TrueNorth mark the module's quiz with answers worth ``scaled`` (in fifths), as the
    AU runtime does before it reports passed/failed (the API is the dev admin here)."""
    import subprocess

    raw = subprocess.run(
        ["tar", "-xzOf", str(BUNDLE), f"learner/07-bundle/cmi5/mod_{index + 1:03d}/course-config.json"],
        check=True,
        capture_output=True,
    ).stdout
    key = [(q["id"], q["answer"]) for q in json.loads(raw)["quiz"]["questions"]]
    right = round(scaled * len(key))
    answers = {qid: (a if i < right else ("A" if a != "A" else "B")) for i, (qid, a) in enumerate(key)}
    r = api_client.post(f"/cmi5/releases/{release['id']}/aus/{index}/grade", json={"answers": answers})
    assert r.status_code == 200 and abs(r.json()["scaled"] - scaled) < 1e-9, r.text


def _statements(lrs, **params) -> list[dict]:
    r = lrs.get("statements", params={"ascending": "true", "limit": "200", **params})
    assert r.status_code == 200, r.text
    return r.json()["statements"]


def test_a_session_lands_in_the_lrs_as_cmi5_requires(api_client, api_base_url, release, lrs):
    index = _fresh_au(api_client, release)
    launch, au = _launch(api_client, api_base_url, release, index)
    sid = launch["session_id"]

    # LMS.LaunchData, read from the LRS with the LRS's own credential.
    state = lrs.get(
        "activities/state",
        params={
            "stateId": "LMS.LaunchData",
            "activityId": au.activity_id,
            "agent": json.dumps(au.actor),
            "registration": au.registration,
        },
    )
    assert state.status_code == 200, state.text
    data = state.json()
    assert data["launchMode"] == "Normal" and data["moveOn"] == "Passed" and data["masteryScore"] == 0.7
    assert data["contextTemplate"]["extensions"] == {EXT + "sessionid": sid}

    au.start()
    assert au.fetch()["error-code"] == "1"  # the fetch URL works once
    _mark(api_client, release, index, 0.8)
    steps = [
        au.statement("initialized"),
        au.statement("completed", moveon=True, result={"completion": True, "duration": "PT30.00S"}),
        au.statement(
            "passed",
            moveon=True,
            ext={EXT + "masteryscore": 0.7},
            result={"success": True, "score": {"scaled": 0.8, "raw": 4, "min": 0, "max": 5}, "duration": "PT60.00S"},
        ),
        au.statement("terminated", result={"duration": "PT90.00S"}),
    ]
    for st in steps:
        r = au.send(st)
        assert r.status_code == 200, r.text

    session = [
        s
        for s in _statements(lrs, registration=au.registration)
        if s["context"].get("extensions", {}).get(EXT + "sessionid") == sid
    ]
    verbs = [s["verb"]["id"].rsplit("/", 1)[1] for s in session]
    assert verbs == ["launched", "initialized", "completed", "passed", "satisfied", "terminated"], verbs
    launched, satisfied = session[0], session[4]
    ext = launched["context"]["extensions"]
    assert {"id": CMI5} in [{"id": c["id"]} for c in launched["context"]["contextActivities"]["category"]]
    assert ext[EXT + "launchmode"] == "Normal" and ext[EXT + "moveon"] == "Passed" and ext[EXT + "masteryscore"] == 0.7
    assert urlsplit(ext[EXT + "launchurl"]).path == urlsplit(launch["url"]).path
    assert json.loads(ext[EXT + "launchparameters"])["module"] == f"mod_{index + 1:03d}"
    assert launched["object"]["id"] == au.activity_id and "ccoe.forces.gc.ca" not in au.activity_id
    assert satisfied["object"]["definition"]["type"] == "https://w3id.org/xapi/cmi5/activitytype/block"
    assert satisfied["object"]["id"].endswith(f"/block/{index}")
    assert satisfied["context"]["contextActivities"]["grouping"][0]["id"].endswith(f"/block/mod_{index + 1:03d}")
    for st in session:
        assert st["actor"]["account"] == au.actor["account"] and "mbox" not in st["actor"]
        assert st["context"]["registration"] == au.registration
        assert st["timestamp"].endswith("Z")
    # Provenance: what the AU wrote carries another authority than what TrueNorth wrote.
    tn_authority = launched["authority"]["account"]["name"]
    au_written = [s for s in session if s["verb"]["id"].rsplit("/", 1)[1] in ("initialized", "completed", "passed")]
    assert au_written and all(s["authority"]["account"]["name"] != tn_authority for s in au_written)
    assert satisfied["authority"]["account"]["name"] == tn_authority
    # The token died with the session.
    assert au.send(au.statement("experienced", defined=False)).status_code == 403


def test_a_refused_statement_never_reaches_the_lrs(api_client, api_base_url, release, lrs):
    index = _fresh_au(api_client, release)
    _, au = _launch(api_client, api_base_url, release, index)
    au.start()
    assert au.send(au.statement("initialized")).status_code == 200
    _mark(api_client, release, index, 0.2)
    bad = au.statement(
        "passed",
        moveon=True,
        ext={EXT + "masteryscore": 0.7},
        result={"success": True, "score": {"scaled": 0.2}, "duration": "PT1S"},
    )
    r = au.send(bad)
    assert r.status_code == 400 and r.json()["violatedReqId"] == "9.3.4.0-2", r.text
    assert lrs.get("statements", params={"statementId": bad["id"]}).status_code == 404
    # Review blocker 1: no uncategorised "passed" about some other TrueNorth activity.
    forged = au.statement("passed", defined=False, result={"success": True, "score": {"scaled": 1.0}})
    forged["object"] = {"objectType": "Activity", "id": au.activity_id.split("/cmi5/")[0] + "/activities/quiz/x"}
    r = au.send(forged)
    assert r.status_code == 403 and r.json()["violatedReqId"] == "TN-SCOPE", r.text
    assert lrs.get("statements", params={"statementId": forged["id"]}).status_code == 404
    write = au.lrs(
        "PUT",
        "activities/state",
        body={"launchMode": "Normal"},
        params={
            "stateId": "LMS.LaunchData",
            "activityId": au.activity_id,
            "agent": json.dumps(au.actor),
            "registration": au.registration,
        },
    )
    assert write.status_code == 403 and write.json()["violatedReqId"] == "10.2.1.0-5"


def test_relaunch_abandons_the_open_session(api_client, api_base_url, release, lrs):
    index = _fresh_au(api_client, release)
    first, au = _launch(api_client, api_base_url, release, index)
    au.start()
    assert au.send(au.statement("initialized")).status_code == 200
    _launch(api_client, api_base_url, release, index)
    abandoned = _statements(lrs, registration=au.registration, verb="https://w3id.org/xapi/adl/verbs/abandoned")
    mine = [s for s in abandoned if s["context"]["extensions"][EXT + "sessionid"] == first["session_id"]]
    assert len(mine) == 1 and mine[0]["result"]["duration"].startswith("PT")
    assert au.send(au.statement("experienced", defined=False)).status_code == 403


def test_reissue_round_trip_against_the_lrs(lrs, monkeypatch):
    """app.xapi_reissue against lrsql: the legacy query (paged with `more`), an idempotent
    re-issue, and voiding. The rows come from a stand-in for the identity table."""
    from app import xapi_reissue
    from app.lms import XAPILRSBackend

    monkeypatch.setenv("XAPI_IRI_BASE", "https://itest.example/xapi")
    monkeypatch.setenv("XAPI_ACCOUNT_HOMEPAGE", "https://itest.example")
    mbox = f"mailto:legacy-{uuid.uuid4().hex[:8]}@example.test"
    originals = []
    for i in range(3):
        st = {
            "id": str(uuid.uuid4()),
            "actor": {"objectType": "Agent", "name": "Legacy", "mbox": mbox},
            "verb": {"id": ADL + "completed", "display": {"en-US": "completed"}},
            "object": {"objectType": "Activity", "id": f"http://truenorthrange.local/exercise/ex-{i}"},
            "context": {"extensions": {"http://truenorthrange.local/extensions/range_id": "r"}},
            "timestamp": f"2026-09-0{i + 1}T10:00:00.000Z",
        }
        assert lrs.post("statements", json=st).status_code == 200
        originals.append(st["id"])

    user_id = uuid.uuid4()
    rows = [type("Row", (), {"user_id": user_id, "legacy_mbox": mbox})()]

    class _Db:
        def query(self, _model):
            return self

        def order_by(self, *_):
            return self

        def all(self):
            return rows

    key, secret = LRS_AUTH.split(":", 1)
    backend = XAPILRSBackend(lrs_url=LRS_URL, lrs_auth=base64.b64encode(f"{key}:{secret}".encode()).decode())
    # TrueNorth's server credential wrote the originals: its authority is what --apply names.
    authority = lrs.get("statements", params={"statementId": originals[0]}).json()["authority"]["account"]["name"]
    first = xapi_reissue.run(_Db(), backend, apply=True, authority=authority)
    assert (first.found, first.reissued, first.errors) == (3, 3, [])
    second = xapi_reissue.run(
        _Db(), backend, apply=True, authority=authority
    )  # identical copies: no error, nothing new
    assert second.errors == [] and second.reissued == 3
    copies = _statements(lrs, agent=json.dumps(xapi_reissue.xapi.actor(user_id)))
    assert sorted(s["context"]["statement"]["id"] for s in copies) == sorted(originals)
    assert all(s["object"]["id"].startswith("https://itest.example/xapi/activities/exercise/") for s in copies)
    voided = xapi_reissue.run(_Db(), backend, apply=True, void=True, authority=authority)
    assert voided.voided == 3 and voided.errors == []
    # The originals drop out of queries; what the agent filter still finds are the voiding
    # statements themselves (a StatementRef matches when its target does, xAPI 1.0.3).
    left = _statements(lrs, agent=json.dumps({"mbox": mbox}))
    assert {s["verb"]["id"] for s in left} == {xapi_reissue.VOIDED}
    assert sorted(s["object"]["id"] for s in left) == sorted(originals)
    assert lrs.get("statements", params={"voidedStatementId": originals[0]}).status_code == 200
