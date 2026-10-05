"""`check --dry-run`: the qa-tester's view of the verdict must not move the run.

A recording check on a fail resets stages to pending; if the qa-tester ran that, the
orchestrator's merge of the qa-tester fragment would be refused and the rework loop would
stall. The dry run computes the same verdict and writes nothing.
"""

from __future__ import annotations

import json

from arc2 import check
from test_arc2_manifest import full_manifest, make_run


def test_dry_run_reports_the_verdict_and_writes_nothing(tmp_path, repo, capsys):
    m = full_manifest()
    m["validators"][0]["zero_hits_fails"] = False  # a sensor-gateway failure
    run = make_run(tmp_path, m)
    before = (run / "manifest.json").read_bytes()

    assert check.main(["check", str(run), "--json", "--dry-run", "--repo-root", str(repo)]) == 1
    qa = json.loads(capsys.readouterr().out)
    assert qa["result"] == "fail" and qa["rework_stage"] == "sensor-gateway"
    assert (run / "manifest.json").read_bytes() == before


def test_qa_tester_merge_succeeds_after_a_failing_dry_run(tmp_path, repo):
    run = make_run(tmp_path, full_manifest())
    (run / "05-sensor" / "validators" / "crit_lateral_movement.md").unlink()  # schema-valid failure
    check.check_run(run, repo, write=False)
    (run / "06-qa" / "fragment.json").write_text("{}")
    merged = check.merge_fragment(run, "qa-tester")
    assert merged["stages"]["qa-tester"]["state"] == "done"

    manifest, _ = check.check_run(run, repo)  # the orchestrator's recording check routes it
    assert manifest["qa"]["rework_stage"] == "sensor-gateway"
    assert manifest["qa"]["cycle"] == 1
    assert manifest["stages"]["sensor-gateway"]["state"] == "pending"
