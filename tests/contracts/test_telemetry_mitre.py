"""MITRE technique tagging (telemetry_mitre.py), shared by the API and the worker.

The API and worker images share no code, so the tagger is two identical files. The
first test fails the moment they drift; the rest pin the tagging rules once, against
both copies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app import telemetry_mitre as api_mitre

pytest.importorskip("celery")
from worker import telemetry_mitre as worker_mitre  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
API_COPY = ROOT / "control-plane/api/app/telemetry_mitre.py"
WORKER_COPY = ROOT / "control-plane/worker/worker/telemetry_mitre.py"


def test_api_and_worker_copies_are_identical():
    assert API_COPY.read_bytes() == WORKER_COPY.read_bytes(), (
        "control-plane/api/app/telemetry_mitre.py and control-plane/worker/worker/telemetry_mitre.py "
        "differ; edit one and copy it over the other"
    )


@pytest.fixture(params=[api_mitre, worker_mitre], ids=["api", "worker"])
def mitre(request):
    return request.param


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"mitre_technique": "T1059.001"}, ["T1059.001"]),
        ({"mitre_technique": " t1059 "}, ["T1059"]),  # normalised
        ({"mitre_technique": ["T1071", "T1573", "T1071"]}, ["T1071", "T1573"]),  # de-duplicated, sorted
        ({"technique_id": "T1046"}, ["T1046"]),  # what injectors emit
        ({"mitre_technique": "T1021", "technique_id": "T1046"}, ["T1021"]),  # explicit field first
        ({"event_type": "dns_query"}, ["T1071.004"]),
        ({"event_type": "DNS_QUERY"}, ["T1071.004"]),  # case-insensitive
        ({"event_type": "sysmon.process_exec"}, ["T1059"]),  # last dotted segment
        ({"event_type": "c2_beacon"}, ["T1071", "T1573"]),
        ({"event_type": "process_exec", "technique_id": "T1003.001"}, ["T1003.001"]),  # event beats map
        ({"event_type": "process_exec", "mitre_technique": "not-an-id"}, ["T1059"]),  # junk falls through
    ],
)
def test_techniques_for(mitre, event, expected):
    assert mitre.techniques_for(event) == expected
    tagged = mitre.tag_event(dict(event))
    assert tagged["mitre_technique"] == expected


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"event_type": "range_metrics"},
        {"event_type": None},
        {"event_type": 42},
        {"mitre_technique": "credential dumping"},  # kept for a human; not an ID
        {"mitre_technique": ["T12", "T1059.1", 1059]},
        {"technique_id": None, "event_type": ""},
    ],
)
def test_untaggable_events_are_left_as_they_were(mitre, event):
    before = dict(event)
    assert mitre.tag_event(event) == before


def test_tag_event_mutates_in_place_and_returns_the_event(mitre):
    ev = {"event_type": "network_scan"}
    assert mitre.tag_event(ev) is ev
    assert ev["mitre_technique"] == ["T1046"]


def test_every_mapped_technique_is_a_valid_attack_id(mitre):
    for event_type, techniques in mitre.EVENT_TYPE_TECHNIQUES.items():
        assert event_type == event_type.lower()
        assert techniques
        assert all(mitre.TECHNIQUE_ID.match(t) for t in techniques), event_type
        # ... and one the ATT&CK catalogue knows as a live technique today.
        assert all(mitre.load().current(t) == t for t in techniques), event_type


# -- the ATT&CK catalogue decides what is a technique (content/mitre) ------------------------
@pytest.mark.parametrize(
    ("event", "expected", "unknown"),
    [
        # Well-formed, never issued: not tagged, kept aside for a human; event_type decides.
        ({"event_type": "process_exec", "mitre_technique": "T9999"}, ["T1059"], ["T9999"]),
        ({"mitre_technique": ["T1071", "T9999"]}, ["T1071"], ["T9999"]),
        ({"technique_id": "t1059.999", "event_type": "network_scan"}, ["T1046"], ["T1059.999"]),
        # Revoked: tagged as the technique MITRE replaced it with.
        ({"mitre_technique": "T1086"}, ["T1059.001"], None),
        ({"technique_id": "T1086", "event_type": "dns_query"}, ["T1059.001"], None),
        # Deprecated but never revoked: still a known id.
        ({"mitre_technique": "T1064"}, ["T1064"], None),
    ],
)
def test_only_ids_the_catalogue_knows_are_tagged(mitre, event, expected, unknown):
    tagged = mitre.tag_event(dict(event))
    assert tagged["mitre_technique"] == expected
    assert tagged.get("mitre_technique_unknown") == unknown


def test_an_event_naming_only_unknown_ids_loses_the_tag_but_keeps_the_ids(mitre):
    tagged = mitre.tag_event({"mitre_technique": ["T9999", "T9998"], "event_type": "range_metrics"})
    assert "mitre_technique" not in tagged
    assert tagged["mitre_technique_unknown"] == ["T9998", "T9999"]


def test_telemetry_is_never_refused_when_the_catalogue_is_unreadable(mitre, monkeypatch):
    monkeypatch.setattr(mitre, "_catalogue", lambda: None)  # what _catalogue() gives on failure
    tagged = mitre.tag_event({"mitre_technique": ["T9999", "T1086"]})
    assert tagged["mitre_technique"] == ["T1086", "T9999"]  # form alone, as before the catalogue
    assert "mitre_technique_unknown" not in tagged


def test_an_unreadable_catalogue_is_logged_and_falls_back(mitre, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("TN_ATTACK_CATALOGUE", str(tmp_path / "missing.json"))
    mitre.load.cache_clear()
    mitre._catalogue.cache_clear()
    try:
        with caplog.at_level("WARNING"):
            assert mitre._catalogue() is None
        assert "falls back to id format only" in caplog.text
    finally:
        monkeypatch.delenv("TN_ATTACK_CATALOGUE")
        mitre.load.cache_clear()
        mitre._catalogue.cache_clear()
