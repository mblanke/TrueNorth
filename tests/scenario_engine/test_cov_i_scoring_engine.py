"""COV-I: scenario_engine/scoring/engine.py — the evaluate() aggregation, unknown
objectives, dependency gating, the scoreboard, partial credit and the time bonus.

Whole-slice coverage before this file: 88.2% (105/119 statements); the missing lines were
the generic-exception branch of evaluate(), unknown-objective results, dependency checks
that actually gate, get_scoreboard(), the partial-credit ratio and the 25% time bonus.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from scenario_engine.scoring import ScoringEngine
from scenario_engine.scoring.engine import Objective

MANUAL_OK = {"id": "m1", "name": "Instructor ack", "max_points": 40,
             "validation_method": "manual", "validation_config": {"approved": True}}
MANUAL_PENDING = {"id": "m2", "name": "Awaiting ack", "max_points": 10,
                  "validation_method": "manual", "validation_config": {"notes": "waiting"}}


def _engine(objectives, **kw):
    return ScoringEngine("ex-1", objectives, **kw)


def test_a_manual_objective_the_instructor_approved_is_judged_achieved():
    result = asyncio.run(_engine([MANUAL_OK]).evaluate())
    assert result.total_score == 40 and result.max_score == 40 and result.percentage == 100.0
    assert result.grade == "A" and result.unscored == {} and result.unscored_points == 0
    [obj] = result.objectives
    assert obj.achieved and obj.points_awarded == 40
    assert obj.evidence == [{"manual_review": True, "approved": True, "notes": ""}]
    assert "achieved" in obj.feedback and obj.timestamp is not None


def test_an_awaited_manual_objective_is_unscored_not_failed():
    """No instructor approval yet: reported apart, never as failed and never graded F."""
    result = asyncio.run(_engine([MANUAL_OK, MANUAL_PENDING]).evaluate())
    assert result.unscored == {"m2": "awaiting instructor acknowledgement"}
    # Only what was judged counts towards the percentage.
    assert result.total_score == 40 and result.max_score == 40
    assert result.unscored_points == 10
    assert result.grade == "A"


def test_nothing_judged_is_no_grade_not_an_f():
    result = asyncio.run(_engine([MANUAL_PENDING]).evaluate())
    assert result.total_score == 0 and result.grade is None and result.percentage == 0.0


def test_an_unknown_validator_is_unscored_with_its_reason():
    bad = {"id": "b1", "name": "Psychic", "max_points": 5, "validation_method": "psychic",
           "validation_config": {}}
    result = asyncio.run(_engine([bad]).evaluate())
    assert "b1" in result.unscored and result.unscored_points == 5
    assert result.objectives == []


def test_an_unexpected_failure_is_reported_apart_as_evaluation_failed():
    """A validator that dies unexpectedly (here: a non-dict config) must not fail the run
    or look unscored-for-a-reason: it is 'evaluation failed: ...'."""
    broken = {"id": "b1", "name": "Broken", "max_points": 7, "validation_method": "manual",
              "validation_config": "not a dict"}
    result = asyncio.run(_engine([broken]).evaluate())
    assert result.unscored["b1"].startswith("evaluation failed: ")
    assert result.unscored_points == 7 and result.objectives == []


def test_evaluating_an_unknown_objective_reports_it_not_scored():
    result = asyncio.run(_engine([MANUAL_OK]).evaluate_objective("nope"))
    assert result.achieved is False and result.points_awarded == 0 and result.max_points == 0
    assert result.feedback == "Unknown objective: nope" and result.evidence == []


def test_a_dependency_the_engine_has_not_judged_gates_the_objective():
    gated = {"id": "g1", "name": "After ack", "max_points": 20, "validation_method": "manual",
             "validation_config": {"approved": True}, "depends_on": ["m1"]}
    engine = _engine([MANUAL_OK, gated])
    result = asyncio.run(engine.evaluate_objective("g1"))
    assert result.achieved is False and result.points_awarded == 0
    assert result.max_points == 20 and result.feedback == "Dependency not met: m1"
    # ...and once the dependency is judged achieved, the gate opens.
    asyncio.run(engine.evaluate_objective("m1"))
    opened = asyncio.run(engine.evaluate_objective("g1"))
    assert opened.achieved and opened.points_awarded == 20


def test_the_scoreboard_reports_each_objective_before_and_after_judging():
    engine = _engine([MANUAL_OK, MANUAL_PENDING])
    board = engine.get_scoreboard()
    assert board["exercise_id"] == "ex-1" and board["total_score"] == 0
    assert board["max_score"] == 50 and board["percentage"] == 0
    assert board["objectives"]["m1"] == {"name": "Instructor ack", "max_points": 40,
                                         "awarded": 0, "achieved": False}
    asyncio.run(engine.evaluate())
    board = engine.get_scoreboard()
    assert board["total_score"] == 40 and board["percentage"] == 80.0
    assert board["objectives"]["m1"]["awarded"] == 40 and board["objectives"]["m1"]["achieved"] is True
    assert board["objectives"]["m2"]["achieved"] is False


def test_partial_credit_is_capped_at_half_the_points():
    obj = Objective.from_dict({"id": "p1", "name": "Deliverable", "description": "",
                               "max_points": 30, "partial_credit": True,
                               "validation_config": {"threshold": 4}})
    engine = _engine([MANUAL_OK])
    # Two of four hits: 2/4 of the points, not more than half even with all hits found.
    assert engine._calculate_points(obj, achieved=False, evidence=[{"hit": 1}, {"hit": 2}]) == 15
    assert engine._calculate_points(obj, achieved=False, evidence=[{"hit": i} for i in range(9)]) == 15


def test_an_error_report_is_not_evidence_for_partial_credit():
    obj = Objective.from_dict({"id": "p1", "name": "Deliverable", "description": "",
                               "max_points": 30, "partial_credit": True,
                               "validation_config": {"threshold": 4}})
    engine = _engine([MANUAL_OK])
    evidence = [{"error": "deliverable not found"}, "junk"]
    assert engine._calculate_points(obj, achieved=False, evidence=evidence) == 0


def test_a_fast_finish_earns_the_time_bonus():
    fast = {"id": "m1", "name": "Quick ack", "max_points": 40, "validation_method": "manual",
            "validation_config": {"approved": True}, "time_bonus": True, "time_limit_seconds": 600}
    started = datetime.now(UTC) - timedelta(seconds=5)
    result = asyncio.run(_engine([fast], start_time=started).evaluate())
    assert result.total_score == 50  # 40 + 25%
    [bonus] = result.bonuses
    assert bonus["objective_id"] == "m1" and bonus["type"] == "time_bonus" and bonus["points"] == 10
    assert "limit 600" in bonus["reason"]


def test_over_the_time_limit_there_is_no_bonus():
    slow = {"id": "m1", "name": "Slow ack", "max_points": 40, "validation_method": "manual",
            "validation_config": {"approved": True}, "time_bonus": True, "time_limit_seconds": 600}
    started = datetime.now(UTC) - timedelta(seconds=601)
    result = asyncio.run(_engine([slow], start_time=started).evaluate())
    assert result.total_score == 40 and result.bonuses == []


def test_no_time_limit_means_no_bonus_even_when_asked():
    odd = {"id": "m1", "name": "No limit", "max_points": 40, "validation_method": "manual",
           "validation_config": {"approved": True}, "time_bonus": True, "time_limit_seconds": 0}
    assert asyncio.run(_engine([odd]).evaluate()).total_score == 40
