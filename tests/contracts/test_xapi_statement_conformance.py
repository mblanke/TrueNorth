"""xAPI 1.0.3 conformance for every statement shape ``app.xapi`` emits.

The schema at ``docs/interfaces/xapi-statement.schema.json`` is a structural
subset of the xAPI 1.0.3 Statement model. Spec rules JSON Schema cannot express
(score.min <= score.raw <= score.max) are asserted here in Python.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from app import xapi
from jsonschema import Draft7Validator

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "interfaces" / "xapi-statement.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft7Validator(_SCHEMA)

_EMAIL = "student@example.mil"
_NAME = "Test Student"


def _errors(stmt: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in _VALIDATOR.iter_errors(stmt)]


def _assert_conformant(stmt: dict) -> None:
    errors = _errors(stmt)
    assert not errors, "xAPI 1.0.3 schema violations:\n  " + "\n  ".join(errors)

    # id, when present, is an RFC 4122 UUID (the pattern checks shape; this checks it parses).
    if "id" in stmt:
        uuid.UUID(stmt["id"])
    # timestamp is ISO 8601 and carries a zone (spec SHOULD; we emit UTC, so hold us to it).
    ts = datetime.fromisoformat(stmt["timestamp"])
    assert ts.tzinfo is not None, "timestamp has no time zone"
    if "registration" in stmt.get("context", {}):
        uuid.UUID(stmt["context"]["registration"])

    # Spec: raw MUST be between min and max when those are present.
    score = stmt.get("result", {}).get("score", {})
    if "raw" in score:
        if "min" in score:
            assert score["raw"] >= score["min"], f"score.raw {score['raw']} < score.min {score['min']}"
        if "max" in score:
            assert score["raw"] <= score["max"], f"score.raw {score['raw']} > score.max {score['max']}"


def test_schema_is_valid_draft7():
    Draft7Validator.check_schema(_SCHEMA)


# ---------------------------------------------------------------------------
# Every statement kind the module builds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb_key", sorted(xapi.VERBS))
def test_build_statement_every_registered_verb(verb_key):
    stmt = xapi.build_statement(verb_key, _EMAIL, _NAME, "exercise", "ex-1", "Blue Team Drill")
    _assert_conformant(stmt)
    assert stmt["verb"]["id"] == xapi.VERBS[verb_key]


def test_build_statement_unregistered_verb_still_gets_an_iri():
    stmt = xapi.build_statement("debriefed", _EMAIL, _NAME, "exercise", "ex-1", "Drill")
    _assert_conformant(stmt)
    assert stmt["verb"]["id"] == "http://truenorthrange.local/verbs/debriefed"


def test_build_statement_with_result_and_extensions():
    stmt = xapi.build_statement(
        "answered",
        _EMAIL,
        _NAME,
        "quiz-question",
        str(uuid.uuid4()),
        "Which port does LDAPS use?",
        result={"success": True, "score": {"raw": 1, "max": 1}},
        context_extensions={"quiz_id": str(uuid.uuid4()), "attempt_id": str(uuid.uuid4())},
    )
    _assert_conformant(stmt)
    assert all(k.startswith("http://truenorthrange.local/extensions/") for k in stmt["context"]["extensions"])


def test_build_statement_empty_activity_id():
    # events.py passes data.get("session_id", "") for login/logout; the object id must still be an IRI.
    stmt = xapi.build_statement("experienced", _EMAIL, _NAME, "session", "", "User Login")
    _assert_conformant(stmt)


@pytest.mark.parametrize("extra", [{}, {"range_id": "r-1", "scenario_id": "s-1"}])
def test_exercise_launched(extra):
    _assert_conformant(xapi.exercise_launched(_EMAIL, _NAME, "ex-1", "Blue Team Drill", **extra))


@pytest.mark.parametrize(
    ("score", "max_score"),
    [
        (80, 100),
        (100, 100),
        (0, 100),  # zero path: a Student who scored nothing
        (0, 0),  # empty path: an exercise with no scoreable objectives
    ],
)
def test_exercise_completed(score, max_score):
    stmt = xapi.exercise_completed(_EMAIL, _NAME, "ex-1", "Drill", score=score, max_score=max_score)
    _assert_conformant(stmt)
    assert stmt["result"]["completion"] is True


@pytest.mark.parametrize("points", [0, 50])
def test_objective_achieved(points):
    stmt = xapi.objective_achieved(_EMAIL, _NAME, "obj-1", "Detect C2 beacon", points)
    _assert_conformant(stmt)
    assert stmt["result"]["success"] is True


def test_scenario_started():
    _assert_conformant(xapi.scenario_started(_EMAIL, _NAME, "scn-1", "Ransomware Lite", "range-1"))


class _CapturingBackgroundTasks:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add_task(self, fn, *args, **kwargs) -> None:
        self.calls.append((fn, args, kwargs))


def test_emit_lifecycle_queues_a_conformant_statement():
    tasks = _CapturingBackgroundTasks()
    xapi.emit_lifecycle(
        tasks,
        verb_key="terminated",
        user_email=_EMAIL,
        user_name=_NAME,
        activity_type="exercise",
        activity_id=str(uuid.uuid4()),
        activity_name="Drill",
        context_extensions={"pause": True},
    )
    assert len(tasks.calls) == 1
    fn, args, _ = tasks.calls[0]
    assert fn is xapi.emit_statement_sync
    _assert_conformant(args[0])


# ---------------------------------------------------------------------------
# The schema rejects what the spec rejects (guards against a vacuous schema)
# ---------------------------------------------------------------------------


def _valid() -> dict:
    return xapi.build_statement("launched", _EMAIL, _NAME, "exercise", "ex-1", "Drill")


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda s: s.pop("actor"), "actor is required"),
        (lambda s: s.pop("verb"), "verb is required"),
        (lambda s: s.pop("object"), "object is required"),
        (lambda s: s["verb"].pop("id"), "verb.id is required"),
        (lambda s: s["verb"].__setitem__("id", "launched"), "verb.id must be an absolute IRI"),
        (lambda s: s["actor"].pop("mbox"), "agent needs an IFI"),
        (lambda s: s["actor"].__setitem__("openid", "https://id.example/u"), "agent has two IFIs"),
        (lambda s: s["actor"].__setitem__("mbox", _EMAIL), "mbox must be a mailto: IRI"),
        (lambda s: s.__setitem__("id", "not-a-uuid"), "id must be a UUID"),
        (lambda s: s.__setitem__("timestamp", "yesterday"), "timestamp must be ISO 8601"),
        (lambda s: s["context"].__setitem__("registration", "abc"), "registration must be a UUID"),
        (lambda s: s.__setitem__("result", {"score": {"scaled": 1.5}}), "scaled is within [-1, 1]"),
        (lambda s: s.__setitem__("result", {"duration": "90 seconds"}), "duration is ISO 8601"),
        (lambda s: s["context"].__setitem__("extensions", {"range_id": "r"}), "extension keys are IRIs"),
        (lambda s: s.__setitem__("score", 1), "no properties outside the spec"),
    ],
)
def test_schema_rejects_nonconformant(mutate, why):
    stmt = _valid()
    mutate(stmt)
    assert _errors(stmt), f"schema accepted a statement where {why}"


def test_schema_accepts_account_ifi_and_registration():
    stmt = _valid()
    stmt["actor"] = {"objectType": "Agent", "account": {"homePage": "https://lms.example", "name": "s-42"}}
    stmt["context"]["registration"] = str(uuid.uuid4())
    _assert_conformant(stmt)
