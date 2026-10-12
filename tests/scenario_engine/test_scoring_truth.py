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
from datetime import UTC, datetime, timedelta

import pytest
from runner.run import evaluate_objectives
from scenario_engine.event_stores import BaseEventStore, EventHits, NullEventStore
from scenario_engine.objective_catalogue import MAX_SCAN, REFUSED_VALIDATORS, evidence_class
from scenario_engine.scoring import ScoringEngine, UnscoredError, validation_method
from scenario_engine.scoring.validators import ScoringValidator

SYNTH = {"tn_ground_truth": {"simulated": True, "evidence_class": "synthetic", "exercise_id": "ex-1"}}


def _validate(method, config, store=None, **kw):
    return asyncio.run(ScoringValidator.validate(method, config, event_store=store, **kw))


class _Down(BaseEventStore):
    async def search(self, index, query, size=20, offset=0):
        raise ConnectionError("store down")


class _Pages(NullEventStore):
    """Records every page asked for."""

    def __init__(self, events):
        super().__init__(events)
        self.pages: list[tuple[int, int]] = []

    async def search(self, index, query, size=20, offset=0):
        self.pages.append((offset, size))
        return await super().search(index, query, size, offset)


class _TooMany(BaseEventStore):
    async def search(self, index, query, size=20, offset=0):
        return EventHits(total=MAX_SCAN + 1, hits=[{"_id": str(offset + i), "_source": {}} for i in range(size)])


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
    # Not in the denominator: reported apart, not as zeros dragging the grade down.
    assert result.max_score == 0 and result.unscored_points == 10 * len(REFUSED_VALIDATORS)


def test_unscored_objectives_are_not_in_max_score():
    objectives = [
        {"id": "ack", "name": "ack", "max_points": 30, "validation_method": "manual", "validation_config": {"approved": True}},
        {"id": "wait", "name": "wait", "max_points": 70, "validation_method": "manual", "validation_config": {}},
    ]
    result = asyncio.run(ScoringEngine("ex-1", objectives).evaluate())
    assert (result.total_score, result.max_score, result.percentage, result.grade) == (30, 30, 100.0, "A")
    assert result.unscored == {"wait": "awaiting instructor acknowledgement"} and result.unscored_points == 70


def test_runner_reports_unscored_points_apart(tmp_path, monkeypatch):
    import json
    import sys

    from runner import run

    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        "name: s\nversion: '1'\nrange_template: t\ntimeline: []\nobjectives:\n"
        "  - {id: d, type: detection, validator: opensearch_query, points: 40, params: {query: 'a:b'}}\n"
        "  - {id: m, type: response, validator: manual_ack, points: 60}\n"
    )
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["run", str(scenario), "--event-store", "null", "--output", str(out)])
    run.main()
    assert json.loads(out.read_text())["score"] == {"earned": 0, "max": 0, "unscored": 100}


@pytest.mark.parametrize("name", ["network_isolation", "artifact_removal", "does_not_exist"])
def test_unknown_validators_are_unscored_not_failed(name):
    with pytest.raises(UnscoredError, match="unknown validator"):
        _validate(validation_method(name), {})


# ── user_disabled: Windows Security 4725, observed, in the exercise window ──

START, END = datetime(2026, 10, 12, 9, 0, tzinfo=UTC), datetime(2026, 10, 12, 11, 0, tzinfo=UTC)
WINDOW = (START, END)


def _at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


def _sec(code=4725, user="svc_backup", minute=30, **extra):
    """A flat-convention Security event, ingested ``minute`` minutes into the window."""
    return {"EventID": code, "TargetUserName": user, "host": "v-dc01", "@timestamp": _at(minute),
            "truenorth": {"ingested_at": _at(minute)}, **extra}


def _disabled_check(events, username="svc_backup", window=WINDOW, store=None):
    return _validate("user_disabled", {"username": username}, store or NullEventStore(events), window=window)


@pytest.mark.parametrize(
    "event",
    [
        _sec(),
        {"winlog.event_id": "4725", "winlog.event_data.TargetUserName": "svc_backup", "truenorth.ingested_at": _at(5)},
        {"event": {"code": "4725"}, "user": {"target": {"name": "svc_backup"}}, "truenorth": {"ingested_at": _at(5)}},
        _sec(user="SVC_Backup"),  # account names are case-insensitive in Windows
    ],
    ids=["flat", "winlogbeat", "ecs", "case"],
)
def test_user_disabled_known_positive(event):
    ok, evidence = _disabled_check([event])
    assert ok is True and len(evidence) == 1


