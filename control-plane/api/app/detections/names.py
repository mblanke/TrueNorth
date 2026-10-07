"""One spelling per validator.

Content and objective rows have said ``opensearch_query``, ``validate.opensearch_query``,
``validate.opensearch.query`` and ``validate_opensearch_query`` for the same thing, and a
scorer that knew one spelling silently skipped the others. Rows are written in the
canonical form below; the scenario schema enumerates it.
"""

from __future__ import annotations

QUERY = "opensearch_query"
MANUAL = "manual_ack"
DELIVERABLE = "deliverable_check"
CANONICAL = (QUERY, MANUAL, DELIVERABLE)


def canonical_validator(name: str | None, default: str = MANUAL) -> str:
    """``validate.opensearch.query`` -> ``opensearch_query``; an empty name is ``default``.

    A name that is not one of the three is returned normalised but otherwise unchanged,
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
