"""Scoring validators are judged from evidence, refused, or unscored; never a free pass (R0-B).

Before 2026-10-12 ``firewall_rule`` counted every expected rule as matched (always
achieved); ``process_killed``, ``user_disabled`` and ``network_isolated`` returned a
"pending_verification" evidence row that earned partial credit; an unknown validator
was "not achieved"; a manual objective could approve itself from scenario content.

Each validator is checked for a known positive, a known negative, zero evidence and an
event store that is down. All stores here are fixtures (NullEventStore or a stub): this
is the scoring contract, not a run against a live range.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from runner.run import evaluate_objectives
from scenario_engine.event_stores import BaseEventStore, EventHits, NullEventStore
from scenario_engine.objective_catalogue import REFUSED_VALIDATORS, evidence_class
from scenario_engine.scoring import ScoringEngine, UnscoredError, validation_method
from scenario_engine.scoring.validators import ScoringValidator

SYNTH = {"tn_ground_truth": {"simulated": True, "evidence_class": "synthetic", "exercise_id": "ex-1"}}


def _validate(method, config, store=None, **kw):
    return asyncio.run(ScoringValidator.validate(method, config, event_store=store, **kw))


class _Down(BaseEventStore):
    async def search(self, index, query, size=20):
        raise ConnectionError("store down")


class _Many(BaseEventStore):
    """More matches than one page: the page holds only synthetic events."""

    async def search(self, index, query, size=20):
        return EventHits(total=500, hits=[{"_id": str(i), "_source": {**SYNTH, "n": i}} for i in range(size)])


# ── refused and unknown validators ─────────────────────────────────────


@pytest.mark.parametrize("method", sorted(REFUSED_VALIDATORS))
def test_refused_validators_are_unscored_never_passed(method):
    # The config that used to pass firewall_rule outright.
    config = {"expected_rules": [{"port": 443, "action": "block"}], "process_name": "a.exe", "host_ip": "10.0.0.1"}
    with pytest.raises(UnscoredError, match="refused") as exc:
        _validate(method, config, NullEventStore([{"any": "event"}]))
    assert "manual_ack" in str(exc.value)


def test_refused_objectives_score_nothing_in_the_engine():
    objectives = [
        {"id": m, "name": m, "max_points": 10, "validation_method": m, "validation_config": {"expected_rules": [{}]}}
        for m in sorted(REFUSED_VALIDATORS)
    ]
    result = asyncio.run(ScoringEngine("ex-1", objectives, event_store=NullEventStore()).evaluate())
    assert result.total_score == 0 and result.objectives == []
    assert set(result.unscored) == set(REFUSED_VALIDATORS)


@pytest.mark.parametrize("name", ["network_isolation", "artifact_removal", "does_not_exist"])
def test_unknown_validators_are_unscored_not_failed(name):
    with pytest.raises(UnscoredError, match="unknown validator"):
        _validate(validation_method(name), {})


# ── user_disabled: Windows Security 4725, observed only ───────────────


def _disabled(user="svc_backup", **extra):
    return {"EventID": 4725, "TargetUserName": user, "host": "v-dc01", **extra}


@pytest.mark.parametrize(
    "event",
    [
        _disabled(),
        {"winlog.event_id": 4725, "winlog.event_data.TargetUserName": "svc_backup"},
        {"event": {"code": 4725}, "user": {"target": {"name": "svc_backup"}}},
    ],
    ids=["flat", "winlogbeat", "ecs"],
)
def test_user_disabled_known_positive(event):
    ok, evidence = _validate("user_disabled", {"username": "svc_backup"}, NullEventStore([event]))
    assert ok is True and len(evidence) == 1


@pytest.mark.parametrize(
    "events",
    [
        [_disabled(user="someone_else")],  # another account was disabled
        [{"EventID": 4720, "TargetUserName": "svc_backup"}],  # created, not disabled
        [{"EventID": 4726, "TargetUserName": "svc_backup"}],  # deleted is not "disabled"
        [_disabled(**SYNTH)],  # a simulated inject's record
    ],
    ids=["other-user", "4720", "4726", "synthetic-only"],
)
def test_user_disabled_known_negative_earns_nothing(events):
    ok, evidence = _validate("user_disabled", {"username": "svc_backup"}, NullEventStore(events))
    assert ok is False and evidence == []
    objective = {"id": "o", "name": "o", "max_points": 20, "validation_method": "user_disabled",
                 "validation_config": {"username": "svc_backup"}, "partial_credit": True}
    result = asyncio.run(ScoringEngine("ex-1", [objective], event_store=NullEventStore(events)).evaluate())
    assert result.total_score == 0 and result.unscored == {}


def test_user_disabled_zero_evidence():
    assert _validate("user_disabled", {"username": "svc_backup"}, NullEventStore([])) == (False, [])


def test_user_disabled_store_down_is_unscored():
    with pytest.raises(UnscoredError, match="event store unavailable"):
        _validate("user_disabled", {"username": "svc_backup"}, _Down())


def test_user_disabled_without_a_store_is_unscored():
    with pytest.raises(UnscoredError, match="no event store"):
        _validate("user_disabled", {"username": "svc_backup"})


@pytest.mark.parametrize("username", ["", "a OR EventID:*", 'x" OR "y', "{{ insider_user }}", "*"])
def test_user_disabled_refuses_unusable_names(username):
    with pytest.raises(UnscoredError):
        _validate("user_disabled", {"username": username}, NullEventStore([_disabled(user=username)]))


def test_one_event_matching_two_conventions_counts_once():
    both = {"EventID": 4725, "TargetUserName": "svc_backup", "winlog.event_id": 4725,
            "winlog.event_data.TargetUserName": "svc_backup"}
    ok, evidence = _validate("user_disabled", {"username": "svc_backup"}, NullEventStore([both]))
    assert ok is True and len(evidence) == 1


# ── opensearch_query require_observed ─────────────────────────────────

QUERY = {"query": "event_type:flow AND dst_ip:10.60.200.10", "threshold": 2}
FLOW = {"event_type": "flow", "dst_ip": "10.60.200.10"}


def test_detection_default_counts_synthetic_inject_records():
    # Detecting a simulated inject's records is what a detection objective scores.
    ok, _ = _validate("opensearch_query", QUERY, NullEventStore([{**FLOW, **SYNTH}] * 2))
    assert ok is True


def test_require_observed_is_not_satisfied_by_synthetic_records():
    store = NullEventStore([{**FLOW, **SYNTH, "n": i} for i in range(5)])
    ok, evidence = _validate("opensearch_query", {**QUERY, "require_observed": True}, store)
    assert ok is False and evidence == []
    objective = {"id": "o", "name": "o", "max_points": 10, "validation_method": "opensearch_query",
                 "validation_config": {**QUERY, "require_observed": True}, "partial_credit": True}
    result = asyncio.run(ScoringEngine("ex-1", [objective], event_store=store).evaluate())
    assert result.total_score == 0  # no partial credit for synthetic matches either


def test_require_observed_passes_on_observed_records():
    store = NullEventStore([{**FLOW, "n": 1}, {**FLOW, "n": 2}, {**FLOW, **SYNTH}])
    ok, evidence = _validate("opensearch_query", {**QUERY, "require_observed": True}, store)
    assert ok is True and len(evidence) == 2
    assert all(evidence_class(h["_source"]) == "observed" for h in evidence)


def test_require_observed_zero_evidence():
    assert _validate("opensearch_query", {**QUERY, "require_observed": True}, NullEventStore([])) == (False, [])


def test_require_observed_store_down_is_unscored():
    with pytest.raises(UnscoredError, match="event store unavailable"):
        _validate("opensearch_query", {**QUERY, "require_observed": True}, _Down())


def test_require_observed_cannot_decide_past_one_page():
    with pytest.raises(UnscoredError, match="more than"):
        _validate("opensearch_query", {**QUERY, "require_observed": True}, _Many())


def test_require_observed_must_be_a_boolean():
    with pytest.raises(UnscoredError, match="require_observed"):
        _validate("opensearch_query", {**QUERY, "require_observed": "yes"}, NullEventStore([FLOW, FLOW]))


@pytest.mark.parametrize(
    ("source", "want"),
    [
        ({"a": 1}, "observed"),
        (SYNTH, "synthetic"),
        ({"tn_ground_truth": {"simulated": False, "evidence_class": "live"}}, "live"),
        ({"tn_ground_truth": {"inject_action": "x"}}, "synthetic"),  # unlabelled inject: not observed
        ({"truenorth_simulated": True}, "synthetic"),  # flat labels, before 2026-10-08
        ({"event.module": "truenorth.inject"}, "synthetic"),
    ],
)
def test_evidence_class(source, want):
    assert evidence_class(source) == want


# ── manual_ack: an explicit instructor action ─────────────────────────


def test_manual_with_instructor_approval_is_achieved():
    ok, evidence = _validate("manual", {"approved": True, "notes": "checked"})
    assert ok is True and evidence[0]["approved"] is True


@pytest.mark.parametrize("config", [{}, {"approved": False}, {"approved": "true"}, {"approved": 1}])
def test_manual_without_approval_is_awaiting_not_failed(config):
    with pytest.raises(UnscoredError, match="awaiting instructor"):
        _validate("manual", config)


def test_content_cannot_pre_approve_a_manual_objective():
    scenario = {"objectives": [{"id": "o", "validator": "manual_ack", "params": {"approved": True}, "points": 5}]}
    [result] = evaluate_objectives(scenario, {"range_id": "rx", "tenant_id": "t"}, event_store=NullEventStore())
    assert result["passed"] is None and result["points_earned"] == 0
    assert "awaiting instructor" in result["unscored"]


# ── deliverable ───────────────────────────────────────────────────────


def test_deliverable_without_a_location_is_unscored():
    # Content says params.key (a store key); nothing here resolves it.
    with pytest.raises(UnscoredError, match="storage_path"):
        _validate("deliverable", {"key": "incident-report.pdf"})


def test_a_missing_deliverable_earns_no_partial_credit():
    objective = {"id": "o", "name": "o", "max_points": 50, "validation_method": "deliverable",
                 "validation_config": {"storage_path": "/nonexistent/report-12345.pdf"}, "partial_credit": True}
    result = asyncio.run(ScoringEngine("ex-1", [objective]).evaluate())
    [r] = result.objectives
    assert r.achieved is False and r.points_awarded == 0 and "error" in r.evidence[0]


def test_a_real_deliverable_is_achieved():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("x" * 200)
    try:
        ok, _ = _validate("deliverable", {"storage_path": fh.name, "min_size_bytes": 10})
        assert ok is True
    finally:
        os.unlink(fh.name)
