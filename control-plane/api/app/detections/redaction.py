"""What a Student may see of a scenario: the briefing, not the answer key (ADR 0005 §5).

The answer key is everything that says what the attack emits: objective ``params``
(the ground-truth queries), the scenario ``variables:`` they are rendered from, the
timeline's ``action`` / ``params`` (the inject playbook), and anything else an author put
in the document (``pre_staged_environment`` lists the compromised hosts and their
indicators). So the briefing is an allowlist: a key not named below is withheld, and a new
kind of content stays hidden until someone decides it is briefing. Staff who may edit
scenarios (``scenario:update``) see it all.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from ..auth import CurrentUser
from ..rbac import Permission, user_has_permission

BRIEFING_KEYS = (
    "name", "version", "title", "description", "difficulty", "duration_minutes", "duration_min", "phase_count",
    "author", "range_template", "environment", "po_id", "mitre_attack", "mitre_ics", "purdue_model", "scoring",
)
# Timeline entries: what happens when, and what the Student is asked to do and hand in.
TIMELINE_KEYS = ("t", "phase", "name", "title", "description", "narrative", "duration_minutes", "tasks",
                 "expected_deliverables")
OBJECTIVE_KEYS = ("id", "type", "name", "description", "points", "validator", "competency_code")
EVIDENCE_KEY_FIELDS = ("query", "events", "index", "threshold")


def sees_answer_key(user: CurrentUser) -> bool:
    return user_has_permission(user, Permission.SCENARIO_UPDATE)


def redact_timeline(timeline: Any) -> list[dict[str, Any]]:
    return [{k: e[k] for k in TIMELINE_KEYS if k in e} for e in timeline or [] if isinstance(e, dict)]


def redact_scenario(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: doc[k] for k in BRIEFING_KEYS if k in doc}
    out["timeline"] = redact_timeline(doc.get("timeline"))
    out["objectives"] = [
        {k: o[k] for k in OBJECTIVE_KEYS if k in o} for o in doc.get("objectives") or [] if isinstance(o, dict)
    ]
    return out


def redact_scenario_yaml(text: str | None) -> str:
    """The briefing as YAML; a document that does not parse is withheld entirely."""
    try:
        doc = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return ""
    if not isinstance(doc, dict):
        return ""
    return yaml.safe_dump(redact_scenario(doc), sort_keys=False, allow_unicode=True)


def redact_evidence(evidence: str | None) -> str | None:
    """Evidence without the key: older scorer evidence carried the query and matched events."""
    if not evidence:
        return evidence
    try:
        data = json.loads(evidence)
    except ValueError:
        return evidence
    if not isinstance(data, dict):
        return evidence
    return json.dumps({k: v for k, v in data.items() if k not in EVIDENCE_KEY_FIELDS})
