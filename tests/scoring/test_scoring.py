"""Tests for TrueNorth Range — Scoring Engine."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime

import pytest

# We test via the package path under scenario_engine/scoring/
from scenario_engine.scoring.engine import (
    Objective,
    ObjectiveResult,
    ScoringEngine,
    ScoringResult,
)
from scenario_engine.scoring.grading import GradeResult, GradingCalculator
from scenario_engine.scoring.leaderboard import Leaderboard
from scenario_engine.scoring.validators import ScoringValidator

# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def sample_objectives() -> list[dict]:
    """Return a list of sample objective definitions."""
    return [
        {
            "id": "obj-detect-1",
            "name": "Detect lateral movement",
            "description": "Identify PsExec usage in Sysmon logs",
            "max_points": 20,
            "objective_type": "detection",
            "validation_method": "manual",
            "validation_config": {"approved": True, "notes": "Good work"},
            "time_bonus": False,
            "partial_credit": True,
        },
        {
            "id": "obj-contain-1",
            "name": "Isolate infected host",
            "description": "Network-isolate the compromised workstation",
            "max_points": 30,
            "objective_type": "containment",
            "validation_method": "manual",
            "validation_config": {"approved": False},
            "time_bonus": True,
            "time_limit_seconds": 600,
            "partial_credit": False,
            "depends_on": [],
        },
        {
            "id": "obj-report-1",
            "name": "Submit incident report",
            "description": "Write and submit a complete IR report",
            "max_points": 50,
            "objective_type": "analysis",
            "validation_method": "deliverable",
            "validation_config": {
                "deliverable_type": "incident_report",
                "storage_path": "",
                "min_size_bytes": 10,
            },
            "time_bonus": False,
            "partial_credit": True,
        },
    ]


@pytest.fixture
def engine(sample_objectives: list[dict]) -> ScoringEngine:
    return ScoringEngine(
        exercise_id="test-ex-001",
        objectives=sample_objectives,
        start_time=datetime(2025, 1, 1, tzinfo=UTC),
    )


# ── Objective dataclass tests ──────────────────────────────


class TestObjective:
    def test_from_dict_minimal(self):
        data = {
            "id": "obj-1",
            "name": "Test",
            "max_points": 10,
        }
        obj = Objective.from_dict(data)
        assert obj.id == "obj-1"
        assert obj.name == "Test"
        assert obj.max_points == 10
        assert obj.objective_type == "detection"
        assert obj.validation_method == "manual"
        assert obj.depends_on == []

    def test_from_dict_full(self, sample_objectives):
        obj = Objective.from_dict(sample_objectives[1])
        assert obj.id == "obj-contain-1"
        assert obj.time_bonus is True
        assert obj.time_limit_seconds == 600
        assert obj.partial_credit is False


# ── ScoringEngine tests ───────────────────────────────────


class TestScoringEngine:
    def test_init(self, engine: ScoringEngine, sample_objectives):
        assert engine.exercise_id == "test-ex-001"
        assert len(engine.objectives) == len(sample_objectives)
        assert engine.max_score == 100  # 20 + 30 + 50
        assert engine.total_score == 0

    @pytest.mark.asyncio
    async def test_evaluate_single_manual_approved(self, engine: ScoringEngine):
        result = await engine.evaluate_objective("obj-detect-1")
        assert isinstance(result, ObjectiveResult)
        assert result.achieved is True
        assert result.points_awarded == 20
        assert result.objective_id == "obj-detect-1"

    @pytest.mark.asyncio
    async def test_evaluate_single_manual_not_approved(self, engine: ScoringEngine):
        result = await engine.evaluate_objective("obj-contain-1")
        assert result.achieved is False
        assert result.points_awarded == 0

    @pytest.mark.asyncio
    async def test_evaluate_unknown_objective(self, engine: ScoringEngine):
        result = await engine.evaluate_objective("does-not-exist")
        assert result.achieved is False
        assert "Unknown" in result.feedback

    @pytest.mark.asyncio
    async def test_evaluate_all(self, engine: ScoringEngine):
        result = await engine.evaluate()
        assert isinstance(result, ScoringResult)
        assert result.exercise_id == "test-ex-001"
        assert result.max_score == 100
        # Only manual-approved objective should give points
        assert result.total_score == 20
        assert result.grade in ("A", "B", "C", "D", "F")

    @pytest.mark.asyncio
    async def test_evaluate_deliverable_with_real_file(self, sample_objectives):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Incident Report\n" * 10)
            tmp_path = f.name

        try:
            sample_objectives[2]["validation_config"]["storage_path"] = tmp_path
            eng = ScoringEngine(
                exercise_id="test-deliverable",
                objectives=sample_objectives,
            )
            result = await eng.evaluate_objective("obj-report-1")
            assert result.achieved is True
            assert result.points_awarded == 50
        finally:
            os.unlink(tmp_path)

    def test_get_scoreboard(self, engine: ScoringEngine):
        board = engine.get_scoreboard()
        assert board["exercise_id"] == "test-ex-001"
        assert board["max_score"] == 100
        assert "objectives" in board


# ── GradingCalculator tests ───────────────────────────────


class TestGradingCalculator:
    @pytest.mark.parametrize(
        "pct, expected",
        [
            (95, "A"),
            (90, "A"),
            (89.9, "B"),
            (80, "B"),
            (75, "C"),
            (60, "D"),
            (59, "F"),
            (0, "F"),
        ],
    )
    def test_letter_grade(self, pct, expected):
        assert GradingCalculator.letter_grade(pct) == expected

    def test_time_bonus_early(self):
        bonus = GradingCalculator.time_bonus(
            elapsed_seconds=100,
            time_limit_seconds=600,
            max_bonus=30,
        )
        assert bonus > 0
        assert bonus <= 30

    def test_time_bonus_overtime(self):
        bonus = GradingCalculator.time_bonus(
            elapsed_seconds=700,
            time_limit_seconds=600,
            max_bonus=30,
        )
        assert bonus == 0

    def test_time_bonus_exponential(self):
        bonus = GradingCalculator.time_bonus(
            elapsed_seconds=100,
            time_limit_seconds=600,
            max_bonus=30,
            curve="exponential",
        )
        assert bonus > 0

    def test_partial_credit(self):
        points = GradingCalculator.partial_credit(
            completed_steps=3,
            total_steps=10,
            max_points=100,
        )
        assert points == 30

    def test_partial_credit_zero_steps(self):
        points = GradingCalculator.partial_credit(
            completed_steps=0,
            total_steps=10,
            max_points=100,
        )
        assert points == 0

    def test_hint_penalty(self):
        penalty = GradingCalculator.hint_penalty(hints_used=3, penalty_per_hint=5)
        assert penalty == 15

    def test_hint_penalty_with_max(self):
        penalty = GradingCalculator.hint_penalty(hints_used=100, penalty_per_hint=5, max_penalty=20)
        assert penalty == 20

    def test_calculate_full_grade(self):
        result = GradingCalculator.calculate(
            raw_score=85,
            max_score=100,
            elapsed_seconds=200,
            time_limit_seconds=600,
            max_time_bonus=10,
            hints_used=1,
            penalty_per_hint=5,
        )
        assert isinstance(result, GradeResult)
        assert result.raw_score == 85
        assert result.final_score > 0
        assert result.letter_grade in ("A", "B", "C", "D", "F")

    def test_team_score(self):
        result = GradingCalculator.team_score([80, 90, 70, 85])
        assert result["total"] == 325
        assert result["average"] == 81.25
        assert result["min"] == 70
        assert result["max"] == 90
        assert result["count"] == 4

    def test_team_score_empty(self):
        result = GradingCalculator.team_score([])
        assert result["total"] == 0

    def test_percentile_rank(self):
        pct = GradingCalculator.percentile_rank(80, [60, 70, 80, 90, 100])
        assert pct == 40.0

    def test_comparative_ranking(self):
        scores = {"alpha": 90, "bravo": 75, "charlie": 95, "delta": 80}
        ranked = GradingCalculator.comparative_ranking(scores)
        assert ranked[0]["team_id"] == "charlie"
        assert ranked[0]["rank"] == 1
        assert ranked[-1]["team_id"] == "bravo"
        assert len(ranked) == 4


# ── Leaderboard tests ─────────────────────────────────────


class TestLeaderboard:
    @pytest.fixture
    def lb(self) -> Leaderboard:
        return Leaderboard(redis_url=None)  # in-memory

    @pytest.mark.asyncio
    async def test_update_and_get(self, lb: Leaderboard):
        await lb.update_score("ex-1", "team-a", 100)
        await lb.update_score("ex-1", "team-b", 200)
        await lb.update_score("ex-1", "team-c", 150)
        board = await lb.get_leaderboard("ex-1", top_n=10)
        assert len(board) == 3
        assert board[0]["team_id"] == "team-b"
        assert board[0]["score"] == 200
        assert board[0]["rank"] == 1

    @pytest.mark.asyncio
    async def test_get_team_rank(self, lb: Leaderboard):
        await lb.update_score("ex-1", "team-x", 50)
        await lb.update_score("ex-1", "team-y", 100)
        rank = await lb.get_team_rank("ex-1", "team-y")
        assert rank == 1
        rank2 = await lb.get_team_rank("ex-1", "team-x")
        assert rank2 == 2

    @pytest.mark.asyncio
    async def test_get_team_rank_not_found(self, lb: Leaderboard):
        rank = await lb.get_team_rank("ex-1", "ghost")
        assert rank == 0

    @pytest.mark.asyncio
    async def test_get_historical(self, lb: Leaderboard):
        await lb.update_score("ex-1", "team-h", 80)
        await lb.update_score("ex-2", "team-h", 90)
        history = await lb.get_historical("team-h")
        assert len(history) == 2
        assert history[0]["exercise_id"] == "ex-1"

    @pytest.mark.asyncio
    async def test_clear(self, lb: Leaderboard):
        await lb.update_score("ex-c", "team-1", 10)
        await lb.clear("ex-c")
        board = await lb.get_leaderboard("ex-c")
        assert len(board) == 0

    @pytest.mark.asyncio
    async def test_top_n_limit(self, lb: Leaderboard):
        for i in range(20):
            await lb.update_score("ex-big", f"team-{i}", i * 10)
        board = await lb.get_leaderboard("ex-big", top_n=5)
        assert len(board) == 5
        assert board[0]["score"] == 190


# ── ScoringValidator tests ─────────────────────────────────


class TestScoringValidator:
    @pytest.mark.asyncio
    async def test_manual_approved(self):
        ok, evidence = await ScoringValidator.validate(
            method="manual",
            config={"approved": True, "notes": "Looks good"},
        )
        assert ok is True
        assert evidence[0]["approved"] is True

    @pytest.mark.asyncio
    async def test_manual_not_approved(self):
        ok, evidence = await ScoringValidator.validate(
            method="manual",
            config={"approved": False},
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_deliverable_missing_file(self):
        ok, evidence = await ScoringValidator.validate(
            method="deliverable",
            config={
                "storage_path": "/tmp/nonexistent_file_12345.txt",
                "min_size_bytes": 10,
            },
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_deliverable_valid_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("x" * 200)
            path = f.name
        try:
            ok, evidence = await ScoringValidator.validate(
                method="deliverable",
                config={"storage_path": path, "min_size_bytes": 10},
            )
            assert ok is True
            assert evidence[0]["size_bytes"] >= 200
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_deliverable_json_validation(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"title": "Report", "findings": []}, f)
            path = f.name
        try:
            ok, evidence = await ScoringValidator.validate(
                method="deliverable",
                config={
                    "storage_path": path,
                    "min_size_bytes": 5,
                    "required_fields": ["title", "findings"],
                },
            )
            assert ok is True
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_deliverable_json_missing_fields(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"title": "Report"}, f)
            path = f.name
        try:
            ok, evidence = await ScoringValidator.validate(
                method="deliverable",
                config={
                    "storage_path": path,
                    "min_size_bytes": 5,
                    "required_fields": ["title", "indicators", "timeline"],
                },
            )
            assert ok is False
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_opensearch_no_url(self):
        ok, evidence = await ScoringValidator.validate(
            method="opensearch_query",
            config={"index": "test-*", "query": {"match_all": {}}, "threshold": 1},
            opensearch_url=None,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_unknown_method(self):
        ok, evidence = await ScoringValidator.validate(
            method="does_not_exist",
            config={},
        )
        assert ok is False
        assert evidence == []

    @pytest.mark.asyncio
    async def test_firewall_rule(self):
        ok, evidence = await ScoringValidator.validate(
            method="firewall_rule",
            config={"expected_rules": [{"port": 443, "protocol": "tcp", "action": "block"}]},
        )
        # Stub always returns True for now
        assert isinstance(ok, bool)
        assert len(evidence) == 1

    @pytest.mark.asyncio
    async def test_process_killed(self):
        ok, evidence = await ScoringValidator.validate(
            method="process_killed",
            config={"process_name": "beacon.exe"},
        )
        # Stub returns False (pending verification)
        assert ok is False
        assert evidence[0]["process_name"] == "beacon.exe"

    @pytest.mark.asyncio
    async def test_user_disabled(self):
        ok, evidence = await ScoringValidator.validate(
            method="user_disabled",
            config={"username": "jsmith"},
        )
        assert ok is False
        assert evidence[0]["username"] == "jsmith"

    @pytest.mark.asyncio
    async def test_network_isolated(self):
        ok, evidence = await ScoringValidator.validate(
            method="network_isolated",
            config={"host_ip": "10.0.30.101"},
        )
        assert ok is False
        assert evidence[0]["host_ip"] == "10.0.30.101"
