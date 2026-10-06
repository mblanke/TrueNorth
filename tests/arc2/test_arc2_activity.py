"""Activities: theory and practical modules need no range; range modules need range evidence.

A theory-only run marks the range stages not_applicable, passes QA and packages. Turning one of
its modules into a range activity re-opens the gates it passed and fails until the range work
exists. A 0.1 manifest keeps its old meaning (every module is a range activity).
"""

from __future__ import annotations

import copy
import json

import pytest
from arc2 import check, lab_profile
from conftest import LAB_PROFILE, course_yaml
from test_arc2_cmi5 import fails, humans, packaged_run
from test_arc2_manifest import TS, checks, full_manifest, make_run

RANGE_KEYS = ("range", "injects", "validators", "scenario", "telemetry_xapi")


def not_applicable(reason: str = "no module is a range activity") -> dict:
    return {"state": "not_applicable", "attempts": 0, "started_at": TS, "finished_at": TS, "stop_reason": reason}


def theory_manifest(kind: str = "theory") -> dict:
    """The one-module course as a theory (or practical) activity: no range, no critical events,
    range stages not_applicable, nothing from them in the manifest."""
    m = full_manifest()
    for key in RANGE_KEYS:
        m.pop(key)
    m["critical_events"] = []
    activity = {"kind": kind, "no_range_reason": "Concepts and judgement; no running system is needed."}
    if kind == "practical":
        activity["practical_needs"] = ["supplied Windows event log export", "free EVTX viewer"]
    m["content"]["modules"][0]["activity"] = activity
    m["files"] = [f for f in m["files"] if f["stage"] != "sensor-gateway"]
    for stage in check.RANGE_STAGES:
        m["stages"][stage] = not_applicable()
    return m


def set_activity(run, kind: str) -> None:
    """Flip mod_001 in both places it is declared, as a reworked blueprint and content would."""
    outline = run / check.OUTLINE_REL
    text = outline.read_text()
    for old in check.ACTIVITY_KINDS:
        text = text.replace(f"activity: {old}", f"activity: {kind}")
    outline.write_text(text)
    m = check.load_manifest(run)
    act = {"kind": kind}
    if kind != check.RANGE:
        act["no_range_reason"] = "no running system needed"
    m["content"]["modules"][0]["activity"] = act
    check.save_manifest(run, m)


class TestTheoryRun:
    @pytest.mark.parametrize("kind", ["theory", "practical"])
    def test_passes_qa_and_packages_without_a_range(self, tmp_path, repo, kind):
        run = packaged_run(tmp_path, repo, theory_manifest(kind))
        m, findings = check.check_run(run, repo)
        assert fails(findings) == set(), [f for f in findings if f.severity == "fail"]
        assert m["qa"]["result"] == "pass"
        assert m["gates"]["preview"]["accepted_activities"] == {"mod_001": kind}
        assert m["cmi5"]["aus"][0]["module_id"] == "mod_001"
        assert not any(k in m for k in RANGE_KEYS)
        assert f"{kind} 1" in check.status_text(run, m)

    def test_the_course_file_carries_no_lab(self, tmp_path, repo):
        run = make_run(tmp_path, theory_manifest())
        _, findings = check.check_run(run, repo)
        assert "qa.course.module_lab" not in checks(findings)
        assert "qa.course.module_lab_unexpected" not in checks(findings)

    def test_lab_text_on_a_theory_module_is_ambiguous(self, tmp_path, repo):
        m = theory_manifest()
        run = make_run(tmp_path, m)
        (run / m["content"]["course_yaml"]).write_text(course_yaml())  # with a lab
        _, findings = check.check_run(run, repo)
        assert "qa.course.module_lab_unexpected" in fails(findings)

    def test_a_range_module_still_needs_its_lab_text(self, tmp_path, repo):
        m = full_manifest()
        run = make_run(tmp_path, m)
        (run / m["content"]["course_yaml"]).write_text(course_yaml(no_lab=frozenset({1})))
        _, findings = check.check_run(run, repo)
        assert "qa.course.module_lab" in fails(findings)


