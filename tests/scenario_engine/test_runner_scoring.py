"""The CLI runner scores objectives through the ScoringEngine.

Until 2026-10-03 ``evaluate_objectives`` imported ``scenario-engine.validators``, a name no
module can have, so it returned [] and every run reported 0 points.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from runner.run import evaluate_objectives
from scenario_engine.event_stores import NullEventStore
from scenario_engine.scoring import validation_method

ROOT = Path(__file__).resolve().parents[2]
RANGE = {"range_id": "rx", "tenant_id": "t"}
# The run's start: observed-only objectives count events ingested since.
START = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
_AT = {"truenorth.ingested_at": "2026-10-12T09:30:00+00:00"}


class _Recording(NullEventStore):
    def __init__(self, events):
        super().__init__(events)
        self.indices: set[str] = set()

    async def search(self, index, query, size=20, offset=0):
        self.indices.add(index)
        return await super().search(index, query, size, offset)


def _observed(scenario: dict, *ids: str) -> dict:
    """The scenario with these objectives judged by the engine (require_observed)."""
    for obj in scenario["objectives"]:
        if obj["id"] in ids:
            obj["params"] = {**obj.get("params", {}), "require_observed": True}
    return scenario


def test_validator_names_map_to_scoring_methods():
    assert validation_method("opensearch_query") == "opensearch_query"
    assert validation_method("validate.opensearch_query") == "opensearch_query"
    assert validation_method("validate.manual_ack") == "manual"
    assert validation_method("validate.opensearch.query") == "opensearch_query"  # ransomware-lite's old spelling
    assert validation_method("validate_opensearch_query") == "opensearch_query"
    assert validation_method("deliverable_check") == "deliverable"
    assert validation_method("") == "manual"


def test_shipped_detection_objectives_are_left_to_student_submissions():
    # ADR 0005: the inject's records match the answer key, so the runner must not credit them.
    scenario = yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text())
    store = _Recording([{"event_type": "email", "attachment.name": "briefing-2026-Q1.docm"}])

    results = {r["id"]: r for r in evaluate_objectives(scenario, RANGE, event_store=store)}

    assert len(results) == len(scenario["objectives"])
    assert all(r["passed"] is None for r in results.values())
    assert "Student's submitted detection" in results["detect_phish"]["unscored"]
    assert sum(r["points_earned"] for r in results.values()) == 0


def test_an_observed_objective_scores_against_the_range_index():
    scenario = _observed(yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text()), "detect_phish")
    store = _Recording([{"event_type": "email", "attachment.name": "briefing-2026-Q1.docm", **_AT}])

    results = {r["id"]: r for r in evaluate_objectives(scenario, RANGE, event_store=store, window_start=START)}

    assert results["detect_phish"]["passed"] is True
    assert results["detect_phish"]["points_earned"] == 10
    assert sum(r["points_earned"] for r in results.values()) == 10
    # Worker/API ingest and the range's own Filebeat/Logstash, as detections/credit.py reads.
    assert store.indices == {"range-rx,range-rx-*"}


def test_the_runner_searches_the_range_indices_credit_reads():
    from app.detections.credit import range_index

    scenario = _observed(yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text()), "detect_phish")
    store = _Recording([])
    evaluate_objectives(scenario, RANGE, event_store=store, window_start=START)
    assert store.indices == {range_index("rx")}


def test_without_a_run_start_observed_objectives_are_unscored():
    scenario = _observed(yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text()), "detect_phish")
    store = _Recording([{"event_type": "email", "attachment.name": "briefing-2026-Q1.docm", **_AT}])
    results = {r["id"]: r for r in evaluate_objectives(scenario, RANGE, event_store=store)}
    assert results["detect_phish"]["passed"] is None and "no exercise window" in results["detect_phish"]["unscored"]


def test_no_objectives_scores_nothing():
    assert evaluate_objectives({"objectives": []}, RANGE, event_store=NullEventStore()) == []


def test_objective_without_a_query_is_unscored_not_match_all():
    # Before 2026-10-06 the runner fell back to match_all: any event passed it.
    scenario = {"objectives": [{"id": "o1", "validator": "opensearch_query",
                                "params": {"min_hits": 0, "require_observed": True}, "points": 5}]}
    [result] = evaluate_objectives(scenario, RANGE, event_store=NullEventStore([{"a": "b"}]))
    assert result["passed"] is None and result["points_earned"] == 0
    assert "no query" in result["unscored"]


def test_min_hits_zero_does_not_pass_on_zero_hits():
    scenario = {"objectives": [{"id": "o1", "validator": "opensearch_query",
                                "params": {"query": "a:nothing", "min_hits": 0, "require_observed": True}, "points": 5}]}
    [result] = evaluate_objectives(scenario, RANGE, event_store=NullEventStore([{"a": "b", **_AT}]), window_start=START)
    assert result["passed"] is None and result["points_earned"] == 0
    assert "0 observed matching events of 1" in result["unscored"]


def test_templated_query_is_rendered_from_scenario_variables():
    scenario = _observed(yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text()), "detect_c2")
    beacon = {"event_type": "http", "url.domain": "cdn-assets.northwind-update.example", **_AT}
    store = _Recording([beacon] * 5)  # min_hits: 5
    results = {r["id"]: r for r in evaluate_objectives(scenario, RANGE, event_store=store, window_start=START)}
    assert results["detect_c2"]["passed"] is True


def test_an_unrendered_placeholder_is_unscored():
    scenario = {"objectives": [{"id": "o1", "validator": "opensearch_query",
                                "params": {"query": "sourceIPAddress:{{ attacker_ip }}", "require_observed": True},
                                "points": 5}]}
    [result] = evaluate_objectives(scenario, RANGE, event_store=NullEventStore([{"sourceIPAddress": "{{"}]))
    assert result["passed"] is None and "attacker_ip" in result["unscored"]
