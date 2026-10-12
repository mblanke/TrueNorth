"""TrueNorth Range — Scoring Validators.

Each validator checks whether a specific type of objective has been
completed, returning ``(achieved, evidence)`` where *evidence* is a
list of supporting artefact dicts.

An objective that cannot be judged raises ``UnscoredError``: an unknown or refused
validator (objective_catalogue.REFUSED_VALIDATORS), missing config, an event store that
is down, or a manual objective no instructor has acknowledged yet. Unscored is never
"achieved" and never "not achieved". On a miss, evidence is empty (or carries ``error``),
so a miss earns no partial credit for evidence that does not exist.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..event_stores import BaseEventStore, OpenSearchEventStore
from ..objective_catalogue import ACCOUNT_NAME, REFUSED_VALIDATORS, evidence_class
from ..variables import unresolved

logger = logging.getLogger(__name__)

# Most hits fetched to tell observed from synthetic evidence.
_OBSERVED_SCAN = 100


class UnscoredError(Exception):
    """The objective could not be judged this time (no query, no store, store outage).

    Distinct from "not achieved": the caller leaves the objective pending and retries.
    """


def _threshold(config: dict[str, Any]) -> int:
    """``threshold`` or ``min_hits``, at least 1, so zero events never pass."""
    raw = config.get("threshold", config.get("min_hits", 1))
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError) as exc:
        raise UnscoredError(f"threshold {raw!r} is not an integer") from exc


async def _observed_hits(
    store: BaseEventStore, index: str, queries: list[str | dict[str, Any]], threshold: int
) -> list[dict[str, Any]]:
    """Hits for any of ``queries`` that a range host or sensor produced (not synthetic).

    Raises ``UnscoredError`` when the store fails, or when a query has more hits than were
    scanned and too few of those scanned were observed to decide.
    """
    observed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        try:
            found = await store.search(index, query, size=_OBSERVED_SCAN)
        except Exception as exc:
            logger.exception("Event store query validation failed")
            raise UnscoredError(f"event store unavailable: {exc}") from exc
        for hit in found.hits:
            source = hit.get("_source") if isinstance(hit.get("_source"), dict) else {}
            # By content: the same event can match more than one query.
            key = json.dumps(source, sort_keys=True, default=str) if source else f"id:{hit.get('_id')}"
            if key not in seen and evidence_class(source) == "observed":
                seen.add(key)
                observed.append(hit)
        if found.total > len(found.hits) and len(observed) < threshold:
            raise UnscoredError(
                f"{found.total} matching events, more than the {len(found.hits)} scanned for observed evidence"
            )
    return observed


class ScoringValidator:
    """Validates objective completion against collected evidence."""

    # ── dispatcher ──────────────────────────────────────────

    @staticmethod
    async def validate(
        method: str,
        config: dict[str, Any],
        opensearch_url: str | None = None,
        event_store: BaseEventStore | None = None,
        exercise_id: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Route to the correct validator based on *method*.

        Query objectives run against ``event_store``; ``opensearch_url`` is the older way
        to say the same thing and builds an OpenSearch store. ``exercise_id`` (the scoring
        engine's) keys ``greyspace_breadcrumb`` tokens when the config names none.
        """
        dispatch = {
            "opensearch_query": ScoringValidator.validate_opensearch_query,
            "deliverable": ScoringValidator.validate_deliverable,
            "user_disabled": ScoringValidator.validate_user_disabled,
            "manual": ScoringValidator.validate_manual,
            "greyspace_breadcrumb": ScoringValidator.validate_greyspace_breadcrumb,
        }
        if method in REFUSED_VALIDATORS:
            raise UnscoredError(f"validator '{method}' is refused: {REFUSED_VALIDATORS[method]}")
        handler = dispatch.get(method)
        if handler is None:
            logger.warning("Unknown validation method: %s", method)
            raise UnscoredError(f"unknown validator '{method}'")
        if method in ("opensearch_query", "user_disabled"):
            if event_store is None and opensearch_url:
                event_store = OpenSearchEventStore(opensearch_url)
            return await handler(config, event_store)
        if method == "greyspace_breadcrumb":
            return await handler(config, exercise_id)
        return await handler(config)

    # ── individual validators ───────────────────────────────

    @staticmethod
    async def validate_opensearch_query(
        config: dict[str, Any],
        event_store: BaseEventStore | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check that expected events exist in the event store.

        The method keeps its historical name because scenario content and objective rows
        say ``opensearch_query``; any ``BaseEventStore`` can serve it.

        ``config`` keys:
            - ``index``: index pattern (e.g. ``truenorth-*``)
            - ``query``: Lucene query string, or query DSL (dict). Required: an objective
              without one is not scored, never scored as match-all.
            - ``threshold`` (or ``min_hits``): minimum number of matching docs, at least 1
            - ``require_observed``: count only events a range host or sensor produced, not
              a simulated inject's synthetic records (objective_catalogue). Default false:
              a detection objective scores finding the inject's records.

        Raises ``UnscoredError`` when the objective cannot be judged (no query, a bad threshold,
        no store, or the store failed), so an outage is never reported as "not achieved".
        """
        query = config.get("query")
        if not query:
            raise UnscoredError("objective has no query")
        if isinstance(query, str) and (names := unresolved(query)):
            raise UnscoredError(f"query has unrendered placeholders: {', '.join(names)}")
        threshold = _threshold(config)
        require_observed = config.get("require_observed", False)
        if not isinstance(require_observed, bool):
            raise UnscoredError(f"require_observed {require_observed!r} is not true or false")
        if event_store is None:
            raise UnscoredError("no event store configured")

        index = config.get("index", "truenorth-*")
        if require_observed:
            observed = await _observed_hits(event_store, index, [query], threshold)
            # Synthetic matches are not evidence here, not even for partial credit.
            return len(observed) >= threshold, observed[:20]
        try:
            found = await event_store.search(index, query, size=min(threshold, 100))
        except Exception as exc:
            logger.exception("Event store query validation failed")
            raise UnscoredError(f"event store unavailable: {exc}") from exc
        return found.total >= threshold, found.hits[:20]

    @staticmethod
    async def validate_greyspace_breadcrumb(
        config: dict[str, Any],
        exercise_id: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Did the team find this exercise's Greyspace breadcrumb (ADR 0007)?

        ``config`` keys:
            - ``crumb_id``: the breadcrumb's id in the greyspace_breadcrumb inject (required)
            - ``salt``: as the inject's (optional)
            - ``exercise_id``: else the scoring engine's (the inject's exercise)
            - ``answer``: what the team submitted (a flag field, a report excerpt)

        Achieved when the answer carries the exercise's token. No answer is not achieved.
        Raises ``UnscoredError`` for an objective that cannot be judged: no crumb id, no
        exercise, or no breadcrumb key (GREYSPACE_CRUMB_KEY / TN_SECRETS_KEY). The token
        is never put in the evidence.
        """
        from ..injectors.greyspace_breadcrumb import BreadcrumbKeyError, crumb_found, crumb_key

        crumb = str(config.get("crumb_id") or "")
        exercise = str(config.get("exercise_id") or exercise_id or "")
        if not crumb:
            raise UnscoredError("greyspace_breadcrumb objective has no crumb_id")
        if not exercise:
            raise UnscoredError("greyspace_breadcrumb objective has no exercise")
        answer = str(config.get("answer") or "")
        try:
            crumb_key()  # no key: unscored, even before anything is submitted
            found = crumb_found(exercise, crumb, answer, config.get("salt"))
        except BreadcrumbKeyError as exc:
            raise UnscoredError(str(exc)) from exc
        if not found:
            # No evidence for a miss: evidence on a failed objective earns partial credit.
            return False, []
        return True, [{"check": "greyspace_breadcrumb", "crumb_id": crumb, "matched": True}]

    @staticmethod
    async def validate_deliverable(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if a deliverable (report, IOC list, etc.) was submitted.

        ``config`` keys:
            - ``deliverable_type``: e.g. ``incident_report``, ``ioc_list``
            - ``storage_path``: path or URL where deliverable should be
            - ``min_size_bytes``: minimum file size to count as valid
            - ``required_fields``: fields that must be present (for JSON)
        """
        import os

        storage_path = config.get("storage_path", "")
        min_size = config.get("min_size_bytes", 100)
        deliverable_type = config.get("deliverable_type", "unknown")

        if not storage_path:
            # Content names a deliverable store key; without a resolved path nothing can be checked.
            raise UnscoredError("deliverable objective has no storage_path to check")

        if not os.path.exists(storage_path):
            return False, [{"error": f"Deliverable not found at {storage_path}"}]

        file_size = os.path.getsize(storage_path)
        if file_size < min_size:
            return False, [
                {
                    "error": f"Deliverable too small ({file_size} < {min_size} bytes)",
                    "path": storage_path,
                }
            ]

        evidence = [
            {
                "deliverable_type": deliverable_type,
                "path": storage_path,
                "size_bytes": file_size,
            }
        ]

        # Optional JSON field validation
        required_fields = config.get("required_fields", [])
        if required_fields and storage_path.endswith(".json"):
            import json

            try:
                with open(storage_path, encoding="utf-8") as f:
                    data = json.load(f)
                missing = [fld for fld in required_fields if fld not in data]
                if missing:
                    return False, [{"error": f"Missing fields: {missing}"}]
            except (json.JSONDecodeError, OSError) as exc:
                return False, [{"error": str(exc)}]

        return True, evidence

    # firewall_rule, process_killed and network_isolated were stubs here until 2026-10-12
    # (firewall_rule passed every objective). They are refused: objective_catalogue says why.

    @staticmethod
    async def validate_user_disabled(
        config: dict[str, Any],
        event_store: BaseEventStore | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Was the account disabled? Windows Security event 4725 for it, observed.

        Range domain controllers audit User Account Management (infra/ansible/roles/
        domain_controller), so a disable writes 4725. The event is looked for under each
        field convention the range's telemetry uses: flat (``EventID``/``TargetUserName``,
        as the scenario content queries) and Winlogbeat ECS. Only observed events count: a
        simulated inject's synthetic records never prove an account was disabled.

        ``config`` keys:
            - ``username``: the account name (required; a plain name, no query syntax)
            - ``index``: index pattern (default ``truenorth-*``)

        Raises ``UnscoredError`` with no usable username, no store, or a store failure.
        """
        username = str(config.get("username") or "")
        if unresolved(username):
            raise UnscoredError(f"username has unrendered placeholders: {', '.join(unresolved(username))}")
        if not ACCOUNT_NAME.match(username):
            raise UnscoredError("user_disabled objective needs a plain account name in username")
        if event_store is None:
            raise UnscoredError("no event store configured")
        queries: list[str | dict[str, Any]] = [
            f'EventID:4725 AND TargetUserName:"{username}"',
            f'winlog.event_id:4725 AND winlog.event_data.TargetUserName:"{username}"',
            f'event.code:4725 AND user.target.name:"{username}"',
        ]
        index = config.get("index", "truenorth-*")
        observed = await _observed_hits(event_store, index, queries, 1)
        return bool(observed), observed[:20]

    @staticmethod
    async def validate_manual(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """An instructor's explicit acknowledgement; until then, unscored.

        Achieved only when the caller passes the instructor's acknowledgement as
        ``approved: True`` (the API records it through ``POST /exercises/{id}/objectives/
        {ref}/ack``). Scenario content cannot set it: the runner drops it and scenario
        validation refuses it. Anything else raises ``UnscoredError``: awaiting review is
        neither a pass nor a fail.

        ``config`` keys:
            - ``approved``: True once an instructor acknowledged the objective
            - ``notes``: instructor feedback
        """
        if config.get("approved") is not True:
            raise UnscoredError("awaiting instructor acknowledgement")
        notes = config.get("notes", "")
        return True, [{"manual_review": True, "approved": True, "notes": notes}]
