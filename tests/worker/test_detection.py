"""Which objective rows become ScoringEngine detections, and with what query/index/threshold."""

from __future__ import annotations

import json

import pytest
from worker.detection import pending_objectives, range_index

RANGE = "r-1"
YAML = """
objectives:
  - id: from_yaml
    validator: opensearch_query
    params: {query: 'event_type:dns', min_hits: 3, index: 'truenorth-*'}
"""


def _row(ref, params=None, validator="opensearch_query", points=10, achieved=False):
    return (ref, validator, json.dumps(params) if params is not None else None, points, achieved)


def test_index_matches_what_telemetry_ingest_writes():
    assert range_index(RANGE) == "range-r-1"


def test_row_params_win_and_yaml_fills_rows_without_a_query():
    rows = [
        _row("row", {"query": "host:ws01", "min_hits": 2}),
        _row("from_yaml", {}),  # exercise_forge stores "{}" when content had no params
    ]
    got = {o["id"]: o["validation_config"] for o in pending_objectives(RANGE, YAML, rows)}
    assert got == {
        "row": {"index": "range-r-1", "query": "host:ws01", "threshold": 2},
        "from_yaml": {"index": "range-r-1", "query": "event_type:dns", "threshold": 3},
    }


def test_skips_achieved_non_query_and_queryless_objectives():
    rows = [
        _row("done", {"query": "a:b"}, achieved=True),
        _row("manual", {"query": "a:b"}, validator="validate.manual_ack"),
        _row("deliverable", {"query": "a:b"}, validator="validate.deliverable_check"),
        _row("empty", None),
        _row("blank", {"query": ""}),
    ]
    assert pending_objectives(RANGE, None, rows) == []


def test_prefixed_validator_name_and_bad_params_are_tolerated():
    rows = [
        _row("prefixed", {"query": "a:b", "min_hits": "x"}, validator="validate.opensearch_query"),
        ("garbled", "opensearch_query", "{not json", 5, False),
    ]
    got = pending_objectives(RANGE, "objectives: [", rows)  # unparseable YAML too
    assert [(o["id"], o["validation_config"]["threshold"], o["partial_credit"]) for o in got] == [
        ("prefixed", 1, False)
    ]


@pytest.mark.parametrize(
    "name", ["opensearch_query", "validate.opensearch_query", "validate.opensearch.query", "validate_opensearch_query"]
)
def test_every_spelling_of_the_query_validator_is_scored(name):
    from worker.detection import is_query_validator

    assert is_query_validator(name)
    assert not is_query_validator("manual_ack")
