"""``{{ name }}`` placeholders in scenario content, filled from the scenario's ``variables:``.

A mirror of scenario_engine/variables.py (tests/api/test_scenario_variables.py keeps
them in step): grading must not depend on the scenario-engine directory being deployed.

An objective query is the answer key detection credit is judged against (ADR 0005). One
that still says ``{{ c2_domain }}`` can never match an event, so it is rendered here and
any placeholder left over makes the objective unscorable rather than silently failed.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(text: str, variables: dict[str, Any] | None) -> str:
    """``text`` with every placeholder that has a value filled in; unknown ones are kept."""
    values = variables or {}
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def unresolved(text: str) -> list[str]:
    """Names of the placeholders still in ``text``."""
    return PLACEHOLDER.findall(text or "")
