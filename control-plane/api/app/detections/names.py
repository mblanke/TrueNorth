"""One spelling per validator.

Content and objective rows have said ``opensearch_query``, ``validate.opensearch_query``,
``validate.opensearch.query`` and ``validate_opensearch_query`` for the same thing, and a
scorer that knew one spelling silently skipped the others. Rows are written in the
canonical form below; the scenario schema enumerates it.

``canonical_validator`` mirrors ``scenario_engine.objective_catalogue.canonical`` (the
engine is not importable at API startup; engine_bridge loads it lazily). A drift test
(tests/scenario_engine/test_scenario_objective_validation.py) holds the two, the schema
enum, the catalogue and the Scenario Studio's list to one answer, empty name included.
"""

from __future__ import annotations

QUERY = "opensearch_query"
MANUAL = "manual_ack"
DELIVERABLE = "deliverable_check"
USER_DISABLED = "user_disabled"  # observed 4725 only (scenario_engine/objective_catalogue.py)
GREYSPACE_BREADCRUMB = "greyspace_breadcrumb"  # ADR 0007
CANONICAL = (QUERY, MANUAL, DELIVERABLE, USER_DISABLED, GREYSPACE_BREADCRUMB)


def canonical_validator(name: str | None, default: str = MANUAL) -> str:
    """``validate.opensearch.query`` -> ``opensearch_query``; an empty name is ``default``.

    A name that is not canonical is returned normalised but otherwise unchanged,
    so a custom validator is still visible (and unscored) rather than silently renamed.
    """
    raw = (name or "").strip().lower()
    if not raw:
        return default
    for prefix in ("validate.", "validate_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.replace(".", "_")
