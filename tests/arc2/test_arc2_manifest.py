"""Contract tests for tools/arc2: schema, fragment ownership, the golden thread, gates, routing.

Fixtures are inline: a tiny crosswalk with one claimed PO, and a complete manifest for a
one-module course. Every negative test breaks exactly one thing and asserts the finding
lands on the stage that owns it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from arc2 import check

TS = "2026-09-24T21:00:00Z"
SLUG = "arc2-adlm"

CROSSWALK = """qsp_code,nqual,tier,po_id,po_title,eos,conditions,critical_events,assessment_type,duration_min,pass_standard,deliverable,environment,target_role,nice_dcwf_task,component_version,scenario_count,build_hours,status
ALJQ,ALJQ,core,PO_007,Analyze Malicious Activity in Network Traffic,007.01,pcap,scanning;exfiltration;lateral_movement,PC practical,240,P/F,report,COTE,Cyber Defense Analyst,T0023,SP800-181r1,4,200,todo
ALJQ,ALJQ,core,PO_009,Security Monitoring,009.01,siem,alert_triage;escalation,PC practical,180,P/F,report,COTE,Cyber Defense Analyst,T0023,SP800-181r1,2,80,todo
ALJQ,ALJQ,core,PO_010,Example,010.01,siem,example_event,PC practical,60,P/F,report,COTE,Analyst,T0023,SP800-181r1,1,10,example
TEMP64,TEMP64,core,PO_001-003,Gain Access,001,lab,initial_access,PC practical,240,P/F,report,COTE,Operator,T0028,SP800-181r1,3,100,offensive_author
"""

CLAIMING_COURSE = """course_code: C204
title: Security Monitoring
modules:
- ordinal: 1
  title: SIEM
  po:
    qsp_code: ALJQ
    po_code: PO_009
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / check.CROSSWALK_REL).parent.mkdir(parents=True)
    (root / check.CROSSWALK_REL).write_text(CROSSWALK)
    (root / check.COURSES_REL).mkdir(parents=True)
    (root / check.COURSES_REL / "c204.yaml").write_text(CLAIMING_COURSE)
    return root


def _done() -> dict:
    return {"state": "done", "attempts": 1, "started_at": TS, "finished_at": TS, "stop_reason": None}


def _pending() -> dict:
    return {"state": "pending", "attempts": 0, "started_at": None, "finished_at": None, "stop_reason": None}


