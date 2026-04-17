"""Tests for xAPI statement builder."""

import uuid

from app.xapi import (
    VERBS,
    build_statement,
    exercise_completed,
    exercise_launched,
    objective_achieved,
    scenario_started,
)


class TestXAPIStatements:
    def test_build_basic_statement(self):
        """build_statement produces all required xAPI fields."""
        stmt = build_statement(
            "launched",
            "user@test.com",
            "Tester",
            "exercise",
            "ex-123",
            "My Exercise",
        )
        for key in ("id", "actor", "verb", "object", "timestamp", "context"):
            assert key in stmt, f"Missing required field: {key}"

    def test_exercise_launched(self):
        """exercise_launched uses the 'launched' verb IRI."""
        stmt = exercise_launched("a@b.com", "Alice", "e1", "Blue Team Drill")
        assert stmt["verb"]["id"] == VERBS["launched"]
        assert stmt["actor"]["mbox"] == "mailto:a@b.com"
        assert stmt["object"]["objectType"] == "Activity"

    def test_exercise_completed_with_score(self):
        """exercise_completed carries score.raw, score.max, score.scaled."""
        stmt = exercise_completed("a@b.com", "Alice", "e1", "Ex1", score=80, max_score=100)
        result = stmt["result"]
        assert result["score"]["raw"] == 80
        assert result["score"]["max"] == 100
        assert result["score"]["scaled"] == 0.8
        assert result["completion"] is True

    def test_objective_achieved(self):
        """objective_achieved has success=True and a raw score."""
        stmt = objective_achieved("a@b.com", "Alice", "obj-1", "Detect C2", 50)
        assert stmt["result"]["success"] is True
        assert stmt["result"]["score"]["raw"] == 50

    def test_scenario_started(self):
        """scenario_started includes range_id in context extensions."""
        stmt = scenario_started("a@b.com", "Alice", "s1", "APT Hunt", range_id="r1")
        exts = stmt["context"].get("extensions", {})
        range_keys = [k for k in exts if "range_id" in k]
        assert len(range_keys) == 1
        assert exts[range_keys[0]] == "r1"

    def test_verb_iri_mapping(self):
        """All VERBS constants resolve to valid HTTP IRIs."""
        for key, iri in VERBS.items():
            assert iri.startswith("http://"), f"Bad IRI for {key}"

    def test_actor_mbox_format(self):
        """Actor mbox field must start with 'mailto:'."""
        stmt = build_statement("launched", "bob@x.com", "Bob", "exercise", "1", "Ex")
        assert stmt["actor"]["mbox"].startswith("mailto:")

    def test_statement_has_uuid_id(self):
        """Statement id must be a valid UUID4."""
        stmt = build_statement("launched", "c@d.com", "C", "exercise", "1", "Ex")
        uuid.UUID(stmt["id"])  # must not raise
