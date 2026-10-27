"""Schema-level inventory for data prohibited from an Unclassified deployment.

This is a ratchet, not a claim that the current schema is compliant.  It identifies
columns that can directly persist learner identity or readable assessment results so new
paths cannot appear unnoticed while the accepted baseline is being removed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import MetaData

IDENTITY_COLUMNS = frozenset(
    {
        "ad_distinguished_name",
        "ad_object_guid",
        "callsign",
        "display_name",
        "email",
        "first_name",
        "keycloak_id",
        "last_name",
        "rank",
        "service_branch",
        "service_number",
        "unit",
        "user_display_name",
    }
)
RESULT_COLUMNS = frozenset(
    {
        "answers",
        "competency_mappings",
        "final_grade",
        "final_score",
        "grade",
        "max_score",
        "raw_score",
        "score",
        "total_score",
    }
)

# These are deliberately narrow. Synthetic persona labels are scenario content, a security
# group's directory GUID identifies the group rather than a learner, and imported results are
# the guarded Secret-side authoritative store introduced by ADR 0015.
EXEMPT_COLUMNS = frozenset(
    {
        "noise_personas.display_name",
        "security_groups.ad_object_guid",
    }
)
SECRET_ONLY_TABLES = frozenset({"imported_results"})


@dataclass(frozen=True, order=True)
class DataFinding:
    category: str
    table: str
    column: str

    @property
    def key(self) -> str:
        return f"{self.category}:{self.table}.{self.column}"


def schema_findings(metadata: MetaData) -> tuple[DataFinding, ...]:
    """Return deterministic Unclassified-remediation findings from registered tables."""
    findings: list[DataFinding] = []
    for table in metadata.sorted_tables:
        if table.name in SECRET_ONLY_TABLES:
            continue
        for column in table.columns:
            qualified = f"{table.name}.{column.name}"
            if qualified in EXEMPT_COLUMNS:
                continue
            if column.name in IDENTITY_COLUMNS:
                findings.append(DataFinding("identity", table.name, column.name))
            elif column.name in RESULT_COLUMNS:
                findings.append(DataFinding("result", table.name, column.name))
    return tuple(sorted(findings))
