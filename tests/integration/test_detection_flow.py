"""Integration: the detection flow end to end, against a real OpenSearch (ADR 0005).

An instructor writes a detection rule and loads a threat intel feed (the upload variant
of the CSV backend); a range carries attack and benign telemetry; a Student, in a running
exercise, submits a detection; the credit shows on the exercise's score and objectives.

The API runs in-process (as the Moodle and vSphere lab integration tests do) so the
Student can be a Student: the live stack's dev login is an admin, whose detections are
recorded as staff attempts, not Student credit. The event store is real: bootstrap's
ingested-at pipeline and range template (on a scratch name) and the API's OpenSearch
backend, so mapping, analysis and the ingest-time window are OpenSearch's, not a fake's.

Needs OpenSearch at OPENSEARCH_URL (default http://localhost:9200, security plugin off);
skips when nothing answers there, and fails instead when INTEGRATION_REQUIRE_API is set.
"""

from __future__ import annotations

import copy
import importlib
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from _shared import acting_as
from app import search_backends
from app.models import Objective, ObjectiveType, UserRole

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
C2_DOMAIN = "northwind-update.example"

SIGMA = f"""title: Beacon to the Northwind update C2
logsource:
  category: proxy
detection:
  selection:
    url.domain|endswith: {C2_DOMAIN}
  condition: selection
level: high
"""

SCENARIO = f"""name: integ-detection-flow
version: "1"
range_template: integ
variables:
  c2_domain: {C2_DOMAIN}
timeline: [{{t: "0:00"}}]
objectives:
  - id: detect_c2
    type: detection
    validator: opensearch_query
    params: {{query: 'event_type:http AND url.domain:*{{{{ c2_domain }}}}*', min_hits: 2}}
    points: 40
  - id: write_report
    type: deliverable
    validator: deliverable_check
    points: 10
"""

FEED_CSV = f"""type,value,first_seen,mitre_technique,severity,confidence,name
domain,cdn.{C2_DOMAIN},2026-10-01,T1071.001,high,85,Northwind C2
ipv4,203.0.113.66,2026-10-01T08:00:00Z,T1071.001;T1041,critical,90,Northwind exfil
""".encode()


def _os_url() -> str:
    return os.getenv("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")


def _reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=3.0)
    except Exception:
        return False
    return True


@pytest.fixture
def event_store(monkeypatch):
    """The API's own OpenSearch backend, against the real cluster."""
    url = _os_url()
    if not _reachable(url):
        if os.getenv("INTEGRATION_REQUIRE_API", "").strip().lower() in ("1", "true", "yes"):
            pytest.fail(f"INTEGRATION_REQUIRE_API is set but no OpenSearch answers at {url}")
        pytest.skip(f"no OpenSearch at {url} — start the stack or set OPENSEARCH_URL")
    monkeypatch.setenv("OPENSEARCH_URL", url)
    monkeypatch.setenv("SEARCH_BACKEND", "opensearch")
    search_backends._reset_backend()
    with httpx.Client(base_url=url, timeout=30) as c:
        yield c
    search_backends._reset_backend()


@pytest.fixture
def range_index(event_store):
    """Bootstrap's ingest-time pipeline and range template for one range id; removed after."""
    sys.path.insert(0, str(ROOT))
    bootstrap = importlib.import_module("telemetry.pipelines.bootstrap")
    tag = uuid.uuid4().hex[:10]
    pipeline, template = f"tn-itest-ingested-at-{tag}", f"tn-itest-range-{tag}"
    made: list[str] = []

    def _for(range_id: str) -> str:
        body = copy.deepcopy(bootstrap.RANGE_TEMPLATE)
        body["index_patterns"] = [f"range-{range_id}*"]
        body["priority"] = 500
        body["template"]["settings"]["index.final_pipeline"] = pipeline
        event_store.put(
            f"/_ingest/pipeline/{pipeline}", json=bootstrap.INGEST_PIPELINES[bootstrap.INGESTED_AT_PIPELINE]
        ).raise_for_status()
        event_store.put(f"/_index_template/{template}", json=body).raise_for_status()
        made.append(range_id)
        return f"range-{range_id}"

    yield _for
    for range_id in made:
        event_store.delete(f"/range-{range_id}")
    event_store.delete(f"/_index_template/{template}")
    event_store.delete(f"/_ingest/pipeline/{pipeline}")


def _ok(resp: httpx.Response, *codes: int) -> dict:
    assert resp.status_code in (codes or (200, 201)), f"{resp.request.method} {resp.request.url} => {resp.status_code} {resp.text}"
    return resp.json()


