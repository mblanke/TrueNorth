"""xAPI statement builder: identity, IRIs, registration, language, results (app/xapi.py)."""

import json
import uuid
from datetime import timedelta

import pytest
from app import xapi
from app.xapi import (
    VERBS,
    build_statement,
    exercise_completed,
    exercise_launched,
    objective_achieved,
    scenario_started,
)

USER = uuid.UUID("0f5c3a52-1111-4222-8333-444455556666")


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    for name in ("XAPI_ACCOUNT_HOMEPAGE", "XAPI_IRI_BASE", "XAPI_DEFAULT_LANGUAGE", "XAPI_DEFAULT_PASS_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOMAIN", "range.example.mil")


def test_statement_has_the_required_properties():
    stmt = build_statement("launched", USER, "exercise", "ex-123", "My Exercise")
    for key in ("id", "actor", "verb", "object", "timestamp", "context"):
        assert key in stmt, f"missing {key}"
    uuid.UUID(stmt["id"])


def test_actor_is_an_account_named_by_user_id_with_no_pii():
    stmt = build_statement("launched", USER, "exercise", "1", "Ex")
    assert stmt["actor"] == {
        "objectType": "Agent",
        "account": {"homePage": "https://range.example.mil", "name": str(USER)},
    }
    text = json.dumps(stmt)
    assert "mailto:" not in text and "mbox" not in text and "@" not in text


def test_homepage_and_iri_base_are_configurable(monkeypatch):
    monkeypatch.setenv("XAPI_ACCOUNT_HOMEPAGE", "https://id.forces.example/")
    monkeypatch.setenv("XAPI_IRI_BASE", "https://id.forces.example/xapi/tn")
    stmt = build_statement("debriefed", USER, "exercise", "ex 1", "Drill", context_extensions={"range_id": "r"})
    assert stmt["actor"]["account"]["homePage"] == "https://id.forces.example"
    assert stmt["object"]["id"] == "https://id.forces.example/xapi/tn/activities/exercise/ex%201"
    assert stmt["object"]["definition"]["type"] == "https://id.forces.example/xapi/tn/activity-types/exercise"
    assert stmt["verb"]["id"] == "https://id.forces.example/xapi/tn/verbs/debriefed"
    assert list(stmt["context"]["extensions"]) == ["https://id.forces.example/xapi/tn/extensions/range_id"]


def test_iri_base_defaults_to_the_platform_url(monkeypatch):
    monkeypatch.delenv("DOMAIN")
    monkeypatch.setenv("LTI_WEB_BASE_URL", "http://localhost:14200/")
    stmt = build_statement("launched", USER, "quiz", "q1", "Quiz")
    assert stmt["object"]["id"] == "http://localhost:14200/xapi/activities/quiz/q1"
    assert stmt["actor"]["account"]["homePage"] == "http://localhost:14200"
    assert "truenorthrange.local" not in json.dumps(stmt)


def test_absolute_extension_keys_are_kept():
    stmt = build_statement("launched", USER, "exercise", "1", "Ex", context_extensions={"https://w3id.org/x": 1})
    assert stmt["context"]["extensions"] == {"https://w3id.org/x": 1}


def test_registration_is_set_when_given_and_dropped_when_malformed():
    reg = uuid.uuid4()
    assert build_statement("launched", USER, "quiz", "q", "Q", registration=reg)["context"]["registration"] == str(reg)
    assert (
        "registration" not in build_statement("launched", USER, "quiz", "q", "Q", registration="not-a-uuid")["context"]
    )


def test_language_follows_the_course_locale_else_the_default(monkeypatch):
    stmt = build_statement("launched", USER, "quiz", "q", "Quiz", language="fr_CA")
    assert stmt["context"]["language"] == "fr-CA"
    assert stmt["object"]["definition"]["name"] == {"fr-CA": "Quiz"}
    assert stmt["verb"]["display"] == {"en": "launched"}  # the verb word is English
    assert build_statement("launched", USER, "quiz", "q", "Quiz")["context"]["language"] == "en"
    monkeypatch.setenv("XAPI_DEFAULT_LANGUAGE", "en-CA")
    assert (
        build_statement("launched", USER, "quiz", "q", "Quiz", language="not a tag!")["context"]["language"] == "en-CA"
    )


def test_an_actor_needs_a_user():
    with pytest.raises(ValueError):
        build_statement("launched", "", "exercise", "1", "Ex")


def test_exercise_statements_carry_the_run_as_registration():
    ex = str(uuid.uuid4())
    assert exercise_launched(USER, ex, "Drill")["context"]["registration"] == ex
    stmt = exercise_completed(USER, ex, "Drill", score=80, max_score=100, duration=timedelta(minutes=90))
    assert stmt["context"]["registration"] == ex
    assert stmt["result"] == {
        "score": {"raw": 80, "min": 0, "max": 100, "scaled": 0.8},
        "success": True,
        "completion": True,
        "duration": "PT5400.00S",
    }


def test_pass_threshold_comes_from_the_activity_else_the_default(monkeypatch):
    assert exercise_completed(USER, "e", "D", score=75, max_score=100, pass_threshold=0.8)["result"]["success"] is False
    assert exercise_completed(USER, "e", "D", score=75, max_score=100)["result"]["success"] is True
    monkeypatch.setenv("XAPI_DEFAULT_PASS_THRESHOLD", "0.9")
    assert exercise_completed(USER, "e", "D", score=75, max_score=100)["result"]["success"] is False


def test_objective_result_is_bounded():
    stmt = objective_achieved(USER, "obj-1", "Detect C2", 50, exercise_id=str(uuid.uuid4()))
    assert stmt["result"] == {"score": {"raw": 50, "min": 0, "max": 50}, "success": True}
    assert "registration" in stmt["context"]


def test_scenario_started():
    exts = scenario_started(USER, "s1", "APT Hunt", range_id="r1")["context"]["extensions"]
    assert exts == {"https://range.example.mil/xapi/extensions/range_id": "r1"}


def test_adl_verbs_keep_their_iris():
    for key, iri in VERBS.items():
        assert iri.startswith("http://adlnet.gov/expapi/verbs/"), key


@pytest.mark.parametrize(
    ("span", "text"), [(None, None), (0, "PT0.00S"), (1.234, "PT1.23S"), (timedelta(hours=1), "PT3600.00S")]
)
def test_iso_duration(span, text):
    assert xapi.iso_duration(span) == text
