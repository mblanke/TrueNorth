"""COV-I: scenario-engine/runner/run.py — the standalone CLI runner.

Whole-slice coverage before this file: 73.6% (95/129 statements); the missing lines were
timecode parsing, schema-validation failures, the timeline loop (dry-run, log-only and
the compressed wait), the objective-error report, and main()'s print path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from runner.run import (
    build_range_context,
    execute_timeline,
    load_scenario,
    main,
    parse_timecode,
    validate_against_schema,
)

CTX = {"range_id": "rx", "tenant_id": "t", "vms": [], "networks": []}


def test_timecodes_parse_to_seconds():
    assert parse_timecode("0:00") == 0
    assert parse_timecode("2:30") == 150
    assert parse_timecode("12:05") == 725


@pytest.mark.parametrize("bad", ["2:3", "1:234", "x:00", ":30", "0:00:00"])
def test_a_malformed_timecode_is_refused(bad):
    with pytest.raises(ValueError, match="Invalid timecode"):
        parse_timecode(bad)


def test_schema_validation_reports_why_it_failed(caplog):
    data = {"objectives": [{"validator": 42}]}
    assert validate_against_schema(data, "no/such/schema.json") is False  # unreadable schema
    assert validate_against_schema(data, str(Path(__file__).parent / "does-not-exist.json")) is False
    assert "Schema validation failed" in caplog.text


def test_load_scenario_reports_objectives_that_could_never_be_judged(tmp_path, caplog):
    scenario = {
        "name": "covertest", "version": "1.0",
        "objectives": [{"id": "o1", "validator": "psychic", "points": 5}],
    }
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(scenario))
    loaded = load_scenario(str(path))
    assert loaded["name"] == "covertest"
    assert "unknown validator 'psychic'" in caplog.text


def test_build_range_context_is_the_standalone_three_host_lab():
    ctx = build_range_context("standalone-001")
    assert ctx["range_id"] == "standalone-001" and ctx["tenant_id"] == "standalone"
    assert [vm["name"] for vm in ctx["vms"]] == ["jump-box", "dc-01", "ws-01"]
    assert {n["name"] for n in ctx["networks"]} == {"mgmt", "srv", "user"}


def test_a_dry_run_logs_every_event_without_executing():
    scenario = {"timeline": [
        {"t": "0:00", "action": "simulated_execution", "params": {"technique": "T1059"}},
        {"t": "0:30", "action": "network_scan"},
    ]}
    results = execute_timeline(scenario, CTX, dry_run=True)
    assert results == [
        {"t": "0:00", "action": "simulated_execution", "status": "dry_run"},
        {"t": "0:30", "action": "network_scan", "status": "dry_run"},
    ]


def test_without_an_injector_registry_a_run_is_log_only_and_never_sleeps_backward():
    """Standalone, the injector registry is not importable, so events are recorded, not
    fired; an event whose offset has already passed waits zero, not negative."""
    scenario = {"timeline": [{"t": "0:01", "action": "simulated_execution", "params": {}}]}
    results = execute_timeline(scenario, CTX, dry_run=False)
    assert results == [{"t": "0:01", "action": "simulated_execution", "status": "log_only"}]


# run.py also has an injector-dispatch branch (import ``scenario-engine.injectors``, then
# ``injector.execute(params, ctx)``). It is dead twice over. INFERRED: the ImportError is
# because ``scenario-engine/injectors/`` does not exist in this layout — the registry lives
# at ``scenario-engine/scenario_engine/injectors/`` — not because of the dash: the dash
# itself is importable via ``import_module`` (OBSERVED here: ``import_module(
# "scenario-engine")`` succeeds, ``import_module("scenario-engine.injectors")`` raises
# "No module named 'scenario-engine.injectors'"). Whether other checkouts or images ship a
# different layout was not observed from this clone. With the real registry made importable
# under that name the dispatch itself crashes — the registry's injectors take
# ``execute(context)`` (OBSERVED: ``BaseInjector.execute(self, context)`` at
# scenario_engine/injectors/base.py:30), run.py passes two arguments (OBSERVED TypeError at
# run.py:134). Left uncovered rather than pinned with a fictional import name; the defect
# is reported on the card.


SCENARIO = {
    "name": "cov-i-runner", "version": "1.0",
    "timeline": [{"t": "0:00", "action": "simulated_execution", "params": {"technique": "T1059"}}],
    "objectives": [{"id": "ack", "validator": "manual_ack", "points": 10}],
}


def _write(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(SCENARIO))
    return path


def test_main_writes_a_scored_results_json(tmp_path, monkeypatch):
    scenario_path = _write(tmp_path)
    out = tmp_path / "results.json"
    monkeypatch.setattr(sys, "argv", [
        "run.py", str(scenario_path), "--range-id", "rx", "--dry-run",
        "--event-store", "null", "--output", str(out),
    ])
    main()
    results = json.loads(out.read_text())
    assert results["scenario"] == "cov-i-runner" and results["range_id"] == "rx"
    assert results["timeline"] == [{"t": "0:00", "action": "simulated_execution", "status": "dry_run"}]
    # The manual objective has no approval from the platform: unscored, 10 points apart.
    assert results["objectives"][0]["passed"] is None
    assert "awaiting instructor acknowledgement" in results["objectives"][0]["unscored"]
    assert results["score"] == {"earned": 0, "max": 0, "unscored": 10}


def test_main_prints_the_results_when_no_output_file_is_given(tmp_path, monkeypatch, capsys):
    scenario_path = _write(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run.py", str(scenario_path), "--range-id", "rx", "--dry-run", "--event-store", "null",
    ])
    main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["score"]["unscored"] == 10
