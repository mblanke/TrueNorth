# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""A Student's detection as evidence for the instructor who judges the objective
(app/detections/evidence.py, ADR 0020).

Team exercises have no answer key: a submitted detection is run against the range's events
inside the exercise window and recorded with what it matched. The instructor's look-for
query is staff-only guidance rendered from the scenario's ``variables:`` — never compared
with a submission, never shown to a Student. The window is server ingest time, so telemetry
from before the exercise is outside it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from _shared import SCENARIO, STARTED, FakeStore
from app.detections.evidence import (
    EVIDENCE_IDS,
    INGESTED_AT,
    look_for,
    matches,
    range_index,
    student_query,
    window,
)
from app.search_backends.query import QueryError


# ── the range's indices ────────────────────────────────────────────────
def test_a_range_is_searched_in_both_index_shapes():
    assert range_index("r-1") == "range-r-1,range-r-1-*"
    assert range_index(42) == "range-42,range-42-*"


# ── the window: server ingest time ─────────────────────────────────────
def test_the_window_bounds_server_ingest_time():
    start, end = STARTED, STARTED + timedelta(hours=2)
    assert window(start, end) == {"range": {INGESTED_AT: {"gte": start.isoformat(), "lte": end.isoformat()}}}
    assert INGESTED_AT == "truenorth.ingested_at"


# ── the look-for query: staff-only guidance ────────────────────────────
def test_a_detection_objective_renders_its_look_for_from_the_scenario_variables():
    q = look_for("opensearch_query", None, "detect_c2", SCENARIO)
    assert q == "event_type:http AND url.domain:*northwind-update.example*"  # {{ c2_domain }} filled


def test_a_dotted_validator_spelling_has_the_same_look_for():
    assert look_for("validate.opensearch_query", None, "detect_c2", SCENARIO) == (
        look_for("opensearch_query", None, "detect_c2", SCENARIO)
    )


def test_row_params_win_over_the_scenario_yaml():
    q = look_for("opensearch_query", '{"query": "event_type:email"}', "detect_c2", SCENARIO)
    assert q == "event_type:email"


def test_params_fall_back_to_the_yaml_objective_with_the_same_id():
    q = look_for("opensearch_query", None, "detect_phish", SCENARIO)
    assert q == "event_type:email AND attachment.name:*.docm"


def test_an_objective_whose_query_is_missing_or_blank_has_no_look_for():
    assert look_for("opensearch_query", None, "detect_c2", "name: s\nobjectives:\n  - id: detect_c2\n") is None
    assert look_for("opensearch_query", '{"query": "   "}', "detect_c2", SCENARIO) is None


def test_a_non_detection_objective_has_no_look_for():
    assert look_for("deliverable_check", None, "write_report", SCENARIO) is None
    assert look_for(None, None, "detect_c2", SCENARIO) is None


@pytest.mark.parametrize("bad", [None, "", "not: [valid", "- a list\n"])
def test_an_unusable_scenario_yaml_yields_no_look_for(bad):
    assert look_for("opensearch_query", None, "detect_c2", bad) is None


# ── the Student's query: the closed grammar ────────────────────────────
def test_a_student_query_is_parsed_and_re_serialised():
    assert student_query("event_type:http AND url.domain:*northwind*") == ("event_type:http AND url.domain:*northwind*")


@pytest.mark.parametrize(
    "query",
    [
        "inject_action:c2_beacon",  # a ground-truth label, the old answer-key exploit
        "just free text",  # free text searches every field, labels included
        "event_type:http OR exercise_id:*",
    ],
)
def test_anything_outside_the_grammar_is_refused(query):
    with pytest.raises(QueryError):
        student_query(query)


# ── matching, with a stand-in for the event store ──────────────────────
def test_a_detection_matches_only_events_inside_the_window():
    inside = {
        "event_type": "http",
        "url.domain": "cdn.northwind-update.example",
        "truenorth": {"ingested_at": STARTED.isoformat()},
    }
    before = {**inside, "truenorth": {"ingested_at": (STARTED - timedelta(minutes=1)).isoformat()}}
    after = {**inside, "truenorth": {"ingested_at": (STARTED + timedelta(hours=3)).isoformat()}}
    store = FakeStore(events=[inside, before, after])
    found = asyncio.run(
        matches(store, "r-1", "event_type:http AND url.domain:*northwind*", STARTED, STARTED + timedelta(hours=2))
    )
    assert found.events_matched == 1 and found.matched_ids == ["0"]


def test_a_trusted_query_skips_the_student_grammar():
    store = FakeStore(events=[])
    # A ground-truth label query, refused for a Student, is fine for staff looking at
    # their own telemetry — and it still goes to the range's indices inside the window.
    with pytest.raises(QueryError):
        asyncio.run(matches(store, "r-1", "inject_action:c2_beacon", STARTED, STARTED))
    asyncio.run(matches(store, "r-1", "inject_action:c2_beacon", STARTED, STARTED, trusted=True))
    assert store.indices == {"range-r-1,range-r-1-*"}


def test_recorded_evidence_ids_are_capped():
    store = FakeStore(
        events=[
            {"event_type": "http", "truenorth": {"ingested_at": STARTED.isoformat()}} for _ in range(EVIDENCE_IDS + 2)
        ]
    )
    found = asyncio.run(matches(store, "r-1", "*", STARTED, STARTED, trusted=True))
    assert found.events_matched == EVIDENCE_IDS + 2  # the count is everything the store matched
    assert len(found.matched_ids) == EVIDENCE_IDS  # the recorded evidence is capped