class TestNotApplicable:
    def test_skip_marks_the_range_stages(self, tmp_path):
        m = theory_manifest()
        for stage in check.RANGE_STAGES:
            m["stages"][stage] = {
                "state": "pending",
                "attempts": 0,
                "started_at": None,
                "finished_at": None,
                "stop_reason": None,
            }
        m["stages"]["artifact-creator"] = m["stages"]["range-engineer"].copy()
        m["stages"]["artifact-creator"]["state"] = "pending"
        m.pop("artifacts")
        run = make_run(tmp_path, m)
        out = check.skip_stage(run, "range-engineer", "theory course")
        assert out["stages"]["range-engineer"] == {**out["stages"]["range-engineer"], "state": "not_applicable"}
        assert out["stages"]["range-engineer"]["stop_reason"] == "theory course"
        with pytest.raises(check.ContractError, match="artifact-creator has not finished"):
            check.skip_stage(run, "sensor-gateway", "theory course")

    def test_skip_is_refused_for_a_run_with_a_range_module(self, tmp_path):
        m = full_manifest()
        for key in ("range", "injects"):
            m.pop(key)
        m["stages"]["range-engineer"] = {
            "state": "pending",
            "attempts": 0,
            "started_at": None,
            "finished_at": None,
            "stop_reason": None,
        }
        run = make_run(tmp_path, m)
        with pytest.raises(check.ContractError, match="mod_001 are range activities"):
            check.skip_stage(run, "range-engineer", "no lab")

    def test_skip_is_refused_without_a_reason_or_for_other_stages(self, tmp_path):
        run = make_run(tmp_path, theory_manifest())
        with pytest.raises(check.ContractError, match="--reason"):
            check.skip_stage(run, "range-engineer", " ")
        with pytest.raises(check.ContractError, match="only range-engineer, sensor-gateway"):
            check.skip_stage(run, "artifact-creator", "nothing to make")

    def test_skip_is_refused_over_existing_output(self, tmp_path):
        m = theory_manifest()
        m["stages"]["range-engineer"] = m["stages"]["code-generator"].copy()
        m["range"] = full_manifest()["range"]
        run = make_run(tmp_path, m)
        with pytest.raises(check.ContractError, match="already wrote range"):
            check.skip_stage(run, "range-engineer", "theory")

    def test_skip_is_refused_when_the_outline_declares_nothing(self, tmp_path):
        run = make_run(tmp_path, theory_manifest())
        (run / check.OUTLINE_REL).write_text("modules:\n- id: mod_001\n  title: x\n")
        with pytest.raises(check.ContractError, match="never skipped by omission"):
            check.skip_stage(run, "range-engineer", "theory")

    def test_not_applicable_with_output_is_a_finding(self, tmp_path, repo):
        m = theory_manifest()
        m["validators"] = full_manifest()["validators"]
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "stage.not_applicable_has_output" in fails(findings)

    def test_not_applicable_without_a_reason_fails_the_schema(self, tmp_path, repo):
        m = theory_manifest()
        m["stages"]["range-engineer"]["stop_reason"] = None
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "schema.valid" in fails(findings)

    def test_only_range_stages_can_be_not_applicable(self, tmp_path, repo):
        m = theory_manifest()
        m["stages"]["artifact-creator"] = not_applicable()
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "stage.not_applicable_invalid" in fails(findings)

    def test_a_non_range_activity_needs_its_reason(self, tmp_path, repo):
        m = theory_manifest()
        del m["content"]["modules"][0]["activity"]["no_range_reason"]
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "schema.valid" in fails(findings)