def full_manifest() -> dict:
    """A complete, consistent manifest for a one-module course with stages 1–5 done."""
    stages = {name: _done() for name in check.STAGE_ORDER[:5]}
    stages["qa-tester"] = _pending()
    stages["package-builder"] = _pending()
    crit = {
        "id": "crit_ce01",
        "kind": "crit",
        "path": "05-sensor/validators/crit_lateral_movement.md",
        "objective_ids": ["M01-O01"],
        "critical_event_id": "CE-01",
        "opensearch_query": True,
        "zero_hits_fails": True,
        "penalises_noise": True,
        "must_pass": True,
        "summative": False,
    }
    ack = {
        "id": "ack_summative",
        "kind": "manual_ack",
        "path": "05-sensor/validators/manual_ack_summative.md",
        "objective_ids": ["M01-O01"],
        "critical_event_id": None,
        "opensearch_query": False,
        "zero_hits_fails": False,
        "penalises_noise": False,
        "must_pass": True,
        "summative": True,
    }
    return {
        "schema_version": check.SCHEMA_VERSION,
        "run_id": "0123456789ab",
        "slug": SLUG,
        "provenance": {
            "git_head": "abc1234",
            "git_dirty": False,
            "enclave": False,
            "created_at": TS,
            "tool_version": "0.1.0",
            "inputs": {"request.txt": "0" * 64},
        },
        "stages": stages,
        "gates": {
            "outline": {"state": "accepted", "ts": TS, "accepted_sha256": None, "feedback": []},
            "preview": {"state": "n/a", "ts": None, "accepted_sha256": None, "rework_count": 0, "feedback": []},
        },
        "request": {
            "text": "SOC tier-2, 8h, intermediate, instructor-led, detecting AD lateral movement",
            "audience": "SOC tier-2 analysts",
            "duration_hours": 8,
            "difficulty": "intermediate",
            "delivery": "instructor-led",
            "topic": "detecting AD lateral movement",
            "constraints": ["no live malware"],
        },
        "course": {
            "code": "ARC2-ADLM",
            "title": "Detecting AD Lateral Movement",
            "summary": "Detect and reconstruct lateral movement from authentication and SMB telemetry.",
            "programme": "cyber-operator",
            "qsp_code": "QSP-TODO",
            "po": None,
            "po_candidates": [],
            "dp_order": 1,
            "provenance": "unsourced",
            "status": "proposed",
            "duration_hours": 8,
        },
        "objectives": [
            {
                "id": "M01-O01",
                "module_id": "mod_001",
                "text": "Detect lateral movement from authentication telemetry",
                "source": "objective",
                "po": None,
            }
        ],
        "critical_events": [
            {
                "id": "CE-01",
                "text": "lateral_movement",
                "source": "objective",
                "objective_ids": ["M01-O01"],
                "crosswalk_ref": None,
            }
        ],
        "content": {
            "course_yaml": "02-content/arc2-adlm.yaml",
            "modules": [
                {
                    "id": "mod_001",
                    "ordinal": 1,
                    "title": "Lateral movement",
                    "objective_ids": ["M01-O01"],
                    "is_required": True,
                    "pass_threshold": 70,
                    "quiz": {"pass_threshold": 70, "question_count": 5},
                    "config": "02-content/mod_001/course-config.json",
                    "pages": ["02-content/mod_001/content/page-01.html"],
                }
            ],
        },
        "range": {
            "mode": "reuse",
            "name": "soc-training",
            "path": "content/ranges/soc-training",
            "templates": [],
            "port_group": None,
            "terraform_validate": {"status": "not_run", "output": ""},
        },
        "injects": {
            "timeline": "03-range/timeline.yaml",
            "items": [
                {
                    "id": "inj-01",
                    "t": "00:10",
                    "objective_id": "M01-O01",
                    "technique": "T1021.002",
                    "critical": True,
                    "critical_event_id": "CE-01",
                    "description": "Pre-captured SMB admin-share session from a workstation to the file server",
                    "author_required": False,
                }
            ],
            "noise_floor": [
                {
                    "id": "n1",
                    "description": "Backup agent SMB traffic",
                    "why_plausible": "Runs nightly on every server",
                },
                {"id": "n2", "description": "Helpdesk RDP session", "why_plausible": "Ticketed remote support"},
            ],
        },
        "artifacts": {
            "rubric": "04-artifacts/rubric.md",
            "deliverable_template": "04-artifacts/deliverable.md",
            "variant_b": "04-artifacts/variant_B/timeline.yaml",
            "xapi_json": "04-artifacts/xapi.json",
            "instructor_dir": "04-artifacts/instructor",
            "media": [],
        },
        "validators": [crit, ack],
        "scenario": {"path": "05-sensor/scenario.yaml", "engine_validate": {"status": "not_run", "errors": []}},
        "telemetry_xapi": [
            {
                "validator_id": "crit_ce01",
                "verb_on_pass": "passed",
                "verb_on_fail": "failed",
                "auto_scored": True,
                "context_template": "cmi5",
            }
        ],
        "files": [
            {
                "path": "02-content/arc2-adlm.yaml",
                "stage": "code-generator",
                "kind": "content",
                "objective_ids": ["M01-O01"],
                "sha256": None,
            },
            {
                "path": "05-sensor/validators/crit_lateral_movement.md",
                "stage": "sensor-gateway",
                "kind": "validator",
                "objective_ids": ["M01-O01"],
                "sha256": None,
            },
        ],
        "human_actions": [],
    }


def make_run(tmp_path: Path, manifest: dict | None = None) -> Path:
    run = tmp_path / "build" / "arc2" / SLUG
    for stage in check.STAGES:
        (run / stage.dir).mkdir(parents=True, exist_ok=True)
    outline = run / "01-blueprint" / "outline.yaml"
    outline.write_text("modules:\n- id: mod_001\n  title: Lateral movement\n")
    (run / "02-content" / "arc2-adlm.yaml").write_text("course_code: ARC2-ADLM\n")
    manifest = manifest or full_manifest()
    if manifest["gates"]["outline"]["state"] == "accepted":
        manifest["gates"]["outline"]["accepted_sha256"] = check.sha256_file(outline)
    check.save_manifest(run, manifest)
    return run


def checks(findings: list[check.Finding], severity: str | None = None) -> set[str]:
    return {f.check for f in findings if severity is None or f.severity == severity}


def owners(findings: list[check.Finding], name: str) -> set[str]:
    return {f.owner_stage for f in findings if f.check == name}


# ── the happy path ─────────────────────────────────────────────────────