class TestDetectionFlow:
    def test_instructor_rule_and_feed_then_student_detection_is_credited(
        self, client, db_session, event_store, range_index
    ):
        # -- the instructor's side: a rule, a feed, a range, an exercise --------------------
        rule = _ok(client.post("/detection-rules", json={
            "title": "Northwind C2 beacon", "detection_yaml": SIGMA, "level": "high",
            "status": "testing", "mitre_attack_ids": ["T1071.001"],
        }), 201)
        assert rule["id"] in {r["id"] for r in _ok(client.get("/detection-rules"))}

        feed = _ok(client.post("/threat-intel/feeds", json={"name": "northwind-iocs", "feed_type": "csv"}), 201)
        pulled = _ok(client.post(
            f"/threat-intel/feeds/{feed['id']}/upload", files={"file": ("northwind.csv", FEED_CSV, "text/csv")}
        ))
        assert (pulled["status"], pulled["created"], pulled["rejected"]) == ("ok", 2, 0)
        iocs = _ok(client.get("/threat-intel/indicators", params={"value": C2_DOMAIN}))
        assert [i["value"] for i in iocs] == [f"cdn.{C2_DOMAIN}"]

        template = _ok(client.post("/templates", json={
            "name": f"integ-detect-{uuid.uuid4().hex[:6]}", "yaml": "name: integ\nenvironment: enterprise\n",
            "is_public": False,
        }), 201)
        rng = _ok(client.post("/ranges", json={"name": "integ-detect-range", "template_id": template["id"]}), 201)
        index = range_index(rng["id"])
        scenario = _ok(client.post("/scenarios", json={"name": "integ-detection-flow", "yaml": SCENARIO,
                                                        "is_public": False}), 201)
        ex = _ok(client.post("/exercises", json={"name": "integ-detect", "range_id": rng["id"],
                                                  "scenario_id": scenario["id"], "max_score": 50}), 201)
        # POST /exercises makes no objective rows (the forge and QSP paths do). Seed them as
        # QSP-made rows look: no params, so the answer key comes from the scenario.
        for ref, kind, validator, points in (("detect_c2", ObjectiveType.detection, "opensearch_query", 40),
                                             ("write_report", ObjectiveType.deliverable, "deliverable_check", 10)):
            db_session.add(Objective(exercise_id=uuid.UUID(ex["id"]), ref_id=ref, objective_type=kind,
                                     description=ref, validator=validator, points=points))
        db_session.commit()
        _ok(client.post(f"/exercises/{ex['id']}/start"), 200, 202)
        assert _ok(client.get(f"/exercises/{ex['id']}"))["state"] == "running"

        # -- the range: the attack's beacons among benign traffic, after the start ---------
        events = [{"event_type": "http", "url": {"domain": f"cdn.{C2_DOMAIN}"}} for _ in range(3)]
        events += [{"event_type": "http", "url": {"domain": f"site{i}.example"}} for i in range(4)]
        _ok(client.post(f"/telemetry/{rng['id']}/events", json=events), 202)
        event_store.post(f"/{index}/_refresh").raise_for_status()

        # -- the Student: nothing is credited for being idle; a precise detection is -------
        with acting_as(UserRole.student):
            assert _ok(client.get(f"/exercises/{ex['id']}/detections")) == []
            before = {o["ref_id"]: o["achieved"] for o in _ok(client.get(f"/exercises/{ex['id']}/objectives"))}
            assert before == {"detect_c2": False, "write_report": False}

            sub = _ok(client.post(
                f"/exercises/{ex['id']}/objectives/detect_c2/detections",
                json={"query": f"url.domain:*{C2_DOMAIN}"},
            ), 201)
            assert sub["verdict"] == "achieved", sub
            assert sub["events_matched"] == 3

        # -- the credit is visible where the score is read ---------------------------------
        scored = _ok(client.get(f"/exercises/{ex['id']}"))
        assert scored["total_score"] == 40
        objectives = {o["ref_id"]: o for o in _ok(client.get(f"/exercises/{ex['id']}/objectives"))}
        assert objectives["detect_c2"]["achieved"] is True and objectives["write_report"]["achieved"] is False
        assert '"source": "student_detection"' in (objectives["detect_c2"]["evidence"] or "")
        attempts = _ok(client.get(f"/exercises/{ex['id']}/detections"))
        assert [a["verdict"] for a in attempts] == ["achieved"]

        _ok(client.post(f"/exercises/{ex['id']}/complete"), 200, 202)
