"""TrueNorth Range — Grade Calculation.

Handles letter grades, time bonuses, partial credit, penalties,
team aggregation, and percentile ranking.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any


# ── Grade thresholds (configurable) ────────────────────────

_DEFAULT_GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (60.0, "D"),
    (0.0, "F"),
]


@dataclass
class GradeResult:
    """Full grading breakdown for one participant or team."""

    raw_score: int
    max_score: int
    percentage: float
    letter_grade: str
    time_bonus: int
    penalties: int
    final_score: int
    final_percentage: float
    details: dict[str, Any] = field(default_factory=dict)


class GradingCalculator:
    """Stateless grading utilities.

    All methods are class / static so they can be used without
    instantiation.
    """

    grade_thresholds: list[tuple[float, str]] = _DEFAULT_GRADE_THRESHOLDS

    # ── letter grade ────────────────────────────────────────

    @classmethod
    def letter_grade(
        cls,
        percentage: float,
        thresholds: list[tuple[float, str]] | None = None,
    ) -> str:
        """Return a letter grade for the given *percentage*.

        >>> GradingCalculator.letter_grade(92.5)
        'A'
        >>> GradingCalculator.letter_grade(55.0)
        'F'
        """
        for cutoff, grade in (thresholds or cls.grade_thresholds):
            if percentage >= cutoff:
                return grade
        return "F"

    # ── time bonus ──────────────────────────────────────────

    @staticmethod
    def time_bonus(
        elapsed_seconds: int,
        time_limit_seconds: int,
        max_bonus: int,
        curve: str = "linear",
    ) -> int:
        """Calculate time-bonus points for early completion.

        Parameters
        ----------
        elapsed_seconds:
            How long the participant took.
        time_limit_seconds:
            Maximum allowed time for the bonus window.
        max_bonus:
            Maximum bonus points awardable.
        curve:
            ``"linear"`` (default) or ``"exponential"``.

        Returns
        -------
        int
            Bonus points (0 if overtime).
        """
        if elapsed_seconds >= time_limit_seconds or time_limit_seconds <= 0:
            return 0
        ratio = 1.0 - (elapsed_seconds / time_limit_seconds)
        if curve == "exponential":
            ratio = ratio ** 2
        return int(max_bonus * ratio)

    # ── partial credit ──────────────────────────────────────

    @staticmethod
    def partial_credit(
        completed_steps: int,
        total_steps: int,
        max_points: int,
        min_steps_for_credit: int = 1,
    ) -> int:
        """Award partial credit proportional to steps completed.

        Returns
        -------
        int
            Points awarded (can be 0 if below *min_steps_for_credit*).
        """
        if completed_steps < min_steps_for_credit or total_steps <= 0:
            return 0
        return int(max_points * (completed_steps / total_steps))

    # ── penalties ───────────────────────────────────────────

    @staticmethod
    def hint_penalty(
        hints_used: int,
        penalty_per_hint: int = 5,
        max_penalty: int | None = None,
    ) -> int:
        """Calculate deductions for hint usage.

        Returns
        -------
        int
            Total penalty (always >= 0).
        """
        penalty = hints_used * penalty_per_hint
        if max_penalty is not None:
            penalty = min(penalty, max_penalty)
        return penalty

    # ── full grade ──────────────────────────────────────────

    @classmethod
    def calculate(
        cls,
        raw_score: int,
        max_score: int,
        elapsed_seconds: int = 0,
        time_limit_seconds: int = 0,
        max_time_bonus: int = 0,
        hints_used: int = 0,
        penalty_per_hint: int = 5,
    ) -> GradeResult:
        """Compute the full grade for one participant.

        Combines raw score, time bonus, and hint penalties into a final
        result with letter grade.
        """
        bonus = cls.time_bonus(elapsed_seconds, time_limit_seconds, max_time_bonus)
        penalty = cls.hint_penalty(hints_used, penalty_per_hint)
        final = max(raw_score + bonus - penalty, 0)
        effective_max = max_score + max_time_bonus
        pct = (final / effective_max * 100) if effective_max > 0 else 0.0
        return GradeResult(
            raw_score=raw_score,
            max_score=max_score,
            percentage=round(raw_score / max_score * 100, 2) if max_score else 0.0,
            letter_grade=cls.letter_grade(pct),
            time_bonus=bonus,
            penalties=penalty,
            final_score=final,
            final_percentage=round(pct, 2),
        )

    # ── team scoring ────────────────────────────────────────

    @staticmethod
    def team_score(individual_scores: list[int]) -> dict[str, Any]:
        """Aggregate individual scores into a team result.

        Returns dict with ``total``, ``average``, ``min``, ``max``,
        and ``count``.
        """
        if not individual_scores:
            return {"total": 0, "average": 0.0, "min": 0, "max": 0, "count": 0}
        return {
            "total": sum(individual_scores),
            "average": round(statistics.mean(individual_scores), 2),
            "min": min(individual_scores),
            "max": max(individual_scores),
            "count": len(individual_scores),
        }

    # ── comparative / percentile ────────────────────────────

    @staticmethod
    def percentile_rank(score: int, all_scores: list[int]) -> float:
        """Return the percentile rank (0-100) of *score* within *all_scores*.

        Uses the "percentage of scores below" method.

        >>> GradingCalculator.percentile_rank(80, [60, 70, 80, 90, 100])
        40.0
        """
        if not all_scores:
            return 0.0
        below = sum(1 for s in all_scores if s < score)
        return round(below / len(all_scores) * 100, 2)

    @staticmethod
    def comparative_ranking(
        scores: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Rank participants/teams and include percentiles.

        Parameters
        ----------
        scores:
            Mapping of ``team_id`` → ``score``.

        Returns
        -------
        list[dict]
            Sorted (descending) list of ``{team_id, score, rank, percentile}``.
        """
        sorted_items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        all_values = list(scores.values())
        result: list[dict[str, Any]] = []
        for rank, (team_id, score) in enumerate(sorted_items, start=1):
            result.append(
                {
                    "team_id": team_id,
                    "score": score,
                    "rank": rank,
                    "percentile": GradingCalculator.percentile_rank(score, all_values),
                }
            )
        return result