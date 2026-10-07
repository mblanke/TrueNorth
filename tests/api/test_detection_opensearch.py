"""Detection credit against a real OpenSearch (review B4 and M4).

The unit tests' FakeStore matches with fnmatch; OpenSearch maps and analyses fields, so a
key that passes there can match nothing here. This runs the real thing: bootstrap's
ingested-at pipeline and range template (under a scratch name), the API's search backend,
and every answer key in shipped content against an event it should find.

Set TEST_OPENSEARCH_URL (e.g. http://localhost:9200, security plugin off). CI's
test-python job runs an OpenSearch service and sets it.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from app.detections import credit
from app.search_backends import OpenSearchBackend

ROOT = Path(__file__).resolve().parents[2]
URL = os.getenv("TEST_OPENSEARCH_URL")
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not URL, reason="set TEST_OPENSEARCH_URL to run against OpenSearch")]

# One event each answer key should find, as a sensor in the range would send it.
FIXTURES: dict[tuple[str, str], dict] = {
    ("apt-nation-state", "detect_phish"): {"event_type": "email", "attachment.name": "briefing-2026-Q1.docm"},
    ("apt-nation-state", "detect_macro"): {
        "ParentImage": "C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE",
        "Image": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    },
    ("apt-nation-state", "detect_c2"): {"event_type": "http", "url.domain": "cdn-assets.northwind-update.example"},
    ("apt-nation-state", "detect_cred_dump"): {
        "process_name": "rundll32.exe",
        "CommandLine": "rundll32.exe C:\\Windows\\System32\\comsvcs.dll, MiniDump 624 C:\\temp\\l.dmp full",
    },
    ("apt-nation-state", "detect_lateral"): {"service_name": "PSEXESVC"},
    ("apt-nation-state", "detect_exfil"): {"event_type": "flow", "dst_port": 443, "bytes_out": 250_000_000},
    ("cloud-breach", "detect_s3_exposure"): {"event_source": "s3", "requester": "ANONYMOUS", "bucket": "company-backups"},
    ("cloud-breach", "detect_stolen_creds"): {
        "eventName": "GetCallerIdentity", "userIdentity": {"userName": "dev-deploy"}, "sourceIPAddress": "203.0.113.66",
    },
    ("cloud-breach", "detect_priv_esc"): {
        "eventName": "AttachUserPolicy",
        "requestParameters": {"policyArn": "arn:aws:iam::aws:policy/AdministratorAccess"},
    },
    ("cloud-breach", "detect_lambda_backdoor"): {
        "eventName": "CreateFunction20150331", "requestParameters": {"functionName": "health-check"},
    },
    ("cloud-breach", "detect_cloudtrail_stop"): {"eventName": "StopLogging"},
    ("cloud-breach", "detect_data_exfil"): {
        "event_source": "s3", "bucket": "secret-configs", "bytesTransferredOut": 300_000_000,
    },
    ("ics-attack", "detect_it_ot_pivot"): {
        "event_type": "flow", "src_ip": "10.50.101.20", "dst_ip": "10.50.104.10", "dst_port": 3389,
    },
    ("ics-attack", "detect_ot_recon"): {"event_type": "scan", "dst_ip": "10.50.104.40", "dst_port": 502},
    ("ics-attack", "detect_modbus_anomaly"): {
        "protocol": "modbus", "function_code": "write_multiple_registers", "src_ip": "10.50.104.40",
    },
    ("ics-attack", "detect_plc_change"): {"event_type": "plc_change", "action": "program_download"},
    ("ics-attack", "detect_safety_bypass"): {"event_type": "safety_event", "status": "BYPASS"},
    ("incident-response-drill", "identify_c2"): {"event_type": "flow", "dst_ip": "10.60.200.10", "dst_port": 443},
    ("insider-threat-advanced", "detect_after_hours"): {"EventID": 4624, "LogonType": 2, "hour_of_day": 23},
    ("insider-threat-advanced", "detect_priv_esc"): {"EventID": 4732, "TargetUserName": "m.delacroix"},
    ("insider-threat-advanced", "detect_bulk_access"): {
        "EventID": 5145, "ShareName": "\\\\fs01\\hr", "SubjectUserName": "m.delacroix",
    },
    ("insider-threat-advanced", "detect_usb"): {"EventID": 6416},
    ("insider-threat-advanced", "detect_cloud_exfil"): {
        "event_type": "http", "url.domain": "content.dropbox.com", "bytes_out": 50_000_000,
    },
    ("insider-threat-advanced", "detect_log_clear"): {"EventID": 1102},
    ("ransomware-lite", "detect_dns"): {"dns": {"question": {"name": "bad.example"}}},
}
DECOY = {"event_type": "http", "url.domain": "intranet.corp.example", "process_name": "svchost.exe"}


def _keys() -> list[tuple[str, str, credit.AnswerKey]]:
    out = []
    for path in sorted((ROOT / "content/scenarios").glob("*/scenario.yaml")):
        text = path.read_text()
        for obj in yaml.safe_load(text).get("objectives") or []:
            if (obj.get("params") or {}).get("query"):
                key = credit.answer_key(obj["validator"], None, obj["id"], text)
                out.append((path.parent.name, obj["id"], key))
    return out


KEYS = _keys()


@pytest.fixture(scope="module")
def cluster():
    """Bootstrap's pipeline and range template on a scratch index family; removed after."""
    sys.path.insert(0, str(ROOT))
    bootstrap = importlib.import_module("telemetry.pipelines.bootstrap")  # the package exports a same-named function

    tag = uuid.uuid4().hex[:10]
    pipeline, template = f"tn-test-ingested-at-{tag}", f"tn-test-range-{tag}"
    range_id = f"{tag}-0000-4000-8000-000000000000"
    body = copy.deepcopy(bootstrap.RANGE_TEMPLATE)
    body["index_patterns"] = [f"range-{range_id}*"]
    body["priority"] = 500
    body["template"]["settings"]["index.final_pipeline"] = pipeline
    with httpx.Client(base_url=URL, timeout=30) as c:
        c.put(f"/_ingest/pipeline/{pipeline}", json=bootstrap.INGEST_PIPELINES[bootstrap.INGESTED_AT_PIPELINE]).raise_for_status()
        c.put(f"/_index_template/{template}", json=body).raise_for_status()
        try:
            yield c, range_id
        finally:
            c.delete(f"/range-{range_id}*")
            c.delete(f"/_index_template/{template}")
            c.delete(f"/_ingest/pipeline/{pipeline}")