class TestTheoryBecomesRange:
    def test_flipping_an_accepted_theory_module_reopens_gates_and_needs_range_work(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo, theory_manifest())
        set_activity(run, "range")
        _, findings = check.check_run(run, repo, write=False)
        found = fails(findings)
        assert {"gate.outline_unchanged", "gate.activity_changed", "stage.not_applicable_invalid"} <= found
        assert "range.evidence_critical_events" in found
        changed = [f.message for f in findings if f.check == "gate.activity_changed"]
        assert any("mod_001 theory → range" in msg for msg in changed)
        assert set(check.gate_verify(run)) == {"outline", "preview"}

    def test_a_new_range_template_must_validate(self, tmp_path, repo):
        m = full_manifest()
        m["range"].update({"mode": "new", "path": "03-range/range.tf"})
        m["files"].append(
            {
                "path": "03-range/range.tf",
                "stage": "range-engineer",
                "kind": "range",
                "objective_ids": ["M01-O01"],
                "sha256": None,
            }
        )
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "range.evidence_terraform" in fails(findings)

    def test_an_uninstalled_terraform_is_a_human_action_not_a_silent_pass(self, tmp_path, repo):
        m = full_manifest()
        m["range"].update({"mode": "new", "path": "03-range/range.tf"})
        m["range"]["terraform_validate"]["status"] = "not_installed"
        manifest, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "range.evidence_terraform" in humans(findings)
        assert any(a["id"].startswith("range.evidence_terraform") for a in manifest["human_actions"])

    def test_a_range_run_needs_injects_noise_and_validators(self, tmp_path, repo):
        m = full_manifest()
        m["injects"]["items"] = []
        m["injects"]["noise_floor"] = m["injects"]["noise_floor"][:1]
        m["validators"] = []
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert {"range.evidence_injects", "range.evidence_validators"} <= fails(findings)

    def test_a_range_run_needs_a_lab_profile(self, tmp_path, repo):
        m = full_manifest()
        del m["range"]["lab_profile"]
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "range.lab_profile" in fails(findings)


class TestActivityDeclarations:
    def test_content_must_agree_with_the_outline(self, tmp_path, repo):
        m = theory_manifest()
        run = make_run(tmp_path, m)
        m = check.load_manifest(run)
        m["content"]["modules"][0]["activity"] = {"kind": "practical", "no_range_reason": "logs"}
        check.save_manifest(run, m)
        _, findings = check.check_run(run, repo)
        assert "content.activity_mismatch" in fails(findings)

    def test_a_02_module_without_an_activity_fails(self, tmp_path, repo):
        m = full_manifest()
        del m["content"]["modules"][0]["activity"]
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "content.activity_missing" in fails(findings)

    def test_an_outline_without_activities_fails(self, tmp_path, repo):
        run = make_run(tmp_path)
        (run / check.OUTLINE_REL).write_text("modules:\n- id: mod_001\n  title: Lateral movement\n")
        m = check.load_manifest(run)
        m["gates"]["outline"]["accepted_sha256"] = check.outline_digest(run, m)
        check.save_manifest(run, m)
        _, findings = check.check_run(run, repo)
        assert "outline.activity_missing" in fails(findings)


class TestVersions:
    def test_a_legacy_manifest_is_all_range_and_needs_no_activity(self, tmp_path, repo):
        m = full_manifest()
        m["schema_version"] = check.LEGACY_SCHEMA_VERSION
        del m["content"]["modules"][0]["activity"]
        del m["range"]["lab_profile"]
        run = make_run(tmp_path, m)
        (run / check.OUTLINE_REL).write_text("modules:\n- id: mod_001\n  title: Lateral movement\n")
        m = check.load_manifest(run)
        m["gates"]["outline"]["accepted_sha256"] = check.outline_digest(run, m)
        check.save_manifest(run, m)
        manifest, findings = check.check_run(run, repo)
        assert fails(findings) == set(), [f for f in findings if f.severity == "fail"]
        assert check.run_activities(run, manifest) == {"mod_001": "range"}
        with pytest.raises(check.ContractError, match="no activities"):
            check.skip_stage(run, "range-engineer", "x")

    def test_an_unknown_version_is_rejected(self, tmp_path, repo):
        m = full_manifest()
        m["schema_version"] = "arc2/manifest/9.9"
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "schema.valid" in fails(findings)

    def test_init_writes_the_current_version(self, tmp_path):
        req = tmp_path / "req.txt"
        req.write_text("an 8h theory course")
        m = check.init_run(tmp_path / "run", "arc2-x", req.read_text(), tmp_path, False)
        assert m["schema_version"] == check.SCHEMA_VERSION == "arc2/manifest/0.2"