class TestValidRun:
    def test_valid_manifest_passes_and_opens_preview_gate(self, tmp_path, repo):
        run = make_run(tmp_path)
        manifest, findings = check.check_run(run, repo)
        assert checks(findings, "fail") == set()
        assert manifest["qa"]["result"] == "pass"
        assert manifest["qa"]["rework_stage"] is None
        assert manifest["gates"]["preview"]["state"] == "pending"

    def test_status_names_the_resume_commands(self, tmp_path, repo):
        run = make_run(tmp_path)
        manifest, _ = check.check_run(run, repo)
        text = check.status_text(run, manifest)
        assert f"/arc2 --resume {SLUG} accept" in text
        assert "preview PENDING" in text
        assert "CE covered 1/1" in text

    def test_schema_file_is_draft7_and_strict(self):
        schema = check.load_schema()
        assert schema["$schema"].startswith("http://json-schema.org/draft-07/")
        assert schema["additionalProperties"] is False
        assert set(check.KEY_OWNER) <= set(schema["properties"])


# ── nothing passes vacuously ───────────────────────────────────────────


class TestVacuity:
    def test_empty_objectives_fail(self, tmp_path, repo):
        m = full_manifest()
        m["objectives"] = []
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "schema.valid" in checks(findings, "fail")
        assert owners(findings, "schema.valid") == {"content-architect"}

    def test_file_with_no_objectives_fails_on_its_own_stage(self, tmp_path, repo):
        m = full_manifest()
        m["files"][0]["objective_ids"] = []
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "code-generator" in owners(findings, "schema.valid") | owners(findings, "trace.file_objectives")

    def test_runtime_kind_only_for_allowlisted_basenames(self, tmp_path, repo):
        m = full_manifest()
        m["files"].append(
            {
                "path": "07-bundle/cmi5/evil.js",
                "stage": "package-builder",
                "kind": "runtime",
                "objective_ids": [],
                "sha256": None,
            }
        )
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "trace.runtime_allowlist") == {"package-builder"}
        m["files"][-1]["path"] = "07-bundle/cmi5/cmi5.js"
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "trace.runtime_allowlist" not in checks(findings)

    def test_stage_that_has_not_run_is_a_finding(self, tmp_path, repo):
        m = full_manifest()
        m["stages"]["range-engineer"] = _pending()
        manifest, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "stage.not_done") == {"range-engineer"}
        assert manifest["qa"]["result"] == "fail"
        assert manifest["qa"]["cycle"] == 0, "a check on a half-built run is not a QA cycle"
        assert manifest["qa"]["rework_stage"] == "range-engineer"

    def test_unobservable_objective_fails(self, tmp_path, repo):
        m = full_manifest()
        m["objectives"][0]["text"] = "Understand lateral movement techniques"
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "objective.observable_verb") == {"content-architect"}


# ── the golden thread ──────────────────────────────────────────────────


class TestGoldenThread:
    def test_critical_event_without_must_pass_validator_fails(self, tmp_path, repo):
        m = full_manifest()
        m["validators"][0]["must_pass"] = False
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "trace.ce_has_crit_validator") == {"sensor-gateway"}

    def test_zero_hits_fails_false_fails(self, tmp_path, repo):
        m = full_manifest()
        m["validators"][0]["zero_hits_fails"] = False
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "sensor-gateway" in owners(findings, "schema.valid")
        assert "trace.ce_has_crit_validator" in checks(findings, "fail")

    def test_objective_needs_content_and_validator(self, tmp_path, repo):
        m = full_manifest()
        m["objectives"].append(
            {
                "id": "M01-O02",
                "module_id": "mod_001",
                "text": "Reconstruct the attacker path from SMB logs",
                "source": "objective",
                "po": None,
            }
        )
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "trace.objective_has_content") == {"code-generator"}
        assert owners(findings, "trace.objective_has_validator") == {"sensor-gateway"}

    def test_critical_event_needs_a_critical_inject(self, tmp_path, repo):
        m = full_manifest()
        m["injects"]["items"][0]["critical"] = False
        m["injects"]["items"][0]["critical_event_id"] = None
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "trace.ce_has_inject") == {"range-engineer"}

    def test_summative_is_never_auto_scored(self, tmp_path, repo):
        m = full_manifest()
        m["telemetry_xapi"].append(
            {
                "validator_id": "ack_summative",
                "verb_on_pass": "passed",
                "verb_on_fail": "failed",
                "auto_scored": True,
                "context_template": "cmi5",
            }
        )
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "trace.summative_not_auto_scored") == {"sensor-gateway"}