@pytest.fixture(scope="module")
def loaded(cluster):
    """Every fixture, min_hits times: half through the API's backend (range-<id>), half as
    Filebeat writes them (range-<id>-<date>, unstamped by the sender, a forged stamp included)."""
    c, range_id = cluster
    start = datetime.now(UTC) - timedelta(seconds=5)
    api_events, beat_events = [], []
    for scenario, ref, key in KEYS:
        events = [copy.deepcopy(FIXTURES[(scenario, ref)]) for _ in range(key.threshold)]
        (api_events if len(api_events) <= len(beat_events) else beat_events).extend(events)
    beat_events.append({**DECOY, "truenorth.ingested_at": "1999-01-01T00:00:00Z"})
    api_events.extend([DECOY] * 3)
    accepted = asyncio.run(OpenSearchBackend(url=URL).ingest(f"range-{range_id}", api_events))
    assert accepted == len(api_events)
    lines = "".join(
        f'{{"index":{{"_index":"range-{range_id}-2026.10.07"}}}}\n' + httpx.Response(200, json=e).text + "\n"
        for e in beat_events
    )
    c.post("/_bulk", content=lines, headers={"content-type": "application/x-ndjson"}).raise_for_status()
    c.post(f"/range-{range_id}*/_refresh").raise_for_status()
    return range_id, start


def test_every_query_objective_has_a_fixture():
    assert {(s, r) for s, r, _ in KEYS} == set(FIXTURES)


@pytest.mark.parametrize(("scenario", "ref", "key"), KEYS, ids=[f"{s}:{r}" for s, r, _ in KEYS])
def test_each_answer_key_is_achievable_by_a_precise_detection(loaded, scenario, ref, key):
    range_id, start = loaded
    judged = asyncio.run(
        credit.judge(OpenSearchBackend(url=URL), range_id, key, key.query, start, datetime.now(UTC) + timedelta(seconds=5))
    )
    assert judged.on_target >= key.threshold, (scenario, ref, key.query, judged)
    assert judged.achieved, judged


def test_catch_all_fails_and_window_excludes_earlier_telemetry(loaded):
    range_id, start = loaded
    [(_, _, key)] = [k for k in KEYS if k[:2] == ("apt-nation-state", "detect_c2")]
    backend = OpenSearchBackend(url=URL)
    now = datetime.now(UTC) + timedelta(seconds=5)
    assert not asyncio.run(credit.judge(backend, range_id, key, "*", start, now)).achieved
    later = asyncio.run(credit.judge(backend, range_id, key, key.query, now, now + timedelta(minutes=1)))
    assert later.events_matched == 0


def test_the_cluster_stamps_ingest_time_over_a_forged_one(cluster, loaded):
    c, range_id = cluster
    hits = c.post(
        f"/range-{range_id}*/_search", json={"query": {"term": {"process_name": "svchost.exe"}}, "size": 10}
    ).json()["hits"]["hits"]
    stamps = {h["_source"]["truenorth"]["ingested_at"][:4] for h in hits}
    assert stamps == {str(datetime.now(UTC).year)}
    assert all("truenorth.ingested_at" not in h["_source"] for h in hits)


def test_a_query_that_does_not_parse_is_a_query_error(loaded):
    from app.search_backends import SearchQueryError

    range_id, start = loaded
    with pytest.raises(SearchQueryError):
        asyncio.run(OpenSearchBackend(url=URL).match(f"range-{range_id}", {"query_string": {"query": "url.domain:("}}))
