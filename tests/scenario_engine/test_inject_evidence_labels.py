"""Simulated injects are labelled synthetic, seeded per exercise, and claim no host effect (R0-B).

Every injector in the engine builds records and touches nothing (execution_mode
"simulated"), yet email_phish said ``delivered: True``, identity_new_admin_user said
``created: True``, http_burst reported HTTP 200 responses, and ad_attack / c2_beacon /
network_scan / file_operation drew from the process-global ``random``.
"""

from __future__ import annotations

import pytest
from scenario_engine import injectors as engine
from scenario_engine.injectors.ad_attack import ADAttackInjector
from scenario_engine.injectors.c2_beacon import C2BeaconInjector
from scenario_engine.injectors.file_operation import FileOperationInjector
from scenario_engine.injectors.network_scan import NetworkScanInjector

CTX = engine.RangeContext(range_id="r-1", tenant_id="t-1", exercise_id="ex-1")

# One run per injector variant: every simulated injector, every branch.
CASES = [
    *[("ad_attack", {"attack_type": a, "target_dc": "dc01"}) for a in ADAttackInjector._TECHNIQUE_MAP],
    *[("c2_beacon", {"c2_type": c, "target_host": "ws01", "beacon_count": 3}) for c in C2BeaconInjector._C2_TYPES],
    *[("file_operation", {"operation": o, "target_host": "ws01"}) for o in FileOperationInjector._OPERATIONS],
    *[("network_scan", {"scan_type": s, "target_network": "10.0.0.0/29"}) for s in NetworkScanInjector._SCAN_TYPES],
    ("dns_spike", {"domains": ["a.example"], "count": 3}),
    ("http_burst", {"url": "http://c2.example/x", "count": 2}),
    ("email_phish", {}),
    ("identity_new_admin_user", {}),
    ("simulated_execution", {"technique": "T1059"}),
]
_IDS = [f"{a}:{next(iter(p.values()), '')}" for a, p in CASES]
# The only fields that follow the wall clock (c2_beacon's callback times); the seed fixes
# everything else. test_only_the_known_fields_follow_the_clock keeps this list complete.
_CLOCK = {"timestamp_epoch"}


def _strip_clock(value):
    if isinstance(value, dict):
        return {k: _strip_clock(v) for k, v in value.items() if k not in _CLOCK}
    if isinstance(value, list):
        return [_strip_clock(v) for v in value]
    return value


def _leaf_diffs(a, b, key=None):
    """The keys of the leaves that differ between two results of the same shape."""
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        return {d for k in a for d in _leaf_diffs(a[k], b[k], k)}
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return {d for x, y in zip(a, b, strict=True) for d in _leaf_diffs(x, y, key)}
    return set() if a == b else {key}


@pytest.fixture
def clock(monkeypatch):
    """Set the wall clock the injectors read (time.time)."""
    import time

    def _set(at: float):
        monkeypatch.setattr(time, "time", lambda: at)

    return _set


def _claims(value, found=None):
    """Every truthy delivered / created / executed field anywhere in a result."""
    found = [] if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("delivered", "created", "executed") and v:
                found.append(k)
            _claims(v, found)
    elif isinstance(value, list):
        for v in value:
            _claims(v, found)
    return found


def test_every_simulated_injector_is_covered():
    simulated = {a for a in engine.list_injectors() if engine.injector_profile(a)["execution_mode"] == "simulated"}
    assert simulated == {a for a, _ in CASES}


@pytest.mark.parametrize(("action", "params"), CASES, ids=_IDS)
def test_simulated_results_are_labelled_synthetic_and_scoped(action, params):
    res = engine.run_inject(action, params, CTX)
    assert res.success, res.detail
    assert res.simulated is True and res.evidence_class == "synthetic" and res.execution_mode == "simulated"
    assert res.raw["simulated"] is True and res.raw["evidence_class"] == "synthetic"
    assert res.raw["exercise_id"] == "ex-1" and res.raw["range_id"] == "r-1"
    assert res.raw["inject_id"] == res.inject_id and res.raw["seed"] == res.seed
    for event in res.telemetry:
        labels = event[engine.GROUND_TRUTH_FIELD]
        assert labels["evidence_class"] == "synthetic" and labels["simulated"] is True
        assert labels["exercise_id"] == "ex-1" and labels["range_id"] == "r-1"
        assert event["range_id"] == "r-1"


@pytest.mark.parametrize(("action", "params"), CASES, ids=_IDS)
def test_simulated_results_never_claim_a_host_effect(action, params):
    res = engine.run_inject(action, params, CTX)
    assert _claims(res.raw) == []
    assert "status" not in str(res.raw.get("results", ""))  # http_burst used to report HTTP 200s


@pytest.mark.parametrize(("action", "params"), CASES, ids=_IDS)
def test_synthetic_records_are_deterministic_per_exercise_and_inject(action, params, clock):
    clock(1_800_000_000.0)  # the same instant: the whole result must match, clock fields too
    first = engine.run_inject(action, params, CTX)
    again = engine.run_inject(action, params, CTX)
    assert first.seed == again.seed
    assert first.raw == again.raw


@pytest.mark.parametrize(("action", "params"), CASES, ids=_IDS)
def test_only_the_known_fields_follow_the_clock(action, params, clock):
    clock(1_800_000_000.0)
    before = engine.run_inject(action, params, CTX).raw
    clock(1_900_000_000.0)
    after = engine.run_inject(action, params, CTX).raw
    assert _leaf_diffs(before, after) <= _CLOCK


def test_seed_differs_by_exercise_and_by_inject():
    params = {"attack_type": "kerberoast", "target_dc": "dc01"}
    a = engine.run_inject("ad_attack", params, CTX)
    other_exercise = engine.run_inject("ad_attack", params, engine.RangeContext("r-1", "t-1", exercise_id="ex-2"))
    other_inject = engine.run_inject("ad_attack", params, CTX, inject_id="phase-2")
    assert len({a.seed, other_exercise.seed, other_inject.seed}) == 3
    assert _strip_clock(a.raw["tickets_obtained"]) != _strip_clock(other_exercise.raw["tickets_obtained"])
    assert other_inject.inject_id == "phase-2"
    assert engine.inject_seed("ex-1", "phase-2") == other_inject.seed


def test_the_global_random_is_not_consumed(monkeypatch):
    import random

    def _boom(*_a, **_k):
        raise AssertionError("injector used the process-global random")

    for name in ("random", "choice", "choices", "randint", "sample"):
        monkeypatch.setattr(random, name, _boom)
    for action, params in CASES:
        assert engine.run_inject(action, params, CTX).success, action


def test_a_simulated_claim_of_delivery_is_overridden(monkeypatch):
    from scenario_engine.injectors.simulated_execution import SimulatedExecutionInjector

    monkeypatch.setattr(
        SimulatedExecutionInjector, "execute", lambda self, ctx: {"delivered": True, "created": True, "simulated": False}
    )
    res = engine.run_inject("simulated_execution", {}, CTX)
    assert res.raw["delivered"] is False and res.raw["created"] is False
    assert res.raw["simulated"] is True and res.raw["evidence_class"] == "synthetic"


def test_a_skipped_inject_has_no_evidence_class():
    res = engine.run_inject("email_phish", {}, CTX, allow_host_effects=False)
    assert res.skipped and res.evidence_class is None and res.seed is None