# ── PO binding against the crosswalk ───────────────────────────────────


class TestPoBinding:
    def test_unknown_po_fails(self, tmp_path, repo):
        m = full_manifest()
        m["course"]["po"] = {"qsp_code": "ALJQ", "po_code": "PO_099"}
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "po.bound_exists") == {"content-architect"}

    def test_claimed_po_is_a_human_finding_not_a_fail(self, tmp_path, repo):
        m = full_manifest()
        m["course"]["po"] = {"qsp_code": "ALJQ", "po_code": "PO_009"}
        manifest, findings = check.check_run(make_run(tmp_path, m), repo)
        human = [f for f in findings if f.check == "po.bound_claimed"]
        assert human and human[0].severity == "human" and "c204.yaml" in human[0].message
        assert manifest["qa"]["result"] == "pass"
        actions = [a for a in manifest["human_actions"] if a["category"] == "standards"]
        assert actions and actions[0]["blocks_promotion"] is True

    def test_offensive_author_po_cannot_be_bound(self, tmp_path, repo):
        m = full_manifest()
        m["objectives"][0]["po"] = {"qsp_code": "TEMP64", "po_code": "PO_001-003"}
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "po.bound_status_allowed") == {"content-architect"}

    def test_candidate_raises_a_standards_action(self, tmp_path, repo):
        m = full_manifest()
        m["course"]["po_candidates"] = [
            {"qsp_code": "ALJQ", "po_code": "PO_007", "status": "todo", "reason": "critical events match"}
        ]
        manifest, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "po.candidate_needs_standards" in checks(findings, "human")
        assert manifest["qa"]["result"] == "pass"

    def test_critical_event_text_must_be_verbatim_from_the_row(self, tmp_path, repo):
        m = full_manifest()
        m["critical_events"][0].update(
            {
                "source": "crosswalk",
                "crosswalk_ref": {"qsp_code": "ALJQ", "po_code": "PO_007"},
                "text": "lateral movement",
            }
        )
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "ce.text_verbatim") == {"content-architect"}
        m["critical_events"][0]["text"] = "lateral_movement"
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert "ce.text_verbatim" not in checks(findings)

    def test_missing_crosswalk_is_a_human_finding(self, tmp_path):
        _, findings = check.check_run(make_run(tmp_path), tmp_path / "nowhere")
        assert "po.crosswalk_readable" in checks(findings, "human")


# ── merge: ownership and STOP ──────────────────────────────────────────


def init_run(tmp_path: Path, repo: Path) -> Path:
    req = tmp_path / "request.txt"
    req.write_text("SOC tier-2 analysts, 8h, intermediate, instructor-led, detecting AD lateral movement")
    run = tmp_path / "build" / "arc2" / SLUG
    check.init_run(run, SLUG, req.read_text(), repo, enclave=False)
    return run


def architect_fragment() -> dict:
    m = full_manifest()
    return {k: m[k] for k in ("request", "course", "objectives", "critical_events")}


