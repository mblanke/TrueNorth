"""run_inject: the registry bridge the worker's inject dispatch calls."""

from __future__ import annotations

import pytest
from scenario_engine import injectors as engine

CTX = engine.RangeContext(range_id="r-1", tenant_id="t-1", exercise_id="ex-1")

# Injectors whose contract is to act on range hosts; skipped where there are none.
HOST_TOUCHING = {"dns_spike", "http_burst", "email_phish", "identity_new_admin_user"}


def test_params_reach_the_injector():
    """The registry used to build every injector with no params, so each ran on defaults."""
    res = engine.run_inject("simulated_execution", {"technique": "T1059", "target": "ws-07"}, CTX)
    assert res.success and res.raw["technique"] == "T1059" and res.raw["target"] == "ws-07"
    assert res.mitre_technique == "T1059"


def test_a_summary_telemetry_event_is_labelled_simulated_and_scoped():
    res = engine.run_inject("simulated_execution", {"technique": "T1059", "target": "ws-07"}, CTX)
    [event] = res.telemetry
    assert event["range_id"] == "r-1" and event["tenant_id"] == "t-1" and "@timestamp" in event
    labels = event[engine.GROUND_TRUTH_FIELD]
    assert labels["exercise_id"] == "ex-1" and labels["inject_action"] == "simulated_execution"
    assert labels["action"] == "simulated_execution" and labels["module"] == "truenorth.inject"
    assert labels["simulated"] is True
    assert labels["technique_id"] == "T1059" and labels["target"] == "ws-07"


def test_no_label_sits_beside_the_observable_fields():
    """Security sweep H4: a Student querying inject_action / exercise_id / event.module /
    threat.technique.id matched exactly the inject and was credited without detecting it."""
    [event] = engine.run_inject("simulated_execution", {"technique": "T1059", "target": "ws-07"}, CTX).telemetry
    assert set(event) == {"@timestamp", "event.kind", "range_id", "tenant_id", engine.GROUND_TRUTH_FIELD}


def test_every_registered_injector_declares_its_contract():
    for action in engine.list_injectors():
        profile = engine.injector_profile(action)
        assert profile["execution_mode"] == "simulated", action  # none delivers to hosts today
        assert profile["touches_range_hosts"] is (action in HOST_TOUCHING), action


@pytest.mark.parametrize("action", sorted(HOST_TOUCHING))
def test_host_touching_injectors_are_skipped_without_hosts(action):
    res = engine.run_inject(action, {"domains": ["a"], "count": 1, "url": "http://x"}, CTX, allow_host_effects=False)
    assert res.skipped and not res.success and res.raw is None and res.telemetry is None


def test_the_authored_inject_prefix_names_the_same_injector():
    res = engine.run_inject("inject.simulated_execution", {"technique": "T1003"}, CTX)
    assert res.success and res.action == "simulated_execution"
    assert res.telemetry[0][engine.GROUND_TRUTH_FIELD]["inject_action"] == "simulated_execution"
    assert engine.injector_profile("inject.dns_spike") == engine.injector_profile("dns_spike")


def test_a_placeholder_inject_is_unknown_not_silently_fired():
    """QSP paths emit ``inject.activity`` as a placeholder; there is no such injector."""
    res = engine.run_inject("inject.activity", {}, CTX)
    assert not res.success and "No injector registered for action 'inject.activity'" in res.detail


def test_unknown_action_fails():
    res = engine.run_inject("deploy_malware", {}, CTX)
    assert not res.success and not res.skipped
    assert res.detail == "No injector registered for action 'deploy_malware'"


def test_missing_required_params_fail_cleanly():
    res = engine.run_inject("network_scan", {}, CTX)
    assert not res.success and res.detail.startswith("Invalid params")


def test_a_non_dict_result_is_an_injector_error(monkeypatch):
    class Bad(engine.BaseInjector):
        name = "bad_test"
        touches_range_hosts = False

        def execute(self, context):
            return ["not", "a", "dict"]

    monkeypatch.setitem(engine._INJECTOR_REGISTRY, "bad_test", Bad)
    res = engine.run_inject("bad_test", {}, CTX)
    assert not res.success and res.detail == "Injector error: injector returned list, expected dict"


def test_an_injectors_own_telemetry_is_kept_and_scoped(monkeypatch):
    class Own(engine.BaseInjector):
        name = "own_test"
        touches_range_hosts = False

        def execute(self, context):
            return {"telemetry": [{"message": "a"}, {"message": "b"}, "junk"]}

    monkeypatch.setitem(engine._INJECTOR_REGISTRY, "own_test", Own)
    res = engine.run_inject("own_test", {}, CTX)
    assert [e["message"] for e in res.telemetry] == ["a", "b"]
    assert all(e[engine.GROUND_TRUTH_FIELD]["exercise_id"] == "ex-1" for e in res.telemetry)
