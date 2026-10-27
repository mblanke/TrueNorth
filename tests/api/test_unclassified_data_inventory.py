from __future__ import annotations

import json
from pathlib import Path

from app.db import Base
from app.result_transfer.dlp import DataFinding, schema_findings
from sqlalchemy import Column, Integer, MetaData, String, Table

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs/security/unclassified-data-baseline.json"


def test_schema_inventory_detects_identity_and_results_without_false_safe_exceptions() -> None:
    metadata = MetaData()
    Table("learner_event", metadata, Column("email", String), Column("score", Integer))
    Table("noise_personas", metadata, Column("display_name", String))
    Table("imported_results", metadata, Column("raw_score", Integer))

    assert schema_findings(metadata) == (
        DataFinding("identity", "learner_event", "email"),
        DataFinding("result", "learner_event", "score"),
    )


def test_unclassified_schema_adds_no_unreviewed_identity_or_result_persistence() -> None:
    accepted = set(json.loads(BASELINE.read_text())["findings"])
    current = {finding.key for finding in schema_findings(Base.metadata)}

    assert current - accepted == set()
    # These are the first producer's concrete leak paths. Keep the scanner honest while
    # allowing each assertion to disappear naturally when its column is removed.
    for key in (
        "identity:users.email",
        "result:quiz_attempts.answers",
        "result:quiz_attempts.score",
        "result:module_progress.score",
        "result:competency_auto_assessments.raw_score",
    ):
        table_column = key.split(":", 1)[1]
        table, column = table_column.split(".", 1)
        if table in Base.metadata.tables and column in Base.metadata.tables[table].columns:
            assert key in current