class TestMerge:
    def test_init_writes_a_valid_skeleton(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        m = check.load_manifest(run)
        assert check.schema_errors(m) == []
        assert set(m["provenance"]["inputs"]) == {"request.txt", "crosswalk.csv"}
        assert all(s["state"] == "pending" for s in m["stages"].values())

    def test_unowned_key_is_rejected(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        (run / "02-content" / "fragment.json").write_text(
            json.dumps({"content": full_manifest()["content"], "cmi5": {}})
        )
        with pytest.raises(check.ContractError, match=r"cmi5 \(owned by package-builder\)"):
            check.merge_fragment(run, "code-generator")
        assert "content" not in check.load_manifest(run)

    def test_shared_entry_must_carry_its_own_stage(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        frag = {
            "content": full_manifest()["content"],
            "files": [
                {
                    "path": "x.yaml",
                    "stage": "sensor-gateway",
                    "kind": "content",
                    "objective_ids": ["M01-O01"],
                    "sha256": None,
                }
            ],
        }
        (run / "02-content" / "fragment.json").write_text(json.dumps(frag))
        with pytest.raises(check.ContractError, match="stage='code-generator'"):
            check.merge_fragment(run, "code-generator")

    def test_invalid_fragment_is_rejected_before_writing(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        frag = architect_fragment()
        frag["objectives"] = []
        (run / "01-blueprint" / "fragment.json").write_text(json.dumps(frag))
        with pytest.raises(check.ContractError, match="objectives"):
            check.merge_fragment(run, "content-architect")
        assert check.load_manifest(run)["stages"]["content-architect"]["state"] == "pending"

    def test_stop_marks_the_stage_failed(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        (run / "01-blueprint" / "fragment.json").write_text(json.dumps({"stop": "audience missing from the request"}))
        with pytest.raises(check.ContractError, match="^STOP from content-architect: audience"):
            check.merge_fragment(run, "content-architect")
        st = check.load_manifest(run)["stages"]["content-architect"]
        assert st["state"] == "failed" and st["stop_reason"].startswith("audience")

    def test_valid_fragment_marks_done_and_opens_the_outline_gate(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        (run / "01-blueprint" / "outline.yaml").write_text("modules: []\n")
        (run / "01-blueprint" / "fragment.json").write_text(json.dumps(architect_fragment()))
        m = check.merge_fragment(run, "content-architect")
        assert m["stages"]["content-architect"]["state"] == "done"
        assert m["gates"]["outline"]["state"] == "pending"
        assert m["course"]["code"] == "ARC2-ADLM"

    def test_rerun_replaces_only_its_own_shared_entries(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        m = full_manifest()
        m["stages"] = {k: _pending() for k in check.STAGE_ORDER}
        m["gates"]["outline"]["state"] = "n/a"
        check.save_manifest(run, m)
        frag = {
            "content": m["content"],
            "files": [
                {
                    "path": "02-content/new.yaml",
                    "stage": "code-generator",
                    "kind": "content",
                    "objective_ids": ["M01-O01"],
                    "sha256": None,
                }
            ],
        }
        (run / "02-content" / "fragment.json").write_text(json.dumps(frag))
        merged = check.merge_fragment(run, "code-generator")
        paths = {f["path"] for f in merged["files"]}
        assert paths == {"02-content/new.yaml", "05-sensor/validators/crit_lateral_movement.md"}


# ── QA routing and the cycle budget ────────────────────────────────────


class TestRouting:
    def test_rework_routes_to_the_earliest_owner(self, tmp_path, repo):
        m = full_manifest()
        m["validators"][0]["zero_hits_fails"] = False  # sensor-gateway
        m["content"]["modules"][0]["objective_ids"] = ["M01-O09"]  # code-generator
        manifest, findings = check.check_run(make_run(tmp_path, m), repo)
        assert check.route(findings) == "code-generator"
        assert manifest["qa"]["rework_stage"] == "code-generator"
        assert manifest["qa"]["cycle"] == 1
        states = {k: v["state"] for k, v in manifest["stages"].items()}
        assert states["content-architect"] == "done"
        assert {states[k] for k in ("code-generator", "range-engineer", "sensor-gateway", "qa-tester")} == {"pending"}

    def test_three_failures_end_in_human_takeover(self, tmp_path, repo):
        m = full_manifest()
        m["validators"][0]["zero_hits_fails"] = False
        run = make_run(tmp_path, m)
        codes = []
        for _ in range(3):
            codes.append(check.main(["check", str(run), "--repo-root", str(repo)]))
            # The orchestrator re-runs the reset stages; here they "finish" without fixing anything.
            current = check.load_manifest(run)
            for name in check.AGENT_STAGES:
                current["stages"][name] = _done()
            check.save_manifest(run, current)
        assert codes == [1, 1, 3]
        assert check.load_manifest(run)["qa"]["result"] == "human_takeover"

    def test_orchestrator_only_failures_have_no_rework_stage(self, tmp_path, repo):
        m = full_manifest()
        m["stages"]["package-builder"] = _done()
        m["bundle"] = {
            "promote_md": "07-bundle/PROMOTE.md",
            "destinations": [
                {
                    "source": "02-content/arc2-adlm.yaml",
                    "destination": "content/courses/arc2-adlm.yaml",
                    "kind": "course",
                }
            ],
        }
        m["cmi5"] = {
            "course_id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm",
            "id_scheme": "arc2-provisional",
            "package_root": "07-bundle/cmi5",
            "base_url": None,
            "lang": "en-CA",
            "objective_iris": {"M01-O01": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm/obj/M01-O01"},
            "aus": [
                {
                    "id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm/au/mod_001",
                    "module_id": "mod_001",
                    "block_id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm/block/mod_001",
                    "objective_ids": ["M01-O01"],
                    "moveOn": "Passed",
                    "masteryScore": 0.7,
                    "launchMethod": "AnyWindow",
                    "url": "mod_001/index.html",
                    "config": "mod_001/course-config.json",
                }
            ],
            "xsd": {"status": "not_run", "errors": []},
        }
        manifest, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "gate.preview_accepted") == {"orchestrator"}
        assert manifest["qa"]["result"] == "fail" and manifest["qa"]["rework_stage"] is None


# ── gates ──────────────────────────────────────────────────────────────


class TestGates:
    def test_outline_accept_records_sha_and_feedback_reopens(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        outline = run / "01-blueprint" / "outline.yaml"
        outline.write_text("modules: []\n")
        (run / "01-blueprint" / "fragment.json").write_text(json.dumps(architect_fragment()))
        check.merge_fragment(run, "content-architect")
        m = check.gate(run, "outline", "accept")
        assert m["gates"]["outline"]["state"] == "accepted"
        assert m["gates"]["outline"]["accepted_sha256"] == check.sha256_file(outline)

        outline.write_text("modules: [changed]\n")
        assert check.gate_verify(run) == ["outline"]
        assert check.load_manifest(run)["gates"]["outline"]["state"] == "pending"

        m = check.gate(run, "outline", "feedback", text="cut module 3, keep it to 6h")
        assert m["gates"]["outline"]["state"] == "feedback"
        assert m["gates"]["outline"]["feedback"][0]["round"] == 1
        assert m["stages"]["content-architect"]["state"] == "pending"

    def test_outline_gate_needs_a_finished_architect(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        with pytest.raises(check.ContractError, match="has not finished"):
            check.gate(run, "outline", "accept")

    def test_preview_accept_needs_qa_pass_and_feedback_routes(self, tmp_path, repo):
        run = make_run(tmp_path)
        with pytest.raises(check.ContractError, match="QA has not passed"):
            check.gate(run, "preview", "accept")
        check.check_run(run, repo)
        m = check.gate(run, "preview", "accept")
        assert m["gates"]["preview"]["state"] == "accepted"
        assert m["gates"]["preview"]["accepted_sha256"] == check.sha256_tree(run, check.PREVIEW_DIRS)

        with pytest.raises(check.ContractError, match="--route"):
            check.gate(run, "preview", "feedback", text="shorten module 2")
        m = check.gate(run, "preview", "feedback", text="shorten module 2", routed_to="code-generator")
        assert m["gates"]["preview"]["rework_count"] == 1
        assert m["gates"]["preview"]["feedback"][0]["routed_to"] == "code-generator"
        assert m["qa"]["cycle"] == 0 and m["qa"]["result"] == "not_run"
        assert m["stages"]["content-architect"]["state"] == "done"
        assert {m["stages"][k]["state"] for k in check.STAGE_ORDER[1:]} == {"pending"}

    def test_changing_previewed_files_after_accept_is_caught(self, tmp_path, repo):
        run = make_run(tmp_path)
        check.check_run(run, repo)
        check.gate(run, "preview", "accept")
        (run / "02-content" / "arc2-adlm.yaml").write_text("course_code: ARC2-ADLM\ntitle: edited\n")
        _, findings = check.check_run(run, repo)
        assert owners(findings, "gate.preview_unchanged") == {"orchestrator"}
        assert check.gate_verify(run) == ["preview"]

    def test_running_past_the_outline_gate_is_a_finding(self, tmp_path, repo):
        m = full_manifest()
        m["gates"]["outline"]["state"] = "pending"
        _, findings = check.check_run(make_run(tmp_path, m), repo)
        assert owners(findings, "gate.outline_accepted") == {"orchestrator"}


class TestCli:
    def test_check_json_prints_qa(self, tmp_path, repo, capsys):
        run = make_run(tmp_path)
        assert check.main(["check", str(run), "--json", "--repo-root", str(repo)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["result"] == "pass"

    def test_merge_of_missing_fragment_is_an_error(self, tmp_path, repo, capsys):
        run = init_run(tmp_path, repo)
        assert check.main(["merge", str(run), "range-engineer"]) == 1
        assert "no fragment" in capsys.readouterr().err

    def test_deep_copy_on_merge_keeps_manifest_untouched_on_reject(self, tmp_path, repo):
        run = init_run(tmp_path, repo)
        before = copy.deepcopy(check.load_manifest(run))
        (run / "03-range" / "fragment.json").write_text(json.dumps({"range": {"mode": "reuse"}}))
        with pytest.raises(check.ContractError):
            check.merge_fragment(run, "range-engineer")
        after = check.load_manifest(run)
        assert after["stages"]["range-engineer"]["state"] == "pending"
        assert after["provenance"] == before["provenance"]
