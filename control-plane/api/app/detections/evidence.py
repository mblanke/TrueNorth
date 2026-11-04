"""A Student's detection as evidence for the instructor who judges the objective (ADR 0020).

Team exercises have no answer key. A submitted detection is run against the range's events
inside the exercise window and recorded with what it matched; nothing compares it with
anything, and nothing is credited by it. The instructor reads the submissions and decides.

What keeps it trustworthy evidence is ADR 0005's machinery, unchanged:

- the Student's query is parsed by the closed detection grammar
  (``search_backends/detection_query.py``): every term names an observable field, never a
  ground-truth label, or it is refused;
- the window is server time (``truenorth.ingested_at``, stamped by OpenSearch at ingest),
  so telemetry from before the exercise, or from an earlier one on the same range, is
  outside it, and no sender can place an event inside it.

The scenario objective's ``query`` is the instructor's **look-for** guidance (``look_for``):
staff-only, rendered from the scenario's ``variables:``, never used to judge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from .. import safe_yaml
from ..search_backends import BaseSearchBackend
from ..search_backends.detection_query import parse_detection
from .names import QUERY, canonical_validator
from .templating import render

INGESTED_AT = "truenorth.ingested_at"
EVIDENCE_IDS = 5
MAX_QUERY_LENGTH = 2000


@dataclass(frozen=True)
class Matches:
    events_matched: int
    matched_ids: list[str]


def range_index(range_id: Any) -> str:
    """The range's events: ``range-<id>`` (API and worker ingest) and ``range-<id>-<date>``
    (Filebeat and Logstash in the range)."""
    return f"range-{range_id},range-{range_id}-*"


def _params(raw: str | None) -> dict[str, Any]:
    try:
        params = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return params if isinstance(params, dict) else {}


def _scenario(scenario_yaml: str | None) -> dict[str, Any]:
    try:
        doc = safe_yaml.load(scenario_yaml or "") or {}
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def is_detection_objective(validator: str | None) -> bool:
    """An objective the team works by finding something in the range's telemetry."""
    return canonical_validator(validator) == QUERY


def look_for(validator: str, validator_params: str | None, ref_id: str, scenario_yaml: str | None) -> str | None:
    """The instructor's look-for query for a detection objective, rendered; None if it has none.

    Params come from the objective row, falling back to the scenario YAML objective with the
    same id (rows made from QSP paths carry none). Guidance only: never shown to Students
    (redaction.py) and never compared with a submission.
    """
    if not is_detection_objective(validator):
        return None
    scenario = _scenario(scenario_yaml)
    params = _params(validator_params)
    if not params.get("query"):
        for obj in scenario.get("objectives") or []:
            if isinstance(obj, dict) and str(obj.get("id")) == ref_id:
                params = obj.get("params") or {}
                break
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    variables = scenario.get("variables") if isinstance(scenario.get("variables"), dict) else {}
    return render(query, variables)


def window(start: datetime, end: datetime) -> dict:
    return {"range": {INGESTED_AT: {"gte": start.isoformat(), "lte": end.isoformat()}}}


def student_query(query: str) -> str:
    """The submitted detection, parsed by the closed detection grammar and re-serialised.

    Raises ``QueryError`` (the API's 422) for anything outside the grammar, free text
    included, and for any ground-truth label field.
    """
    return parse_detection(query).lucene()


async def matches(
    backend: BaseSearchBackend, range_id: Any, query: str, start: datetime, end: datetime, *, trusted: bool = False
) -> Matches:
    """What ``query`` matches in the range's events inside the window, with up to
    ``EVIDENCE_IDS`` event ids.

    A Student's query goes through ``student_query`` first; ``trusted`` (staff look-for
    queries) skips it. Raises ``QueryError`` outside the grammar and ``SearchBackendError``
    if the store cannot answer.
    """
    lucene = query if trusted else student_query(query)
    found = await backend.match(
        range_index(range_id),
        {"bool": {"filter": [{"query_string": {"query": lucene}}, window(start, end)]}},
        size=EVIDENCE_IDS,
    )
    return Matches(events_matched=found.total, matched_ids=found.ids)