def test_user_disabled_then_re_enabled_is_not_achieved():
    ok, evidence = _disabled_check([_sec(minute=10), _sec(code=4722, minute=20)])
    assert ok is False and evidence == []


def test_re_enabled_then_disabled_again_is_achieved():
    assert _disabled_check([_sec(minute=10), _sec(code=4722, minute=20), _sec(minute=40)])[0] is True


@pytest.mark.parametrize(
    "events",
    [
        [_sec(user="someone_else")],  # another account was disabled
        [_sec(code=4720)],  # created, not disabled
        [_sec(code=4726)],  # deleted is not "disabled"
        [_sec(**SYNTH)],  # a simulated inject's record
        [_sec(minute=-30)],  # before the exercise: an earlier exercise on a reused range
        [_sec(minute=180)],  # after it ended
        [{"EventID": 4725, "TargetUserName": "svc_backup"}],  # no ingest time: cannot be placed in the window
        [],
    ],
    ids=["other-user", "4720", "4726", "synthetic-only", "before", "after", "no-ingest-time", "zero"],
)
def test_no_disable_in_the_window_is_unscored_never_a_fail(events):
    # A missing 4725 may be a telemetry gap; a real disable must never score as a fail.
    with pytest.raises(UnscoredError, match="no observed 4725 or 4722"):
        _disabled_check(events)
    objective = {"id": "o", "name": "o", "max_points": 20, "validation_method": "user_disabled",
                 "validation_config": {"username": "svc_backup"}, "partial_credit": True}
    result = asyncio.run(
        ScoringEngine("ex-1", [objective], event_store=NullEventStore(events), window_start=START, window_end=END).evaluate()
    )
    assert result.total_score == 0 and "o" in result.unscored


def test_a_disable_and_re_enable_at_the_same_instant_is_unscored():
    with pytest.raises(UnscoredError, match="same instant"):
        _disabled_check([_sec(minute=10), _sec(code=4722, minute=10)])


def test_user_disabled_without_an_exercise_window_is_unscored():
    with pytest.raises(UnscoredError, match="no exercise window"):
        _disabled_check([_sec()], window=None)


def test_content_cannot_supply_the_window():
    config = {"username": "svc_backup", "window_start": _at(-600)}
    with pytest.raises(UnscoredError, match="no exercise window"):
        _validate("user_disabled", config, NullEventStore([_sec(minute=-30)]))


def test_the_engine_passes_its_exercise_window():
    objective = {"id": "o", "name": "o", "max_points": 20, "validation_method": "user_disabled",
                 "validation_config": {"username": "svc_backup"}}
    result = asyncio.run(
        ScoringEngine("ex-1", [objective], event_store=NullEventStore([_sec()]), window_start=START).evaluate()
    )
    assert result.total_score == 20


def test_user_disabled_store_down_is_unscored():
    with pytest.raises(UnscoredError, match="event store unavailable"):
        _disabled_check([], store=_Down())


def test_user_disabled_without_a_store_is_unscored():
    with pytest.raises(UnscoredError, match="no event store"):
        _validate("user_disabled", {"username": "svc_backup"}, window=WINDOW)


@pytest.mark.parametrize("username", ["", "a OR EventID:*", 'x" OR "y', "{{ insider_user }}", "*"])
def test_user_disabled_refuses_unusable_names(username):
    with pytest.raises(UnscoredError):
        _disabled_check([_sec(user=username)], username=username)


def test_one_event_matching_two_conventions_counts_once():
    both = _sec(**{"winlog.event_id": "4725", "winlog.event_data.TargetUserName": "svc_backup"})
    ok, evidence = _disabled_check([both])
    assert ok is True and len(evidence) == 1


# ── opensearch_query require_observed ─────────────────────────────────

QUERY = {"query": "event_type:flow AND dst_ip:10.60.200.10", "threshold": 2}
OBSERVED = {**QUERY, "require_observed": True}
FLOW = {"event_type": "flow", "dst_ip": "10.60.200.10"}


def test_a_detection_objective_is_never_credited_by_the_engine():
    # The inject's own records match the answer key: that was full points with no Student
    # action. Detection credit comes only from a Student's submission (ADR 0005).
    store = NullEventStore([{**FLOW, **SYNTH}] * 2 + [FLOW] * 2)
    with pytest.raises(UnscoredError, match="Student's submitted detection"):
        _validate("opensearch_query", QUERY, store)
    objective = {"id": "o", "name": "o", "max_points": 10, "validation_method": "opensearch_query",
                 "validation_config": QUERY, "partial_credit": True}
    result = asyncio.run(ScoringEngine("ex-1", [objective], event_store=store).evaluate())
    assert result.total_score == 0 and result.max_score == 0 and result.unscored_points == 10