class TestLabProfile:
    def profile(self):
        import yaml

        return yaml.safe_load(LAB_PROFILE)

    def test_the_fixture_profile_is_clean(self, repo):
        assert (
            lab_profile.findings(self.profile(), range_modules={"mod_001"}, catalogue=lab_profile.catalogue_ids(repo))
            == []
        )

    def test_images_must_be_enabled_catalogue_ids(self, repo):
        p = self.profile()
        p["nodes"][0]["catalogue_id"] = "made-up-template"
        rules = lab_profile.findings(p, range_modules={"mod_001"}, catalogue=lab_profile.catalogue_ids(repo))
        assert ("lab.catalogue", "node analyst uses made-up-template, which is not in the image catalogue") in rules
        p["nodes"][0]["catalogue_id"] = "win-xp-sp3"
        rules = lab_profile.findings(p, range_modules={"mod_001"}, catalogue=lab_profile.catalogue_ids(repo))
        assert any(r == "lab.catalogue" and "disabled" in msg for r, msg in rules)

    def test_size_is_bounded_only_by_the_declared_limits(self):
        p = self.profile()
        node = p["nodes"][0]
        p["nodes"] = [dict(node, name=f"n{i}") for i in range(8)]
        p["health_checks"] = [{"node": f"n{i}", "kind": "tools", "timeout_s": 60} for i in range(8)]
        p["access"] = [{"node": "n0", "kind": "console"}]
        p["evidence_checks"] = [{"id": "e", "node": "n0", "description": "x"}]
        rules = {r for r, _ in lab_profile.findings(p, range_modules={"mod_001"}, catalogue=None)}
        assert rules == {"lab.size"}  # 8 nodes against the fixture's max_vms 1
        n = len(p["nodes"])
        p["limits"] = {"max_vms": n, "vcpu_total": 2 * n, "ram_mb_total": 4096 * n, "disk_gb_total": 40 * n}
        assert lab_profile.findings(p, range_modules={"mod_001"}, catalogue=None) == []  # no fixed VM cap

    def test_references_and_coverage(self):
        p = self.profile()
        p["access"] = [{"node": "ghost", "kind": "console"}]
        p["module_ids"] = ["mod_001", "mod_002"]
        msgs = [m for _, m in lab_profile.findings(p, range_modules={"mod_001", "mod_003"}, catalogue=None)]
        assert "access names unknown node ghost" in msgs
        assert "range module mod_003 has no lab in this profile" in msgs
        assert "mod_002 is not a range module; theory and practical modules get no VM" in msgs

    def test_schema_errors_are_reported(self):
        p = self.profile()
        p["reset"] = {"mode": "pray"}
        rules = lab_profile.findings(p, range_modules={"mod_001"}, catalogue=None)
        assert rules and all(r == "lab.schema" for r, _ in rules)

    def test_a_bad_profile_fails_the_run(self, tmp_path, repo):
        m = full_manifest()
        run = make_run(tmp_path, m)
        bad = copy.deepcopy(self.profile())
        bad["nodes"][0]["catalogue_id"] = "nope"
        import yaml

        (run / m["range"]["lab_profile"]).write_text(yaml.safe_dump(bad))
        _, findings = check.check_run(run, repo)
        assert "range.lab.catalogue" in fails(findings)


def test_cli_skip(tmp_path, capsys):
    m = theory_manifest()
    m["stages"]["sensor-gateway"]["state"] = "pending"
    m["stages"]["sensor-gateway"]["stop_reason"] = None
    run = make_run(tmp_path, m)
    assert check.main(["skip", str(run), "sensor-gateway", "--reason", "theory course"]) == 0
    assert json.loads((run / "manifest.json").read_text())["stages"]["sensor-gateway"]["state"] == "not_applicable"
