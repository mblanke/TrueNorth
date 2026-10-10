"""Shared pieces for the cmi5 API tests: an in-memory LRS, the C105 release, and an AU.

``MemoryLRS`` answers the xAPI resources the cmi5 code uses the way yetanalytics/lrsql
v0.9.9 does (checked against the real thing, 2026-10-09): a State/Profile PUT over an
existing document without If-Match is 409; re-PUTting an identical statement is 204 and a
different one under the same id 409; an absent document is 404. The live round trip
against a real lrsql is tests/integration/test_cmi5_lrs.py.

``AU`` is a conformant cmi5 assignable unit driven through the TestClient: it does what
TrueNorth's SPA runtime does (fetch once, read LMS.LaunchData and the learner
preferences, then initialized ... terminated), and lets a test send anything else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.lms import LRSResponse, XAPILRSBackend

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "content/releases/c105-foundations-d0e83b9f6c1f.tar.gz"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
CROSSWALK = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"
XSD = ROOT / "docs/interfaces/cmi5/CourseStructure.xsd"

CMI5 = "https://w3id.org/xapi/cmi5/context/categories/cmi5"
MOVEON = "https://w3id.org/xapi/cmi5/context/categories/moveon"
EXT = "https://w3id.org/xapi/cmi5/context/extensions/"
ADL = "http://adlnet.gov/expapi/verbs/"
SATISFIED = "https://w3id.org/xapi/adl/verbs/satisfied"
ABANDONED = "https://w3id.org/xapi/adl/verbs/abandoned"
WAIVED = "https://w3id.org/xapi/adl/verbs/waived"


def _key(params: dict, *names: str) -> tuple:
    out = []
    for n in names:
        v = params.get(n)
        if n == "agent" and v is not None:
            acct = json.loads(v).get("account") or {}
            v = (acct.get("homePage"), acct.get("name"))
        out.append(v)
    return tuple(out)


class MemoryLRS(XAPILRSBackend):
    def __init__(self):
        super().__init__(lrs_url="http://memory.invalid", lrs_auth="")
        self.statements: dict[str, dict] = {}
        self.order: list[str] = []
        self.docs: dict[tuple, bytes] = {}
        self.refuse_statements = False
        self.calls: list[tuple[str, str]] = []

    def xapi_request(self, method, resource, *, params=None, body=None, headers=None, timeout=10.0):
        params = dict(params or {})
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.calls.append((method, resource))
        if resource == "about":
            return LRSResponse(200, b'{"version":["1.0.3"]}', {"content-type": "application/json"})
        if resource == "statements":
            return self._statements(method, params, body)
        if resource == "activities/state":
            return self._doc(
                method, ("state", *_key(params, "activityId", "agent", "registration", "stateId")), body, headers
            )
        if resource == "agents/profile":
            return self._doc(method, ("agent", *_key(params, "agent", "profileId")), body, headers)
        if resource == "activities/profile":
            return self._doc(method, ("activity", *_key(params, "activityId", "profileId")), body, headers)
        return LRSResponse(404)

    def _statements(self, method, params, body):
        if self.refuse_statements:
            return LRSResponse(500, b'{"error":"down"}')
        if method == "GET":
            return LRSResponse(200, json.dumps({"statements": [], "more": ""}).encode())
        payload = json.loads(body)
        batch = payload if isinstance(payload, list) else [payload]
        if method == "PUT":
            batch[0].setdefault("id", params["statementId"])
        ids = []
        for st in batch:
            st.setdefault("id", str(uuid.uuid4()))
            have = self.statements.get(st["id"])
            if have is not None and have != st:
                return LRSResponse(409, b'{"error":"conflict"}')
            if have is None:
                self.statements[st["id"]] = st
                self.order.append(st["id"])
            ids.append(st["id"])
        if method == "PUT":
            return LRSResponse(204)
        return LRSResponse(200, json.dumps(ids).encode(), {"content-type": "application/json"})

    def _doc(self, method, key, body, headers):
        have = self.docs.get(key)
        etag = f'"{hashlib.sha1(have).hexdigest()}"' if have is not None else None
        if method == "GET":
            if have is None:
                return LRSResponse(404)
            return LRSResponse(200, have, {"content-type": "application/json", "etag": etag})
        if method == "DELETE":
            self.docs.pop(key, None)
            return LRSResponse(204)
        if have is not None and headers.get("if-match") != etag:
            return LRSResponse(409 if "if-match" not in headers else 412)
        if have is None and headers.get("if-match"):
            return LRSResponse(412)
        if method == "POST" and have is not None:
            body = json.dumps({**json.loads(have), **json.loads(body)}).encode()
        self.docs[key] = body
        return LRSResponse(204)

    # -- what tests look at --
    def by_verb(self, verb_id: str) -> list[dict]:
        return [self.statements[i] for i in self.order if self.statements[i]["verb"]["id"] == verb_id]

    def verbs(self, registration: str) -> list[str]:
        return [
            self.statements[i]["verb"]["id"].rsplit("/", 1)[1]
            for i in self.order
            if self.statements[i].get("context", {}).get("registration") == registration
        ]

    def launch_data(self, activity_id: str, actor: dict, registration: str) -> dict:
        key = (
            "state",
            activity_id,
            (actor["account"]["homePage"], actor["account"]["name"]),
            registration,
            "LMS.LaunchData",
        )
        return json.loads(self.docs[key])


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AU:
    """A conformant AU, driven through the API (TestClient), started from a launch URL."""

    def __init__(self, client, launch_url: str):
        q = {k: v[0] for k, v in parse_qs(urlsplit(launch_url).query).items()}
        self.client = client
        self.url = launch_url
        self.endpoint = urlsplit(q["endpoint"]).path.removeprefix("/api")  # TestClient has no /api prefix
        self.fetch_url = urlsplit(q["fetch"]).path.removeprefix("/api")
        self.actor = json.loads(q["actor"])
        self.registration = q["registration"]
        self.activity_id = q["activityId"]
        self.token: str | None = None
        self.launch_data: dict = {}

    def fetch(self) -> dict:
        r = self.client.post(self.fetch_url)
        assert r.status_code == 200, r.text
        body = r.json()
        if "auth-token" in body:
            self.token = body["auth-token"]
        return body

    def headers(self, **extra) -> dict:
        return {"Authorization": f"Basic {self.token}", "X-Experience-API-Version": "1.0.3", **extra}

    def lrs(self, method: str, resource: str, *, params=None, json_body=None, headers=None):
        return self.client.request(
            method,
            f"{self.endpoint}{resource}",
            params=params,
            content=None if json_body is None else json.dumps(json_body),
            headers=self.headers(
                **({"Content-Type": "application/json"} if json_body is not None else {}), **(headers or {})
            ),
        )

    def state_params(self, state_id: str) -> dict:
        return {
            "stateId": state_id,
            "activityId": self.activity_id,
            "agent": json.dumps(self.actor),
            "registration": self.registration,
        }

    def start(self) -> AU:
        assert "auth-token" in self.fetch()
        r = self.lrs("GET", "activities/state", params=self.state_params("LMS.LaunchData"))
        assert r.status_code == 200, r.text
        self.launch_data = r.json()
        r = self.lrs(
            "GET", "agents/profile", params={"agent": json.dumps(self.actor), "profileId": "cmi5LearnerPreferences"}
        )
        assert r.status_code in (200, 404), r.text
        return self

    def statement(self, verb: str, *, defined=True, moveon=False, result=None, ext=None, **override) -> dict:
        ctx = json.loads(json.dumps(self.launch_data["contextTemplate"]))
        ctx["registration"] = self.registration
        ctx.setdefault("contextActivities", {})
        if defined:
            ctx["contextActivities"]["category"] = [{"id": CMI5}] + ([{"id": MOVEON}] if moveon else [])
        ctx["extensions"] = {**(ext or {}), **ctx.get("extensions", {})}
        st = {
            "id": str(uuid.uuid4()),
            "actor": self.actor,
            "verb": {"id": verb if "://" in verb else ADL + verb, "display": {"en": verb.rsplit("/", 1)[-1]}},
            "object": {"objectType": "Activity", "id": self.activity_id},
            "context": ctx,
            "timestamp": now(),
        }
        if result is not None:
            st["result"] = result
        st.update(override)
        return st

    def send(self, *statements: dict):
        body = statements[0] if len(statements) == 1 else list(statements)
        return self.lrs("POST", "statements", json_body=body)

    def initialized(self):
        return self.send(self.statement("initialized"))

    def completed(self):
        return self.send(self.statement("completed", moveon=True, result={"completion": True, "duration": "PT60.00S"}))

    def scored(self, scaled: float):
        ms = self.launch_data.get("masteryScore")
        ok = scaled >= ms if ms is not None else scaled >= 0.5
        return self.send(
            self.statement(
                "passed" if ok else "failed",
                moveon=True,
                result={"success": ok, "score": {"scaled": scaled}, "duration": "PT90.00S"},
                ext={EXT + "masteryscore": ms} if ms is not None else None,
            )
        )

    def terminated(self):
        return self.send(self.statement("terminated", result={"duration": "PT120.00S"}))


def token_session_id(token: str) -> str:
    return base64.b64decode(token).decode().split(":", 1)[0]
