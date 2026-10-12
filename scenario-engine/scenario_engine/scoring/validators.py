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
import re
from datetime import UTC, datetime
from typing import Any

from ..event_stores import BaseEventStore, OpenSearchEventStore
from ..objective_catalogue import ACCOUNT_NAME, MAX_SCAN, REFUSED_VALIDATORS, evidence_class
from ..variables import unresolved

logger = logging.getLogger(__name__)

# Page size when classifying every match as observed or synthetic.
_PAGE = 500


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


INGESTED_AT = "truenorth.ingested_at"  # stamped by the cluster at ingest (telemetry/pipelines/bootstrap.py)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _instant(value: Any) -> datetime | None:
    """A datetime (naive = UTC) from a datetime or an ISO string; None if it is neither."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            # date_nanos carries 9 fractional digits; fromisoformat takes at most 6.
            moment = datetime.fromisoformat(_FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _window(config: dict[str, Any], *, required: bool) -> tuple[datetime | None, datetime | None]:
    """The exercise window the platform put in ``config`` (``window_start`` .. ``window_end``)."""
    start, end = _instant(config.get("window_start")), _instant(config.get("window_end"))
    if config.get("window_start") is not None and start is None:
        raise UnscoredError(f"window_start {config.get('window_start')!r} is not a time")
    if config.get("window_end") is not None and end is None:
        raise UnscoredError(f"window_end {config.get('window_end')!r} is not a time")
    if required and start is None:
        raise UnscoredError("no exercise window: the objective is judged only inside a running exercise")
    return start, end


def _ingested(source: dict[str, Any]) -> datetime | None:
    nested = source.get("truenorth")
    raw = source.get(INGESTED_AT) or (nested.get("ingested_at") if isinstance(nested, dict) else None)
    return _instant(raw)


def _in_window(source: Any, start: datetime | None, end: datetime | None) -> bool:
    """Ingested inside the window. With a window, an event with no ingest time is outside it."""
    if start is None and end is None:
        return True
    at = _ingested(source if isinstance(source, dict) else {})
    return at is not None and (start is None or at >= start) and (end is None or at <= end)


DISABLED, ENABLED = "4725", "4722"  # Windows Security: user account disabled / enabled
_EVENT_ID_FIELDS = ("EventID", "winlog.event_id", "event.code")
_TARGET_USER_FIELDS = ("TargetUserName", "winlog.event_data.TargetUserName", "user.target.name")


def _field(source: dict[str, Any], dotted: str) -> Any:
    """``a.b.c`` as a flat key, else as a nested path."""
    if dotted in source:
        return source[dotted]
    cur: Any = source
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _event_id(source: dict[str, Any]) -> str | None:
    for name in _EVENT_ID_FIELDS:
        if (value := _field(source, name)) is not None:
            return str(value)
    return None


def _target_user(source: dict[str, Any]) -> str | None:
    for name in _TARGET_USER_FIELDS:
        if isinstance(value := _field(source, name), str) and value:
            return value.casefold()
    return None


def _event_order(source: dict[str, Any]) -> tuple[datetime, datetime]:
    """When it happened on the host (``@timestamp``), then when it was ingested."""
    floor = datetime.min.replace(tzinfo=UTC)
    return (_instant(source.get("@timestamp")) or floor, _ingested(source) or floor)


async def _observed_hits(store: BaseEventStore, index: str, queries: list[str | dict[str, Any]]) -> list[dict[str, Any]]:
    """Every hit for any of ``queries`` that a range host or sensor produced (not synthetic).

    The synthetic label is not indexed (objective_catalogue), so each match is classified
    here: all of them, page by page, never a first page only. Raises ``UnscoredError`` when
    the store fails or a query matches more than ``MAX_SCAN`` events.
    """
    observed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        offset = 0
        while True:
            try:
                found = await store.search(index, query, size=_PAGE, offset=offset)
            except Exception as exc:
                logger.exception("Event store query validation failed")
                raise UnscoredError(f"event store unavailable: {exc}") from exc
            if found.total > MAX_SCAN:
                raise UnscoredError(f"{found.total} matching events, more than the {MAX_SCAN} classified")
            for hit in found.hits:
                source = hit.get("_source") if isinstance(hit.get("_source"), dict) else {}
                # By document id: the same event can match more than one query, and two
                # events with the same content are still two events.
                key = str(hit["_id"]) if hit.get("_id") is not None else json.dumps(source, sort_keys=True, default=str)
                if key not in seen and evidence_class(source) == "observed":
                    seen.add(key)
                    observed.append(hit)
            offset += len(found.hits)
            if not found.hits or offset >= found.total:
                break
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
        window: tuple[datetime | None, datetime | None] | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Route to the correct validator based on *method*.

        Query objectives run against ``event_store``; ``opensearch_url`` is the older way
        to say the same thing and builds an OpenSearch store. ``exercise_id`` (the scoring
        engine's) keys ``greyspace_breadcrumb`` tokens when the config names none.
        ``window`` is the exercise's (started_at, completed_at), from the platform; it
        replaces any ``window_start`` / ``window_end`` in the config.
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
            config = {**config, "window_start": None, "window_end": None}
            if window is not None:
                config["window_start"], config["window_end"] = window
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
        """Enough observed events match the query (``require_observed: true`` only).

        The method keeps its historical name because scenario content and objective rows
        say ``opensearch_query``; any ``BaseEventStore`` can serve it.

        Without ``require_observed: true`` this is a detection objective, credited only from
        a Student's submitted detection (ADR 0005, app/detections/credit.py): unscored here.
        Scoring it from the store credited the inject's own synthetic records, full points
        with no Student action.

        ``config`` keys:
            - ``index``: index pattern (e.g. ``truenorth-*``)
            - ``query``: Lucene query string, or query DSL (dict). Required: an objective
              without one is not scored, never scored as match-all.
            - ``threshold`` (or ``min_hits``): minimum number of matching docs, at least 1
            - ``require_observed``: must be true for the engine to judge it; counts only
              events a range host or sensor produced (objective_catalogue)
            - ``window_start`` (required) / ``window_end``: set by the platform; only events
              ingested inside the exercise window count, so a reused range's earlier real
              events cannot satisfy this run

        Achieved with ``threshold`` observed matches. Fewer is unscored, not failed: the
        evidence may not have arrived. Raises ``UnscoredError`` for that and whenever the
        objective cannot be judged (no query, a bad threshold, no exercise window, no store,
        a store failure).
        """
        require_observed = config.get("require_observed", False)
        if not isinstance(require_observed, bool):
            raise UnscoredError(f"require_observed {require_observed!r} is not true or false")
        if not require_observed:
            raise UnscoredError("detection objective: credited from a Student's submitted detection, not by the engine")
        query = config.get("query")
        if not query:
            raise UnscoredError("objective has no query")
        if isinstance(query, str) and (names := unresolved(query)):
            raise UnscoredError(f"query has unrendered placeholders: {', '.join(names)}")
        threshold = _threshold(config)
        if event_store is None:
            raise UnscoredError("no event store configured")

        index = config.get("index", "truenorth-*")
        start, end = _window(config, required=True)
        observed = [
            h for h in await _observed_hits(event_store, index, [query]) if _in_window(h.get("_source"), start, end)
        ]
        if len(observed) < threshold:
            # Synthetic matches are not evidence, so neither is their count: no partial credit.
            raise UnscoredError(f"{len(observed)} observed matching events of {threshold} needed")
        return True, observed[:20]

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
        """Did the Student submit the deliverable (report, IOC list, etc.)?

        Judged only from a Student submission record the platform passes in as
        ``submission``; scenario content cannot name the file (objective_catalogue
        PLATFORM_PARAMS, and the runner drops it). Before 2026-10-12 a content
        ``storage_path`` was trusted, so ``/etc/hosts`` passed. The platform has no
        deliverable submission yet: without one the objective is unscored, and an
        instructor acknowledges it.

        ``config`` keys:
            - ``submission``: ``{"path": ..., "submitted_by": ...}`` from the platform
            - ``deliverable_type``: e.g. ``incident_report``, ``ioc_list``
            - ``min_size_bytes``: minimum file size to count as valid
            - ``required_fields``: fields that must be present (for JSON)
        """
        import os

        submission = config.get("submission")
        if not isinstance(submission, dict) or not submission.get("path") or not submission.get("submitted_by"):
            raise UnscoredError("no Student submission record for this deliverable")
        storage_path = str(submission["path"])
        min_size = config.get("min_size_bytes", 100)
        deliverable_type = config.get("deliverable_type", "unknown")

        if not os.path.isfile(storage_path):
            # The record exists but its file does not: the platform's fault, not the Student's.
            raise UnscoredError("the submitted deliverable's file is missing")

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
                "submitted_by": submission["submitted_by"],
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
        """Is the account disabled? Observed Windows Security 4725 for it, not re-enabled.

        Range domain controllers audit User Account Management (infra/ansible/roles/
        domain_controller), so a disable writes 4725 and a re-enable 4722. Both are looked
        for under each field convention the range's telemetry uses (flat ``EventID`` /
        ``TargetUserName`` as the scenario content queries, Winlogbeat, ECS); the account
        is matched case-insensitively. Only observed events ingested inside the exercise
        window count: a simulated inject's synthetic records, or an earlier exercise's
        events on a reused range, never prove this exercise disabled the account.

        Achieved when the account's latest such event is a 4725. Not achieved when it is a
        4722 (re-enabled). With neither, unscored rather than failed: a missing event may
        be a telemetry gap (Winlogbeat not shipping, the event not ingested yet), and a real
        disable must never score as a fail.

        ``config`` keys:
            - ``username``: the account name (required; a plain name, no query syntax)
            - ``index``: index pattern (default ``truenorth-*``)
            - ``window_start`` (required) / ``window_end``: set by the platform

        Raises ``UnscoredError`` with no usable username, no exercise window, no store, a
        store failure, or no 4725/4722 for the account.
        """
        username = str(config.get("username") or "")
        if unresolved(username):
            raise UnscoredError(f"username has unrendered placeholders: {', '.join(unresolved(username))}")
        if not ACCOUNT_NAME.match(username):
            raise UnscoredError("user_disabled objective needs a plain account name in username")
        start, end = _window(config, required=True)
        if event_store is None:
            raise UnscoredError("no event store configured")
        queries: list[str | dict[str, Any]] = [
            f"{field}:{code}" for code in (DISABLED, ENABLED) for field in _EVENT_ID_FIELDS
        ]
        index = config.get("index", "truenorth-*")
        wanted = username.casefold()
        events = [
            h
            for h in await _observed_hits(event_store, index, queries)
            if _in_window(h.get("_source"), start, end) and _target_user(h["_source"]) == wanted
        ]
        if not events:
            raise UnscoredError(f"no observed 4725 or 4722 for {username} in the exercise window")
        events.sort(key=lambda h: _event_order(h["_source"]))
        last = events[-1]
        latest = {_event_id(h["_source"]) for h in events if _event_order(h["_source"]) == _event_order(last["_source"])}
        if latest == {DISABLED, ENABLED}:
            raise UnscoredError(f"a disable and a re-enable of {username} at the same instant")
        if _event_id(last["_source"]) != DISABLED:
            return False, []  # re-enabled after any disable: not disabled now
        return True, [last]

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
