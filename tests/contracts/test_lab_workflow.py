"""The live vSphere lab workflow runs by hand only, on the self-hosted lab runner.

Until a runner labelled [self-hosted, truenorth-lab] is registered, a nightly schedule only
queues runs that fail, so it is off (docs/runbooks/lab-runner.md says how to turn it back
on). It must never run on push or pull_request: a self-hosted runner executes whatever it
checks out.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _triggers() -> dict:
    wf = yaml.safe_load((ROOT / ".github/workflows/lab.yml").read_text())
    # PyYAML reads the bare key `on` as True.
    return wf.get("on", wf.get(True))


def test_lab_runs_by_hand_only():
    triggers = _triggers()
    assert set(triggers) == {"workflow_dispatch"}, triggers
    assert "pull_request" not in triggers and "push" not in triggers


def test_the_runbook_says_the_nightly_is_off():
    runbook = (ROOT / "docs/runbooks/lab-runner.md").read_text()
    assert "The nightly schedule (06:17 UTC) is off" in runbook
