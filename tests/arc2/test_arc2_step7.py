"""The adversarial review of PR 3 found two ways a run could never finish; these pin the fixes.

- A failure found by the post-package check (step 7) could not be reworked: package-builder
  merged only after a QA pass, and `check` kept judging the stale `07-bundle/cmi5/`.
- A real sha256 in a stage fragment matched `qa.defang`'s base64-run pattern, so every run that
  recorded digests failed QA.
"""

from __future__ import annotations

import json

import pytest
from arc2 import check, qa
from test_arc2_cmi5 import fails, packaged_run
from test_arc2_manifest import full_manifest, make_run, owners


def test_sha256_digests_are_not_payloads(tmp_path, repo):
    m = full_manifest()
    run = make_run(tmp_path, m)
    digest = check.sha256_file(run / m["injects"]["timeline"])
    (run / "03-range" / "fragment.json").write_text(
        json.dumps({"files": [{"path": "03-range/timeline.yaml", "sha256": digest}]})
    )
    assert "qa.defang" not in {f["check"] for f in qa.check_run(run, m, repo)}
    (run / "03-range" / "notes.md").write_text(
        "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODk=\n"
    )
    assert "range-engineer" in owners([check.Finding(**f) for f in qa.check_run(run, m, repo)], "qa.defang")


def test_config_drift_is_caught_at_qa_before_packaging(tmp_path, repo):
    m = full_manifest()
    run = make_run(tmp_path, m)
    cfg = run / m["content"]["modules"][0]["config"]
    data = json.loads(cfg.read_text())
    data["quiz"]["questions"] = data["quiz"]["questions"][:4]  # manifest says 5
    cfg.write_text(json.dumps(data))
    manifest, findings = check.check_run(run, repo)
    assert owners(findings, "cmi5.config_matches_manifest") == {"code-generator"}
    assert manifest["qa"]["rework_stage"] == "code-generator"


def test_framework_token_in_a_page_is_caught_at_qa(tmp_path, repo):
    m = full_manifest()
    run = make_run(tmp_path, m)
    (run / m["content"]["modules"][0]["pages"][0]).write_text("<p>Maps to DCWF task T0023.</p>")
    _, findings = check.check_run(run, repo)
    assert owners(findings, "cmi5.no_framework_tokens") == {"code-generator"}


def test_stale_package_is_not_judged_while_stage_7_is_pending(tmp_path, repo):
    run = packaged_run(tmp_path, repo)
    js = run / "07-bundle" / "cmi5" / "cmi5.js"
    js.write_text(js.read_text() + "\n// stale\n")
    manifest = check.load_manifest(run)
    manifest["stages"]["package-builder"]["state"] = "pending"
    check.save_manifest(run, manifest)
    _, findings = check.check_run(run, repo)
    assert not any(f.check.startswith("cmi5.au_layout") for f in findings)


def test_a_package_builder_failure_reruns_stage_7(tmp_path, repo):
    run = packaged_run(tmp_path, repo)
    js = run / "07-bundle" / "cmi5" / "cmi5.js"
    original = js.read_text()
    js.write_text(original + "\n// tampered\n")
    manifest, findings = check.check_run(run, repo)
    assert "cmi5.au_layout" in fails(findings)
    assert manifest["qa"]["rework_stage"] == "package-builder"
    assert manifest["gates"]["preview"]["state"] == "accepted"  # the accepted inputs did not change
    assert manifest["stages"]["package-builder"]["state"] == "pending"

    js.write_text(original)  # package-builder re-runs `arc2.cmi5 package`
    merged = check.merge_fragment(run, "package-builder")  # allowed although QA is `fail`
    assert merged["stages"]["package-builder"]["state"] == "done"
    manifest, findings = check.check_run(run, repo)
    assert fails(findings) == set()
    assert manifest["qa"]["result"] == "pass"


def test_package_builder_still_needs_qa_for_other_failures(tmp_path, repo):
    run = packaged_run(tmp_path, repo)
    manifest = check.load_manifest(run)
    manifest["stages"]["package-builder"]["state"] = "pending"
    manifest["qa"].update({"result": "fail", "rework_stage": "code-generator"})
    check.save_manifest(run, manifest)
    with pytest.raises(check.ContractError, match="QA has not passed"):
        check.merge_fragment(run, "package-builder")
