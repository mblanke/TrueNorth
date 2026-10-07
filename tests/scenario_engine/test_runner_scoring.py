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
