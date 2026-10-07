"""The CLI runner scores objectives through the ScoringEngine.

Until 2026-10-03 ``evaluate_objectives`` imported ``scenario-engine.validators``, a name no
module can have, so it returned [] and every run reported 0 points.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from runner.run import evaluate_objectives
from scenario_engine.event_stores import NullEventStore
from scenario_engine.scoring import validation_method

ROOT = Path(__file__).resolve().parents[2]
RANGE = {"range_id": "rx", "tenant_id": "t"}


class _Recording(NullEventStore):
    def __init__(self, events):
        super().__init__(events)
        self.indices: set[str] = set()

    async def search(self, index, query, size=20):
        self.indices.add(index)
        return await super().search(index, query, size)


def test_validator_names_map_to_scoring_methods():
    assert validation_method("opensearch_query") == "opensearch_query"
    assert validation_method("validate.opensearch_query") == "opensearch_query"
    assert validation_method("validate.manual_ack") == "manual"
    assert validation_method("validate.opensearch.query") == "opensearch_query"  # ransomware-lite's old spelling
    assert validation_method("validate_opensearch_query") == "opensearch_query"
    assert validation_method("deliverable_check") == "deliverable"
    assert validation_method("") == "manual"


def test_shipped_scenario_scores_against_the_range_index():
    scenario = yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text())
    store = _Recording([{"event_type": "email", "attachment.name": "briefing-2026-Q1.docm"}])

    results = {r["id"]: r for r in evaluate_objectives(scenario, RANGE, event_store=store)}

    assert len(results) == len(scenario["objectives"])
    assert results["detect_phish"]["passed"] is True
    assert results["detect_phish"]["points_earned"] == 10
    assert sum(r["points_earned"] for r in results.values()) == 10
    assert store.indices == {"range-rx"}


def test_no_objectives_scores_nothing():
    assert evaluate_objectives({"objectives": []}, RANGE, event_store=NullEventStore()) == []


def test_objective_without_a_query_is_unscored_not_match_all():
    # Before 2026-10-06 the runner fell back to match_all: any event passed it.
    scenario = {"objectives": [{"id": "o1", "validator": "opensearch_query", "params": {"min_hits": 0}, "points": 5}]}
    [result] = evaluate_objectives(scenario, RANGE, event_store=NullEventStore([{"a": "b"}]))
    assert result["passed"] is None and result["points_earned"] == 0
    assert "no query" in result["unscored"]


def test_min_hits_zero_does_not_pass_on_zero_hits():
    scenario = {"objectives": [{"id": "o1", "validator": "opensearch_query",
                                "params": {"query": "a:nothing", "min_hits": 0}, "points": 5}]}
    [result] = evaluate_objectives(scenario, RANGE, event_store=NullEventStore([{"a": "b"}]))
    assert result["passed"] is False and result["points_earned"] == 0


def test_templated_query_is_rendered_from_scenario_variables():
    scenario = yaml.safe_load((ROOT / "content/scenarios/apt-nation-state/scenario.yaml").read_text())
    beacon = {"event_type": "http", "url.domain": "cdn-assets.northwind-update.example"}
    store = _Recording([beacon] * 5)  # min_hits: 5
    results = {r["id"]: r for r in evaluate_objectives(scenario, RANGE, event_store=store)}
    assert results["detect_c2"]["passed"] is True


def test_an_unrendered_placeholder_is_unscored():
    scenario = {"objectives": [{"id": "o1", "validator": "opensearch_query",
                                "params": {"query": "sourceIPAddress:{{ attacker_ip }}"}, "points": 5}]}
    [result] = evaluate_objectives(scenario, RANGE, event_store=NullEventStore([{"sourceIPAddress": "{{"}]))
    assert result["passed"] is None and "attacker_ip" in result["unscored"]
