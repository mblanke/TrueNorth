# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""MITRE ATT&CK tagging of range telemetry (app/telemetry_mitre.py).

Every event written to ``range-<id>`` leaves with a normalised ``mitre_technique`` list
when a technique is known: first what the event itself names (``mitre_technique`` or the
injectors' ``technique_id``, upper-cased and kept only when the ATT&CK catalogue knows it),
else the ``event_type`` mapping (a dotted type is also tried by its last segment). Nothing
matched leaves the event untouched; a well-formed id the catalogue does not know moves to
``mitre_technique_unknown`` so a human still sees it. Telemetry is never refused over a
technique id.

These tests run against the API copy; tests/contracts/test_telemetry_mitre.py holds the
worker's byte-identical copy to the same rules.
"""

from __future__ import annotations

import pytest
from app import telemetry_mitre
from app.telemetry_mitre import EVENT_TYPE_TECHNIQUES, tag_event, techniques_for


def test_an_explicit_known_technique_is_normalised_and_tagged():
    assert techniques_for({"technique_id": " t1059 "}) == ["T1059"]
    assert techniques_for({"mitre_technique": ["t1071", "T1573", "T1071"]}) == ["T1071", "T1573"]
    event = tag_event({"technique_id": "T1110"})
    assert event == {"technique_id": "T1110", "mitre_technique": ["T1110"]}


def test_a_known_sub_technique_is_kept_as_itself():
    # The catalogue knows the sub-technique spelling, so it is tagged, not "unknown".
    assert techniques_for({"mitre_technique": "T1059.001"}) == ["T1059.001"]


@pytest.mark.parametrize("value", ["T9999", "T9999.001"])
def test_a_well_formed_id_the_catalogue_does_not_know_is_not_tagged_but_recorded(value):
    assert techniques_for({"mitre_technique": value}) == []
    event = tag_event({"event_type": "process_exec", "mitre_technique": value})
    assert event["mitre_technique_unknown"] == [value]
    assert event["mitre_technique"] == ["T1059"]  # the event type still tags it


def test_an_event_naming_only_unknown_ids_has_the_member_moved_not_dropped():
    event = tag_event({"mitre_technique": "T9999"})
    assert event == {"mitre_technique_unknown": ["T9999"]}


def test_a_non_identifier_value_is_left_for_a_human_to_read():
    event = tag_event({"mitre_technique": "beaconing", "event_type": "dns_query"})
    assert event["mitre_technique"] == ["T1071.004"]  # tagged from the event type
    event = tag_event({"mitre_technique": "beaconing"})
    assert event == {"mitre_technique": "beaconing"}  # nothing matched: kept, not guessed over


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("dns_query", ["T1071.004"]),
        ("DNS_QUERY", ["T1071.004"]),  # case-insensitive
        ("sysmon.process_exec", ["T1059"]),  # a dotted type is tried by its last segment
        ("c2_beacon", ["T1071", "T1573"]),  # two techniques, sorted
        ("curl", []),
        ("", []),
    ],
)
def test_the_event_type_falls_back_through_the_mapping(event_type, expected):
    assert techniques_for({"event_type": event_type}) == expected


def test_every_mapped_technique_is_well_formed_and_known_to_the_catalogue():
    catalogue = telemetry_mitre._catalogue()
    assert catalogue is not None and EVENT_TYPE_TECHNIQUES
    for event_type, tids in EVENT_TYPE_TECHNIQUES.items():
        for tid in tids:
            assert telemetry_mitre.TECHNIQUE_ID.match(tid), (event_type, tid)
            assert catalogue.current(tid), (event_type, tid)  # a live ATT&CK id, not just well-formed


def test_the_explicit_technique_wins_over_the_event_type():
    assert techniques_for({"event_type": "http_request", "mitre_technique": "T1110"}) == ["T1110"]


def test_an_event_with_nothing_to_say_is_returned_unchanged():
    for event in (
        {"url.domain": "x.example"},
        {"event_type": "quiet"},
        {"mitre_technique": None},
        {"technique_id": None},
        {"technique_id": [None, 7]},
    ):
        assert tag_event(dict(event)) == event


def test_tagging_is_in_place_and_returns_the_event():
    event = {"event_type": "network_scan"}
    assert tag_event(event) is event
    assert event["mitre_technique"] == ["T1046"]
