# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scenario_engine.scoring.grading: the pure grading math — letter grades against the
default thresholds and custom ones, the linear/exponential time bonus and its overtime and
zero-limit edges, partial credit below the minimum-step floor, hint penalties capped at the
ceiling, the full calculate() breakdown (including a floored final score and the bonus-widened
percentage), team aggregation with and without members, and the percentile/comparative
ranking that never exceeds 100. Everything here is pure computation: no Redis, no DB, no
network — and no live exercise run, which is why the scenario runner itself stays uncovered."""

from __future__ import annotations

from scenario_engine.scoring.grading import GradingCalculator as G


def test_letter_grade_walks_the_thresholds_in_order():
    assert G.letter_grade(92.5) == "A"
    assert G.letter_grade(90.0) == "A"  # the cutoff itself is inclusive
    assert G.letter_grade(89.999) == "B"
    assert G.letter_grade(60.0) == "D"
    assert G.letter_grade(0.0) == "F"
    assert G.letter_grade(55.0) == "F"
    assert G.letter_grade(-5.0) == "F"
    # A custom scale replaces the default entirely.
    assert G.letter_grade(75.0, thresholds=[(75.0, "pass"), (0.0, "fail")]) == "pass"
    # A scale with no matching cutoff still degrades to F.
    assert G.letter_grade(50.0, thresholds=[(90.0, "A")]) == "F"


def test_time_bonus_is_linear_and_shrinks_with_elapsed_time():
    assert G.time_bonus(0, 600, 60) == 60
    assert G.time_bonus(300, 600, 60) == 30  # half the window, half the bonus
    assert G.time_bonus(1, 600, 60) == 59
    # Overtime, the full limit, and a zero/negative limit earn nothing.
    assert G.time_bonus(600, 600, 60) == 0
    assert G.time_bonus(601, 600, 60) == 0
    assert G.time_bonus(0, 0, 60) == 0
    assert G.time_bonus(0, -5, 60) == 0


def test_time_bonus_exponential_curve_front_loads_the_points():
    linear = G.time_bonus(300, 600, 100)
    expo = G.time_bonus(300, 600, 100, curve="exponential")
    assert linear == 50 and expo == 25  # (0.5)**2
    assert G.time_bonus(0, 600, 100, curve="exponential") == 100
    assert expo < linear


def test_partial_credit_is_proportional_and_gated_by_the_minimum():
    assert G.partial_credit(5, 10, 40) == 20
    assert G.partial_credit(10, 10, 40) == 40
    # Below min_steps_for_credit, and empty/negative totals, earn nothing.
    assert G.partial_credit(0, 10, 40) == 0
    assert G.partial_credit(3, 10, 40, min_steps_for_credit=4) == 0
    assert G.partial_credit(1, 0, 40) == 0
    assert G.partial_credit(1, -1, 40) == 0
    # Rounding truncates rather than rounds up.
    assert G.partial_credit(1, 3, 100) == 33


def test_hint_penalty_scales_and_caps():
    assert G.hint_penalty(0) == 0
    assert G.hint_penalty(3) == 15
    assert G.hint_penalty(3, penalty_per_hint=10) == 30
    assert G.hint_penalty(10, max_penalty=20) == 20
    assert G.hint_penalty(1, max_penalty=20) == 5  # under the cap the cap does nothing


def test_calculate_combines_bonus_penalty_and_letter_grade():
    # 90 raw of 100, finished half inside a 60 s window with 10 bonus points on offer,
    # two hints at 5 each: 90 + 5 - 10 = 85 of an effective 110.
    result = G.calculate(90, 100, elapsed_seconds=300, time_limit_seconds=600, max_time_bonus=10, hints_used=2)
    assert result.raw_score == 90 and result.max_score == 100
    assert result.percentage == 90.0
    assert result.letter_grade == "C"  # 85 of an effective 110 is 77.27 %
    assert result.time_bonus == 5
    assert result.penalties == 10
    assert result.final_score == 85
    assert result.final_percentage == round(85 / 110 * 100, 2)


def test_calculate_never_reaches_a_negative_final_score():
    # Penalties exceed every point the participant could hold: the floor is 0, not negative.
    result = G.calculate(10, 100, hints_used=10, penalty_per_hint=5)
    assert result.final_score == 0
    assert result.penalties == 50
    # No bonus window at all: bonus stays 0 and max_score 0 keeps every percentage 0.
    result = G.calculate(0, 0)
    assert result.percentage == 0.0 and result.final_percentage == 0.0
    assert result.letter_grade == "F"


def test_team_score_aggregates_and_sums_the_empty_case():
    assert G.team_score([]) == {"total": 0, "average": 0.0, "min": 0, "max": 0, "count": 0}
    scores = G.team_score([70, 90, 85])
    assert scores == {"total": 245, "average": round(245 / 3, 2), "min": 70, "max": 90, "count": 3}


def test_percentile_rank_counts_the_scores_strictly_below():
    # 60 and 70 are below 80 of the five scores: 2/5 = 40 %.
    assert G.percentile_rank(80, [60, 70, 80, 90, 100]) == 40.0
    assert G.percentile_rank(60, [60, 70, 80]) == 0.0  # ties do not count as below
    assert G.percentile_rank(100, [60]) == 100.0
    assert G.percentile_rank(50, []) == 0.0  # no cohort, no percentile


def test_comparative_ranking_sorts_descending_with_percentiles():
    ranked = G.comparative_ranking({"red": 90, "blue": 60, "gold": 90})
    # Ties stay in mapping order (the sort is stable), so red precedes gold.
    assert [row["team_id"] for row in ranked] == ["red", "gold", "blue"]
    assert [row["rank"] for row in ranked] == [1, 2, 3]
    assert [row["score"] for row in ranked] == [90, 90, 60]
    # 60 beats nobody strictly below; 90 beats exactly blue.
    assert [row["percentile"] for row in ranked] == [round(1 / 3 * 100, 2), round(1 / 3 * 100, 2), 0.0]


def test_comparative_ranking_of_no_teams_is_empty():
    assert G.comparative_ranking({}) == []


def test_grade_result_defaults_to_empty_details():
    from dataclasses import fields

    from scenario_engine.scoring.grading import GradeResult

    detail_field = next(f for f in fields(GradeResult) if f.name == "details")
    assert detail_field.default_factory is dict  # a fresh dict per instance, not a shared one
    result = G.calculate(50, 100)
    assert result.details == {}
    assert G.calculate(60, 100).details is not result.details
