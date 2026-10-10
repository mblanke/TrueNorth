"""TrueNorth Range — xAPI 1.0.3 statement builder.

Every statement TrueNorth emits server-side is built here. The rules, and why
(docs/xapi-conformance.md has the long form):

* **Actor** is an Agent with an ``account`` IFI: ``homePage`` is TrueNorth's identity
  authority (``XAPI_ACCOUNT_HOMEPAGE``, default the platform's public URL) and ``name``
  is the user's ``users.id``, a random UUID. No email, no display name: an email is
  personal information, changes, and is forbidden as a cmi5 actor; hashing it is not
  anonymisation. Reports join names from TrueNorth, not from the LRS.
* **IRIs** (activities, activity types, extensions, TrueNorth-only verbs) live under
  ``XAPI_IRI_BASE`` (default ``<public URL>/xapi``), a domain TrueNorth controls. ADL
  verbs keep their ADL IRIs. Activity IDs identify things, never attempts.
* **context.registration** is set on every learning statement: the enrolment for work
  inside a course, the quiz attempt for a stand-alone quiz, the exercise run for range
  work (one registration per run, shared by the run's participants).
* **Language**: ``context.language`` and the activity language maps use the course's
  locale when there is one, else ``XAPI_DEFAULT_LANGUAGE`` (default ``en``). Verb
  displays are English words and are tagged ``en``.
* **Results** obey xAPI 1.0.3 (``min <= raw <= max``, ``-1 <= scaled <= 1``, ISO 8601
  durations); success is judged against the activity's own pass mark (the quiz's
  ``pass_pct``, the module's ``pass_threshold``), and only falls back to
  ``XAPI_DEFAULT_PASS_THRESHOLD`` (0.7) where the activity has none.

The LRS is reached only through ``app.lms`` (the adapter registry); this module never
reads the LRS URL.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from app.lms import get_lms_backend

# A conservative RFC 5646 shape: a 2-3 letter language, then subtags. Anything else falls
# back to the default rather than producing a language map the LRS rejects.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


# -- Configuration (read at call time, so a test or a restart picks up a change) --------
def platform_url() -> str:
    """The platform's public URL: https://$DOMAIN in production, else the web base URL."""
    domain = os.getenv("DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return (os.getenv("LTI_WEB_BASE_URL", "").strip() or "http://localhost:4200").rstrip("/")


def account_homepage() -> str:
    """``actor.account.homePage``: the authority that issues the account names."""
    return (os.getenv("XAPI_ACCOUNT_HOMEPAGE", "").strip() or platform_url()).rstrip("/")


def iri_base() -> str:
    """Root of every TrueNorth-minted IRI. Change it once, before data exists: IRIs are
    compared as strings, so moving it splits the record."""
    return (os.getenv("XAPI_IRI_BASE", "").strip() or f"{platform_url()}/xapi").rstrip("/")


def default_language() -> str:
    lang = os.getenv("XAPI_DEFAULT_LANGUAGE", "").strip()
    return lang if _LANGUAGE_TAG.match(lang) else "en"


def default_pass_threshold() -> float:
    """Pass mark (0..1) for activities that carry none of their own."""
    try:
        value = float(os.getenv("XAPI_DEFAULT_PASS_THRESHOLD", "0.7"))
    except ValueError:
        return 0.7
    return value if 0.0 <= value <= 1.0 else 0.7


def language_tag(value: object) -> str:
    """A usable RFC 5646 tag from a course locale (``en_CA`` becomes ``en-CA``), or the default."""
    if isinstance(value, str):
        tag = value.strip().replace("_", "-")
        if _LANGUAGE_TAG.match(tag):
            return tag
    return default_language()


def iso_duration(span: timedelta | float | None) -> str | None:
    """ISO 8601 duration with centisecond precision (xAPI compares at 0.01 s)."""
    if span is None:
        return None
    seconds = span.total_seconds() if isinstance(span, timedelta) else float(span)
    centis = max(0, round(seconds * 100))
    return f"PT{centis // 100}.{centis % 100:02d}S"


# ADL verb IRIs (the cmi5-defined ones among them).
VERBS = {
    "launched": "http://adlnet.gov/expapi/verbs/launched",
    "initialized": "http://adlnet.gov/expapi/verbs/initialized",
    "completed": "http://adlnet.gov/expapi/verbs/completed",
    "passed": "http://adlnet.gov/expapi/verbs/passed",
    "failed": "http://adlnet.gov/expapi/verbs/failed",
    "terminated": "http://adlnet.gov/expapi/verbs/terminated",
    "scored": "http://adlnet.gov/expapi/verbs/scored",
    "experienced": "http://adlnet.gov/expapi/verbs/experienced",
    "attempted": "http://adlnet.gov/expapi/verbs/attempted",
    "answered": "http://adlnet.gov/expapi/verbs/answered",
}


def actor(user_id: uuid.UUID | str) -> dict:
    """The Agent for a TrueNorth user: ``account`` IFI, opaque stable name, no PII."""
    name = str(user_id).strip()
    if not name:
        raise ValueError("an xAPI actor needs a user id")
    return {"objectType": "Agent", "account": {"homePage": account_homepage(), "name": name}}


def verb(verb_key: str) -> dict:
    """ADL verb when there is one, else a TrueNorth verb under the IRI base."""
    return {
        "id": VERBS.get(verb_key, f"{iri_base()}/verbs/{quote(verb_key, safe='')}"),
        "display": {"en": verb_key},
    }


def activity_iri(activity_type: str, activity_id: str = "") -> str:
    """``<base>/activities/<type>[/<id>]``: identifies the thing, never an attempt."""
    base = f"{iri_base()}/activities/{quote(activity_type, safe='')}"
    return f"{base}/{quote(str(activity_id), safe='')}" if str(activity_id) else base


def extension_iri(key: str) -> str:
    """A key that is already an absolute IRI is kept; anything else goes under the base."""
    if urlsplit(key).scheme in ("http", "https"):
        return key
    return f"{iri_base()}/extensions/{quote(key, safe='')}"


def activity(
    activity_type: str, activity_id: str, name: str, description: str = "", language: str | None = None
) -> dict:
    """xAPI Activity (Object) with language maps in the statement's language."""
    lang = language_tag(language)
    return {
        "objectType": "Activity",
        "id": activity_iri(activity_type, activity_id),
        "definition": {
            "type": f"{iri_base()}/activity-types/{quote(activity_type, safe='')}",
            "name": {lang: name or activity_type},
            "description": {lang: description or name or activity_type},
        },
    }


def _registration(value: uuid.UUID | str | None) -> str | None:
    """A registration is a UUID or nothing; a malformed one is dropped, not sent (the LRS
    would refuse the whole statement)."""
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def build_statement(
    verb_key: str,
    user_id: uuid.UUID | str,
    activity_type: str,
    activity_id: str,
    activity_name: str,
    result: dict | None = None,
    context_extensions: dict | None = None,
    *,
    registration: uuid.UUID | str | None = None,
    language: str | None = None,
) -> dict:
    """A complete xAPI 1.0.3 statement about ``user_id`` (see the module docstring)."""
    lang = language_tag(language)
    stmt: dict = {
        "id": str(uuid.uuid4()),
        "actor": actor(user_id),
        "verb": verb(verb_key),
        "object": activity(activity_type, activity_id, activity_name, language=lang),
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
    if result:
        stmt["result"] = result

    context: dict = {"platform": "TrueNorth Range", "language": lang}
    reg = _registration(registration)
    if reg:
        context["registration"] = reg
    if context_extensions:
        context["extensions"] = {extension_iri(k): v for k, v in context_extensions.items()}
    stmt["context"] = context
    return stmt


async def send_statement(statement: dict) -> bool:
    """Send an xAPI statement to the LRS."""
    return await get_lms_backend().emit_statement(statement)


async def send_statements(statements: list[dict]) -> int:
    """Send multiple xAPI statements. Returns count of successful sends."""
    return await get_lms_backend().emit_statements(statements)


# ── Convenience builders ───────────────────────────────────────────────


def exercise_launched(user_id: uuid.UUID | str, exercise_id: str, exercise_name: str, **kwargs) -> dict:
    """A Student started range work. The exercise run is the registration."""
    return build_statement(
        "launched", user_id, "exercise", exercise_id, exercise_name, context_extensions=kwargs, registration=exercise_id
    )


def scaled_score(raw: float, max_score: float) -> float:
    """``raw / max`` clamped to xAPI's [0, 1] for a non-negative score."""
    return min(max(raw / max_score, 0.0), 1.0) if max_score > 0 else 0.0


def score_result(
    raw: float | None,
    max_score: float | None,
    *,
    pass_threshold: float | None = None,
    completion: bool | None = None,
    duration: timedelta | float | None = None,
) -> dict:
    """xAPI ``result`` with a score, valid under xAPI 1.0.3.

    The spec requires ``min <= raw <= max`` and ``-1 <= scaled <= 1``.

    * With a positive ``max_score``, ``raw`` is clamped to ``[0, max_score]``: points
      achieved beyond the maximum count as full marks, never as more than 100%.
      ``success`` is ``scaled >= pass_threshold`` (the activity's own pass mark, else
      ``XAPI_DEFAULT_PASS_THRESHOLD``).
    * With no ``max_score`` (None or <= 0) nothing is scoreable, so ``max``, ``scaled``
      and ``success`` are omitted rather than invented: nothing to pass or fail.
    """
    value = max(raw or 0, 0)
    out: dict = {}
    if not max_score or max_score <= 0:
        out["score"] = {"raw": value, "min": 0}
    else:
        value = min(value, max_score)
        scaled = scaled_score(value, max_score)
        threshold = default_pass_threshold() if pass_threshold is None else pass_threshold
        out["score"] = {"raw": value, "min": 0, "max": max_score, "scaled": round(scaled, 4)}
        out["success"] = scaled >= threshold
    if completion is not None:
        out["completion"] = completion
    if (d := iso_duration(duration)) is not None:
        out["duration"] = d
    return out


def exercise_result(
    score: int | None,
    max_score: int | None,
    *,
    pass_threshold: float | None = None,
    duration: timedelta | float | None = None,
) -> dict:
    """xAPI ``result`` for a completed exercise (see ``score_result``)."""
    return score_result(score, max_score, pass_threshold=pass_threshold, completion=True, duration=duration)


def exercise_completed(
    user_id: uuid.UUID | str,
    exercise_id: str,
    exercise_name: str,
    score: int | None,
    max_score: int | None,
    *,
    pass_threshold: float | None = None,
    duration: timedelta | float | None = None,
) -> dict:
    return build_statement(
        "completed",
        user_id,
        "exercise",
        exercise_id,
        exercise_name,
        result=exercise_result(score, max_score, pass_threshold=pass_threshold, duration=duration),
        registration=exercise_id,
    )


def objective_result(points: int | None) -> dict:
    """An achieved objective: full marks out of its own points."""
    pts = max(points or 0, 0)
    return {"score": {"raw": pts, "min": 0, "max": pts}, "success": True}


def objective_achieved(
    user_id: uuid.UUID | str,
    objective_id: str,
    objective_name: str,
    points: int,
    *,
    exercise_id: str | None = None,
) -> dict:
    return build_statement(
        "passed",
        user_id,
        "objective",
        objective_id,
        objective_name,
        result=objective_result(points),
        context_extensions={"exercise_id": exercise_id} if exercise_id else None,
        registration=exercise_id,
    )


def scenario_started(user_id: uuid.UUID | str, scenario_id: str, scenario_name: str, range_id: str) -> dict:
    return build_statement(
        "initialized",
        user_id,
        "scenario",
        scenario_id,
        scenario_name,
        context_extensions={"range_id": range_id},
    )


# ── Fire-and-forget emitter (for FastAPI BackgroundTasks / Celery) ────
import logging as _logging  # noqa: E402

_xapi_logger = _logging.getLogger("truenorth.xapi")


def emit_statement_sync(statement: dict, timeout: float = 2.0) -> bool:
    """Send an xAPI statement synchronously to the LRS.

    Designed for use from ``fastapi.BackgroundTasks``. Swallows all errors —
    xAPI emission is advisory: it must never block or fail a lifecycle
    transition.
    """
    return get_lms_backend().emit_statement_sync(statement, timeout=timeout)


def emit_lifecycle(
    background_tasks,  # fastapi.BackgroundTasks
    verb_key: str,
    user_id: uuid.UUID | str,
    activity_type: str,
    activity_id: str,
    activity_name: str,
    result: dict | None = None,
    context_extensions: dict | None = None,
    *,
    registration: uuid.UUID | str | None = None,
    language: str | None = None,
) -> None:
    """Queue a lifecycle xAPI statement for background emission.

    Never raises: emission is advisory and must not fail the request that caused it. A
    statement that cannot be built is logged and dropped, not sent half-formed.
    """
    try:
        stmt = build_statement(
            verb_key,
            user_id,
            activity_type,
            activity_id,
            activity_name,
            result=result,
            context_extensions=context_extensions,
            registration=registration,
            language=language,
        )
        background_tasks.add_task(emit_statement_sync, stmt)
    except Exception as exc:  # pragma: no cover
        _xapi_logger.warning("xAPI statement queue failed: %s", exc)
