"""Judging a Student's detection against the objective's answer key (ADR 0005 §1–3).

The scenario query (``objectives[].params.query``) is the ground truth: the events that
make up the attack. A Student's query is credited when, inside the exercise window,

    on_target = |Student ∩ ground truth| >= threshold          (min_hits, at least 1)
    on_target / |Student| >= min_precision                      (default 0.5)

The precision floor is what stops ``*`` passing. The Student's query is parsed by the
closed detection grammar (``search_backends/detection_query.py``): every term names an
observable field, never a ground-truth label, or it is refused before any attempt is used. The window is server time
(``truenorth.ingested_at``, stamped at ingest), so neither the inject's telemetry from
before the exercise nor an earlier exercise on the same range counts, and no sender can
place an event inside it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from .. import safe_yaml
from ..search_backends import BaseSearchBackend
from ..search_backends.detection_query import parse_detection, references_labels
from .names import QUERY, canonical_validator
from .templating import render, unresolved

INGESTED_AT = "truenorth.ingested_at"
DEFAULT_MIN_PRECISION = 0.5
DEFAULT_MAX_ATTEMPTS = 5
EVIDENCE_IDS = 5
MAX_QUERY_LENGTH = 2000


class NotScorable(Exception):  # noqa: N818 — a state, not an error the caller should log
    """The objective cannot be judged by a submitted detection (and never will be)."""


@dataclass(frozen=True)
class AnswerKey:
    query: str
    threshold: int
    min_precision: float
    max_attempts: int


@dataclass(frozen=True)
class Judgement:
    achieved: bool
    events_matched: int
    on_target: int
    precision: float
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


def _int(params: dict, *keys: str, default: int) -> int:
    for key in keys:
        if key in params:
            try:
                return max(int(params[key]), 1)
            except (TypeError, ValueError) as exc:
                raise NotScorable(f"{key} {params[key]!r} is not an integer") from exc
    return default


def answer_key(validator: str, validator_params: str | None, ref_id: str, scenario_yaml: str | None) -> AnswerKey:
    """The objective's rendered ground-truth query and its pass rules.

    Params come from the objective row, falling back to the scenario YAML objective with the
    same id (rows made from QSP paths carry none). Placeholders are filled from the
    scenario's ``variables:``; any left over make the objective not scorable.
    """
    if canonical_validator(validator) != QUERY:
        raise NotScorable("this objective is not a detection query objective")
    scenario = _scenario(scenario_yaml)
    params = _params(validator_params)
    if not params.get("query"):
        for obj in scenario.get("objectives") or []:
            if isinstance(obj, dict) and str(obj.get("id")) == ref_id:
                params = obj.get("params") or {}
                break
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise NotScorable("this objective has no answer key")
    variables = scenario.get("variables") if isinstance(scenario.get("variables"), dict) else {}
    query = render(query, variables)
    if names := unresolved(query):
        raise NotScorable(f"the answer key has undefined variables: {', '.join(names)}")
    if references_labels(query):
        # Inject labels live under tn_ground_truth, stored but not indexed, so a key on them
        # can never match; and Students may not query them. Keys name observable fields.
        raise NotScorable("the answer key queries platform labels, not observable telemetry")
    try:
        min_precision = float(params.get("min_precision", DEFAULT_MIN_PRECISION))
    except (TypeError, ValueError) as exc:
        raise NotScorable(f"min_precision {params.get('min_precision')!r} is not a number") from exc
    return AnswerKey(
        query=query,
        threshold=_int(params, "min_hits", "threshold", default=1),
        min_precision=min(max(min_precision, 0.0), 1.0),
        max_attempts=_int(params, "max_attempts", default=DEFAULT_MAX_ATTEMPTS),
    )


def _window(start: datetime, end: datetime) -> dict:
    return {"range": {INGESTED_AT: {"gte": start.isoformat(), "lte": end.isoformat()}}}


def _lucene(query: str) -> dict:
    return {"query_string": {"query": query}}


def student_query(query: str) -> str:
    """The submitted detection, parsed by the closed detection grammar and re-serialised.

    Raises ``QueryError`` (the API's 422, no attempt used) for anything outside the grammar,
    free text included, and for any ground-truth label field: querying the labels the
    platform stamps on inject telemetry used to match exactly the attack and earn credit.
    """
    return parse_detection(query).lucene()


async def judge(
    backend: BaseSearchBackend, range_id: Any, key: AnswerKey, student: str, start: datetime, end: datetime
) -> Judgement:
    """Count the Student's matches and how many of them are the attack, inside the window.

    ``student`` is the Student's detection as submitted; only its ``student_query`` form
    reaches the store. Raises ``QueryError`` outside the grammar and ``SearchBackendError``
    if the store cannot answer.
    """
    index = range_index(range_id)
    window = _window(start, end)
    mine_q = _lucene(student_query(student))
    mine = await backend.match(index, {"bool": {"filter": [mine_q, window]}})
    hits = await backend.match(
        index, {"bool": {"filter": [mine_q, _lucene(key.query), window]}}, size=EVIDENCE_IDS
    )
    precision = hits.total / mine.total if mine.total else 0.0
    return Judgement(
        achieved=hits.total >= key.threshold and precision >= key.min_precision,
        events_matched=mine.total,
        on_target=hits.total,
        precision=round(precision, 3),
        matched_ids=hits.ids,
    )
