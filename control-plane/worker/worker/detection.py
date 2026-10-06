"""Detection objectives scored against a range's telemetry, for exercises on a real backend.

Mock runs auto-achieve every objective (``tasks.run_scenario_v2``). On any other backend
this module asks scenario_engine's ``ScoringEngine`` whether each unachieved query
objective (validator ``opensearch_query`` / ``validate.opensearch_query``) has matching
events in the range's index, and records the ones that do, with their evidence, then
re-totals the exercise score. The run calls it after each timeline event and once at the
end, so the scoreboard moves while the exercise is live.

The Lucene query comes from the objective row's ``validator_params``, falling back to the
scenario YAML objective with the same id (rows made from QSP paths carry no params). An
objective with no query is skipped, never scored as match-all.

The index is always the range's own (``range_index``, which ``ingest_telemetry_batch``
writes to). An ``index`` in scenario content is ignored: a pattern such as ``truenorth-*``
would score one tenant's exercise against another tenant's telemetry.

scenario_engine is copied into the worker image (see the Dockerfile). It is imported
lazily so a worker built without it still runs mock exercises and says why detections
stay unscored, instead of failing to start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

import yaml

from . import db_ops
from .telemetry import range_index

logger = logging.getLogger("truenorth.worker.detection")

def is_query_validator(name: str | None) -> bool:
    """``opensearch_query`` in any of the spellings content and older rows use."""
    raw = (name or "").strip().lower()
    for prefix in ("validate.", "validate_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.replace(".", "_") == "opensearch_query"
EVIDENCE_EVENTS = 5  # matched events kept on the objective row


def detection_scorer(exercise_id: str, session: Callable, backend: str) -> DetectionScorer | None:
    """A scorer for a real-backend run, or None: scoring is off unless DETECTION_SCORING=on.

    Off by default because it is not yet a measure of the Student: the scenario queries
    describe the attack itself and there is no time window, so the inject's own telemetry
    (or an earlier exercise's on the same range) would achieve the objective for a Student
    who did nothing. Turn it on only once objectives credit what the Student did.
    """
    if backend == "mock" or os.getenv("DETECTION_SCORING", "off").strip().lower() not in ("1", "on", "true", "yes"):
        return None
    return DetectionScorer(exercise_id, session)


def _scenario_params(scenario_yaml: str | None) -> dict[str, dict]:
    """Objective id -> ``params`` from the scenario YAML."""
    try:
        doc = yaml.safe_load(scenario_yaml or "") or {}
    except yaml.YAMLError:
        return {}
    objs = doc.get("objectives") if isinstance(doc, dict) else None
    return {str(o.get("id")): o.get("params") or {} for o in objs or [] if isinstance(o, dict)}


def _row_params(raw: str | None) -> dict:
    try:
        params = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return params if isinstance(params, dict) else {}


def _threshold(params: dict) -> int:
    """``min_hits`` (scenario content) or ``threshold``; at least 1, so zero events never pass."""
    try:
        return max(int(params.get("min_hits", params.get("threshold", 1))), 1)
    except (TypeError, ValueError):
        return 1


def pending_objectives(range_id: str, scenario_yaml: str | None, rows: Sequence) -> list[dict[str, Any]]:
    """ScoringEngine objective dicts for the unachieved query objectives that carry a query.

    ``rows`` are ``db_ops.objectives_to_score`` rows.
    """
    from_yaml = _scenario_params(scenario_yaml)
    out = []
    for ref_id, validator, raw_params, points, achieved in rows:
        if achieved or not is_query_validator(validator):
            continue
        params = _row_params(raw_params)
        if not params.get("query"):
            params = from_yaml.get(ref_id) or {}
        query = params.get("query")
        if not query:
            continue
        out.append(
            {
                "id": ref_id,
                "name": ref_id,
                "max_points": points or 0,
                "validation_method": "opensearch_query",
                "validation_config": {
                    "index": range_index(range_id),
                    "query": query,
                    "threshold": _threshold(params),
                },
                "partial_credit": False,
            }
        )
    return out


def _evidence(config: dict[str, Any], hits: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "source": "event_store",
            "index": config["index"],
            "query": config["query"],
            "threshold": config["threshold"],
            "events": hits[:EVIDENCE_EVENTS],
        },
        default=str,
    )


class DetectionScorer:
    """Scores one exercise's detection objectives; call ``score()`` whenever telemetry may have moved."""

    def __init__(
        self,
        exercise_id: str,
        session_factory: Callable[[], AbstractContextManager],
        event_store: Any | None = None,
    ) -> None:
        self.exercise_id = exercise_id
        self._session = session_factory
        self._store = event_store
        self._engine: Any = None
        try:
            from scenario_engine.scoring import ScoringEngine

            if self._store is None:
                from scenario_engine.event_stores import event_store_from_env

                self._store = event_store_from_env()
            self._engine = ScoringEngine
        except ImportError as exc:
            logger.error("[detection] scenario_engine unavailable (%s); detection objectives stay unscored", exc)

    def score(self) -> int:
        """Record newly detected objectives; return how many of the exercise's objectives are achieved."""
        with self._session() as db:
            ctx = db_ops.exercise_range_and_yaml(db, self.exercise_id)
            rows = db_ops.objectives_to_score(db, self.exercise_id)
        already = sum(1 for r in rows if r[4])
        if ctx is None or self._engine is None:
            return already
        pending = pending_objectives(str(ctx[0]), ctx[1], rows)
        if not pending:
            return already

        result = asyncio.run(self._engine(self.exercise_id, pending, event_store=self._store).evaluate())
        configs = {o["id"]: o["validation_config"] for o in pending}
        detected = [r for r in result.objectives if r.achieved]
        if detected:
            with self._session() as db:
                for r in detected:
                    evidence = _evidence(configs[r.objective_id], r.evidence)
                    db_ops.achieve_objective(db, self.exercise_id, r.objective_id, evidence=evidence)
                db_ops.refresh_exercise_score(db, self.exercise_id)
            logger.info(
                "[detection] exercise %s: %s detected", self.exercise_id, ", ".join(r.objective_id for r in detected)
            )
        return already + len(detected)