def test_require_observed_is_not_satisfied_by_synthetic_records():
    store = NullEventStore([{**FLOW, **SYNTH}] * 5)
    with pytest.raises(UnscoredError, match="0 observed matching events of 2"):
        _validate("opensearch_query", OBSERVED, store)
    objective = {"id": "o", "name": "o", "max_points": 10, "validation_method": "opensearch_query",
                 "validation_config": OBSERVED, "partial_credit": True}
    result = asyncio.run(ScoringEngine("ex-1", [objective], event_store=store).evaluate())
    assert result.total_score == 0 and "o" in result.unscored  # no partial credit either


def test_require_observed_passes_on_observed_records():
    # Two identical observed events are two events.
    store = NullEventStore([FLOW, FLOW, {**FLOW, **SYNTH}])
    ok, evidence = _validate("opensearch_query", OBSERVED, store)
    assert ok is True and len(evidence) == 2
    assert all(evidence_class(h["_source"]) == "observed" for h in evidence)


def test_require_observed_classifies_every_match_not_a_first_page():
    # 600 synthetic matches come first; the 2 observed ones are past the first page.
    store = _Pages([{**FLOW, **SYNTH}] * 600 + [FLOW, FLOW])
    ok, _ = _validate("opensearch_query", OBSERVED, store)
    assert ok is True and [o for o, _ in store.pages] == [0, 500]


def test_require_observed_counts_only_the_exercise_window():
    inside = {**FLOW, "truenorth.ingested_at": _at(5)}
    before = {**FLOW, "truenorth.ingested_at": _at(-5)}
    with pytest.raises(UnscoredError, match="1 observed"):
        _validate("opensearch_query", OBSERVED, NullEventStore([inside, before]), window=WINDOW)
    assert _validate("opensearch_query", OBSERVED, NullEventStore([inside, inside]), window=WINDOW)[0] is True


def test_require_observed_zero_evidence_is_unscored():
    with pytest.raises(UnscoredError, match="0 observed"):
        _validate("opensearch_query", OBSERVED, NullEventStore([]))


def test_require_observed_store_down_is_unscored():
    with pytest.raises(UnscoredError, match="event store unavailable"):
        _validate("opensearch_query", OBSERVED, _Down())


def test_too_many_matches_to_classify_is_unscored():
    with pytest.raises(UnscoredError, match="more than the"):
        _validate("opensearch_query", OBSERVED, _TooMany())


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


@pytest.mark.parametrize(
    "config",
    [
        {"key": "incident-report.pdf"},  # what content says: a store key nothing resolves
        {"storage_path": "/etc/hosts", "min_size_bytes": 1},  # passed before 2026-10-12
        {"submission": {"path": "/etc/hosts"}},  # no Student on the record
    ],
    ids=["content-key", "content-path", "anonymous"],
)
def test_a_deliverable_without_a_student_submission_is_unscored(config):
    with pytest.raises(UnscoredError, match="no Student submission"):
        _validate("deliverable", config)


def test_runner_drops_a_content_storage_path():
    scenario = {"objectives": [{"id": "o", "validator": "deliverable_check", "points": 5,
                                "params": {"storage_path": "/etc/hosts", "min_size_bytes": 1}}]}
    [result] = evaluate_objectives(scenario, {"range_id": "rx", "tenant_id": "t"}, event_store=NullEventStore())
    assert result["passed"] is None and "no Student submission" in result["unscored"]


def test_a_submission_whose_file_is_gone_is_unscored():
    with pytest.raises(UnscoredError, match="file is missing"):
        _validate("deliverable", {"submission": {"path": "/nonexistent/r-12345.pdf", "submitted_by": "s1"}})


def test_a_too_small_deliverable_earns_no_partial_credit(tmp_path):
    path = tmp_path / "r.txt"
    path.write_text("x")
    objective = {"id": "o", "name": "o", "max_points": 50, "validation_method": "deliverable",
                 "validation_config": {"submission": {"path": str(path), "submitted_by": "s1"}, "min_size_bytes": 10},
                 "partial_credit": True}
    result = asyncio.run(ScoringEngine("ex-1", [objective]).evaluate())
    [r] = result.objectives
    assert r.achieved is False and r.points_awarded == 0 and "error" in r.evidence[0]


def test_a_submitted_deliverable_is_achieved():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("x" * 200)
    try:
        ok, evidence = _validate("deliverable", {"submission": {"path": fh.name, "submitted_by": "s1"}, "min_size_bytes": 10})
        assert ok is True and evidence[0]["submitted_by"] == "s1"
    finally:
        os.unlink(fh.name)
