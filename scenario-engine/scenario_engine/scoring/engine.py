"""TrueNorth Range — Real-time Scoring Engine.

Evaluates trainee performance against defined objectives during
cyber-range exercises.  Supports OpenSearch-backed validation,
deliverable checks, time bonuses, and partial credit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────


@dataclass
class Objective:
    """A single scoreable objective within an exercise."""

    id: str
    name: str
    description: str
    max_points: int
    objective_type: str  # detection, containment, eradication, recovery, analysis
    validation_method: str  # opensearch_query, deliverable, manual, automated
    validation_config: dict[str, Any]
    time_bonus: bool = False
    time_limit_seconds: int = 0
    partial_credit: bool = True
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Objective:
        """Construct an *Objective* from a plain dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            max_points=data.get("max_points", 0),
            objective_type=data.get("objective_type", "detection"),
            validation_method=data.get("validation_method", "manual"),
            validation_config=data.get("validation_config", {}),
            time_bonus=data.get("time_bonus", False),
            time_limit_seconds=data.get("time_limit_seconds", 0),
            partial_credit=data.get("partial_credit", True),
            depends_on=data.get("depends_on", []),
        )


@dataclass
class ObjectiveResult:
    """Result of evaluating one objective."""

    objective_id: str
    achieved: bool
    points_awarded: int
    max_points: int
    evidence: list[dict[str, Any]]
    timestamp: datetime | None
    feedback: str


@dataclass
class ScoringResult:
    """Aggregate result for an entire exercise evaluation."""

    exercise_id: str
    total_score: int
    max_score: int
    percentage: float
    grade: str  # A, B, C, D, F
    objectives: list[ObjectiveResult]
    time_elapsed: int  # seconds
    bonuses: list[dict[str, Any]]


# ── Scoring Engine ──────────────────────────────────────────


class ScoringEngine:
    """Real-time scoring engine for cyber exercises.

    Parameters
    ----------
    exercise_id:
        Unique exercise identifier.
    objectives:
        List of objective definitions (dicts).
    opensearch_url:
        Optional OpenSearch endpoint for query-based validation.
    start_time:
        Exercise start timestamp; defaults to *now*.
    """

    def __init__(
        self,
        exercise_id: str,
        objectives: list[dict[str, Any]],
        opensearch_url: str | None = None,
        start_time: datetime | None = None,
    ) -> None:
        self.exercise_id = exercise_id
        self.objectives: dict[str, Objective] = {
            obj["id"]: Objective.from_dict(obj) for obj in objectives
        }
        self.opensearch_url = opensearch_url
        self.start_time = start_time or datetime.now(timezone.utc)
        self.total_score: int = 0
        self.max_score: int = sum(
            obj.max_points for obj in self.objectives.values()
        )
        self._results: dict[str, ObjectiveResult] = {}
        self._bonuses: list[dict[str, Any]] = []

    # ── public API ──────────────────────────────────────────

    async def evaluate(self) -> ScoringResult:
        """Evaluate **all** objectives and return the aggregate result."""
        tasks = [
            self.evaluate_objective(oid) for oid in self.objectives
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        objective_results: list[ObjectiveResult] = []
        for res in results:
            if isinstance(res, Exception):
                logger.error("Objective evaluation failed: %s", res)
                continue
            objective_results.append(res)

        self.total_score = sum(r.points_awarded for r in objective_results)
        percentage = (
            (self.total_score / self.max_score * 100) if self.max_score else 0.0
        )

        from scenario_engine.scoring.grading import GradingCalculator

        grade = GradingCalculator.letter_grade(percentage)

        elapsed = int(
            (datetime.now(timezone.utc) - self.start_time).total_seconds()
        )

        return ScoringResult(
            exercise_id=self.exercise_id,
            total_score=self.total_score,
            max_score=self.max_score,
            percentage=round(percentage, 2),
            grade=grade,
            objectives=objective_results,
            time_elapsed=elapsed,
            bonuses=list(self._bonuses),
        )

    async def evaluate_objective(self, objective_id: str) -> ObjectiveResult:
        """Evaluate a single objective by *objective_id*."""
        obj = self.objectives.get(objective_id)
        if obj is None:
            return ObjectiveResult(
                objective_id=objective_id,
                achieved=False,
                points_awarded=0,
                max_points=0,
                evidence=[],
                timestamp=None,
                feedback=f"Unknown objective: {objective_id}",
            )

        # Check dependencies
        for dep_id in obj.depends_on:
            dep_result = self._results.get(dep_id)
            if dep_result is None or not dep_result.achieved:
                return ObjectiveResult(
                    objective_id=objective_id,
                    achieved=False,
                    points_awarded=0,
                    max_points=obj.max_points,
                    evidence=[],
                    timestamp=None,
                    feedback=f"Dependency not met: {dep_id}",
                )

        # Validate
        from scenario_engine.scoring.validators import ScoringValidator

        achieved, evidence = await ScoringValidator.validate(
            method=obj.validation_method,
            config=obj.validation_config,
            opensearch_url=self.opensearch_url,
        )

        # Score
        points = self._calculate_points(obj, achieved, evidence)

        result = ObjectiveResult(
            objective_id=objective_id,
            achieved=achieved,
            points_awarded=points,
            max_points=obj.max_points,
            evidence=evidence,
            timestamp=datetime.now(timezone.utc) if achieved else None,
            feedback=self._build_feedback(obj, achieved, points),
        )
        self._results[objective_id] = result
        return result

    def get_scoreboard(self) -> dict[str, Any]:
        """Return current scoreboard suitable for display."""
        return {
            "exercise_id": self.exercise_id,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "percentage": round(
                (self.total_score / self.max_score * 100) if self.max_score else 0,
                2,
            ),
            "objectives": {
                oid: {
                    "name": obj.name,
                    "max_points": obj.max_points,
                    "awarded": self._results[oid].points_awarded
                    if oid in self._results
                    else 0,
                    "achieved": self._results[oid].achieved
                    if oid in self._results
                    else False,
                }
                for oid, obj in self.objectives.items()
            },
            "elapsed_seconds": int(
                (datetime.now(timezone.utc) - self.start_time).total_seconds()
            ),
        }

    # ── helpers ─────────────────────────────────────────────

    def _calculate_points(
        self,
        obj: Objective,
        achieved: bool,
        evidence: list[dict[str, Any]],
    ) -> int:
        if not achieved:
            if obj.partial_credit and evidence:
                # Award partial credit proportional to evidence gathered
                ratio = min(len(evidence) / max(obj.validation_config.get("threshold", 1), 1), 0.5)
                return int(obj.max_points * ratio)
            return 0

        points = obj.max_points

        # Time bonus
        if obj.time_bonus and obj.time_limit_seconds > 0:
            elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
            if elapsed < obj.time_limit_seconds:
                bonus = int(obj.max_points * 0.25)  # 25 % bonus
                self._bonuses.append(
                    {
                        "objective_id": obj.id,
                        "type": "time_bonus",
                        "points": bonus,
                        "reason": f"Completed in {int(elapsed)}s (limit {obj.time_limit_seconds}s)",
                    }
                )
                points += bonus

        return points

    @staticmethod
    def _build_feedback(obj: Objective, achieved: bool, points: int) -> str:
        if achieved:
            return f"Objective '{obj.name}' achieved — {points}/{obj.max_points} points."
        return f"Objective '{obj.name}' not yet achieved (0/{obj.max_points})."