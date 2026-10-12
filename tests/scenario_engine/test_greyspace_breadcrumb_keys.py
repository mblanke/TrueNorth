"""Greyspace breadcrumb tokens are keyed, and scored by the scoring engine (review of #133).

Until 2026-10-11 a token was SHA-256("greyspace-crumb:{salt}:{exercise}:{crumb_id}") with
an empty salt nothing set, so a Student who knew the scheme could compute every token; and
the ``greyspace_breadcrumb`` validator was not a ScoringValidator method, so an objective
naming it was "unknown" (never achieved). tests/conftest.py sets a test TN_SECRETS_KEY.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from scenario_engine import injectors as engine
from scenario_engine.injectors.greyspace_breadcrumb import BreadcrumbKeyError, breadcrumb_token
from scenario_engine.scoring import ScoringEngine, validation_method
from scenario_engine.scoring.validators import ScoringValidator, UnscoredError
from scenario_engine.validators.greyspace_breadcrumb import GreyspaceBreadcrumbValidator

CTX = engine.RangeContext(range_id="r-1", tenant_id="t-1", exercise_id="ex-1")
CRUMBS = [{"id": "note", "kind": "web", "site": "pastebox.io", "path": "/p/x.txt", "content": "{{ token }}"}]
STRONG = "k" * 40


def _old_unkeyed_token(exercise: str, crumb_id: str, salt: str = "") -> str:
    """The pre-2026-10-11 scheme: anyone who knew it could compute every token."""
    return "GS-" + hashlib.sha256(f"greyspace-crumb:{salt}:{exercise}:{crumb_id}".encode()).hexdigest()[:12].upper()


def test_tokens_depend_on_the_installation_key(monkeypatch):
    monkeypatch.setenv("GREYSPACE_CRUMB_KEY", STRONG)
    a = breadcrumb_token("ex-1", "note", "")
    monkeypatch.setenv("GREYSPACE_CRUMB_KEY", "z" * 40)
    b = breadcrumb_token("ex-1", "note", "")
    assert a != b
    assert _old_unkeyed_token("ex-1", "note") not in (a, b)
    assert a.startswith("GS-") and len(a) == len("GS-") + 16


def test_the_key_falls_back_to_tn_secrets_keys_first_key(monkeypatch):
    monkeypatch.delenv("GREYSPACE_CRUMB_KEY", raising=False)
    monkeypatch.setenv("TN_SECRETS_KEY", f"{STRONG},{'o' * 40}")
    via_fallback = breadcrumb_token("e", "c", "")
    monkeypatch.setenv("GREYSPACE_CRUMB_KEY", STRONG)
    assert breadcrumb_token("e", "c", "") == via_fallback


@pytest.mark.parametrize("crumb_key,secrets_key", [("", ""), ("short", ""), ("", "short,"), ("   ", "")])
def test_no_token_without_a_strong_key(monkeypatch, crumb_key, secrets_key):
    monkeypatch.setenv("GREYSPACE_CRUMB_KEY", crumb_key)
    monkeypatch.setenv("TN_SECRETS_KEY", secrets_key)
    with pytest.raises(BreadcrumbKeyError):
        breadcrumb_token("ex-1", "note", "")
    res = engine.run_inject("greyspace_breadcrumb", {"crumbs": CRUMBS}, CTX)
    assert not res.success and "breadcrumb tokens would be guessable" in res.detail
    with pytest.raises(BreadcrumbKeyError):  # the validator refuses to judge, too
        GreyspaceBreadcrumbValidator({"crumb_id": "note"}).validate({"exercise_id": "ex-1", "answer": "GS-X"})
    # Removing crumbs renders no token, so it needs no key.
    assert engine.run_inject("greyspace_breadcrumb", {"operation": "remove"}, CTX).success


def test_no_token_without_an_exercise():
    with pytest.raises(BreadcrumbKeyError):
        breadcrumb_token("", "note", "")


def _score(config, exercise_id="ex-1"):
    return asyncio.run(ScoringValidator.validate("greyspace_breadcrumb", config, exercise_id=exercise_id))


class TestScoringMethod:
    def test_the_right_token_passes_and_the_evidence_does_not_carry_it(self):
        token = breadcrumb_token("ex-1", "note", "")
        ok, evidence = _score({"crumb_id": "note", "answer": f"handover note said {token.lower()}"})
        assert ok and evidence == [{"check": "greyspace_breadcrumb", "crumb_id": "note", "matched": True}]
        assert token not in str(evidence)

    def test_another_exercises_token_another_crumbs_or_a_guess_fails(self):
        # A miss carries no evidence: evidence on a failed objective earns partial credit.
        assert _score({"crumb_id": "note", "answer": breadcrumb_token("ex-2", "note", "")}) == (False, [])
        assert _score({"crumb_id": "txt", "answer": breadcrumb_token("ex-1", "note", "")})[0] is False
        assert _score({"crumb_id": "note", "answer": _old_unkeyed_token("ex-1", "note")})[0] is False

    def test_no_answer_is_not_achieved(self):
        assert _score({"crumb_id": "note"}) == (False, [])
        assert _score({"crumb_id": "note", "answer": ""}) == (False, [])

    def test_the_config_may_name_the_exercise(self):
        token = breadcrumb_token("ex-7", "note", "s")
        assert _score({"crumb_id": "note", "salt": "s", "exercise_id": "ex-7", "answer": token}, None)[0]

    @pytest.mark.parametrize(
        "config,exercise_id,why",
        [({"answer": "GS-1"}, "ex-1", "no crumb_id"), ({"crumb_id": "note", "answer": "GS-1"}, None, "no exercise")],
    )
    def test_a_malformed_objective_is_unscored(self, config, exercise_id, why):
        with pytest.raises(UnscoredError, match=why):
            _score(config, exercise_id)

    def test_no_key_is_unscored_not_failed(self, monkeypatch):
        monkeypatch.delenv("GREYSPACE_CRUMB_KEY", raising=False)
        monkeypatch.setenv("TN_SECRETS_KEY", "")
        with pytest.raises(UnscoredError, match="guessable"):
            _score({"crumb_id": "note"})

    def test_the_engine_scores_it_with_its_exercise_id(self):
        token = breadcrumb_token("ex-1", "note", "")
        method = validation_method("greyspace_breadcrumb")
        assert method == "greyspace_breadcrumb"
        objectives = [
            {"id": "found", "name": "found", "max_points": 10, "validation_method": method,
             "validation_config": {"crumb_id": "note", "answer": token}},
            {"id": "wrong", "name": "wrong", "max_points": 5, "validation_method": method,
             "validation_config": {"crumb_id": "note", "answer": "GS-0000000000000000"}},
            {"id": "broken", "name": "broken", "max_points": 5, "validation_method": method,
             "validation_config": {"answer": token}},
        ]
        result = asyncio.run(ScoringEngine("ex-1", objectives).evaluate())
        by_id = {r.objective_id: r for r in result.objectives}
        assert by_id["found"].achieved and by_id["found"].points_awarded == 10
        assert not by_id["wrong"].achieved and by_id["wrong"].points_awarded == 0
        assert "broken" in result.unscored and result.total_score == 10
