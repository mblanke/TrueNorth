"""scripts/arc2_qa_course.py and scripts/arc2_qa_batch.py: ARC²'s QA stage over an existing course
file and its instructor pack. The positive case is C105's committed ARC² bundle, adapted to the
TAZ shape (a course YAML plus a staff-only pack); every failure type has a synthetic bad course."""

from __future__ import annotations

import copy
import io
import json
import socket
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import arc2_qa_batch as batch  # noqa: E402
import arc2_qa_course as qa1  # noqa: E402
from arc2 import qa as arc2_qa  # noqa: E402

RELEASE = ROOT / "content/releases/c105-foundations-d0e83b9f6c1f.tar.gz"
HOSTS = ["jump-box", "dc-01", "ws-01", "fw-01"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The harness makes no network or model call; any attempt fails the test."""

    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def c105_course() -> dict:
    with tarfile.open(RELEASE) as tar:
        member = tar.extractfile("platform/02-content/arc2-c105.yaml")
        assert member is not None
        return yaml.safe_load(io.BytesIO(member.read()))


def labbed(doc: dict) -> dict:
    """C105 as a TAZ course: every module a range lab on small-enterprise, readiness draft."""
    doc = copy.deepcopy(doc)
    doc["readiness"] = "draft"
    for m in doc["modules"]:
        m.pop("activity", None)
        m["lab"] = "Lab: record the domain password policy. Evidence: the minimum length. Pass: recorded exactly."
        m["lab_range"] = {
            "template": "small-enterprise",
            "steps": [
                {"hosts": ["jump-box"], "action": "Log on to jump-box over SSH and open a terminal."},
                {"hosts": ["jump-box", "dc-01"], "action": "From jump-box, query dc-01 for the password policy."},
                {"hosts": ["ws-01", "fw-01"], "action": "On ws-01 note the gateway, then list the rules on fw-01."},
                {"hosts": ["jump-box"], "action": "Judge each finding and propose one change."},
            ],
        }
    return doc


def theory(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["readiness"] = "draft"
    return doc


def pack_for(doc: dict) -> dict:
    """An instructor pack whose answer-key notes cite every quiz question with the keyed letter."""
    mods = doc["modules"]
    return {
        "course_code": doc["course_code"],
        "audience": "staff_only",
        "overview": "Adapted from the C105 ARC2 bundle for the harness tests.",
        "timings": [
            {"module": m["ordinal"], "minutes": m["duration_minutes"], "plan": "teach, lab, quiz"} for m in mods
        ],
        "answer_key_notes": [
            {
                "module": m["ordinal"],
                "notes": " ".join(
                    f"Q{i} {q['answer']}: the keyed option is right; each distractor tests a misconception."
                    for i, q in enumerate(m["quiz"]["questions"], start=1)
                ),
            }
            for m in mods
        ],
        "lab_setup": [
            {"module": m["ordinal"], "setup": "Revert the range to baseline and confirm jump-box reaches dc-01."}
            for m in mods
        ],
        "common_faults": [{"module": 1, "fault": "dc-01 not reachable", "remedy": "Revert the range."}],
    }


def write(tmp_path: Path, doc: dict, pack: dict | None, name: str = "c105-harness.yaml") -> tuple[Path, Path | None]:
    cdir = tmp_path / "courses"
    (cdir / "instructor").mkdir(parents=True, exist_ok=True)
    course = cdir / name
    course.write_text(
        "# Unsourced generated draft: not validated by Standards.\n" + yaml.safe_dump(doc, sort_keys=False)
    )
    if pack is None:
        return course, None
    pfile = cdir / "instructor" / name
    pfile.write_text(yaml.safe_dump(pack, sort_keys=False))
    return course, pfile


def run(tmp_path: Path, doc: dict, pack: dict | None = None, **kw):
    course, pfile = write(tmp_path, doc, pack)
    return qa1.qa_course(course, pfile, tmp_path / "out", ROOT, catalogue_code="C105", **kw)


def checks_failing(report: dict) -> set[str]:
    return {f["check"] for f in report["findings"] if f["severity"] == "fail"}


def ledger(report: dict) -> dict[str, dict]:
    return {c["id"]: c for c in report["checks"]}


@pytest.fixture(scope="module")
def base() -> dict:
    return c105_course()


# ── positive cases: C105's ARC² bundle, adapted ──────────────────────────


def test_c105_theory_bundle_with_a_pack_passes(tmp_path, base):
    doc = theory(base)
    report = run(tmp_path, doc, pack_for(doc))
    assert checks_failing(report) == set(), report["findings"]
    assert report["result"] == "pass"
    assert all(o["verdict"] == "PASS" for o in report["objectives"]) and len(report["objectives"]) == 18
    assert report["lab_hosts_verified"] == {}
    assert ledger(report)["lab.hosts_in_template"]["status"] == "not_applicable"


def test_c105_with_labs_passes_and_lists_the_hosts_verified(tmp_path, base):
    doc = labbed(base)
    report = run(tmp_path, doc, pack_for(doc))
    assert checks_failing(report) == set(), report["findings"]
    assert report["lab_hosts_verified"] == {"small-enterprise": sorted(HOSTS)}
    assert ledger(report)["lab.hosts_in_template"]["status"] == "ran"


def test_the_release_is_a_real_arc2_run_the_adapter_agrees_with(tmp_path, base):
    """The adapter manifest feeds ARC²'s own functions: the same course passes arc2.qa directly."""
    doc = theory(base)
    report = run(tmp_path, doc, pack_for(doc))
    manifest = json.loads((tmp_path / "out/manifest.json").read_text())
    api, why = arc2_qa._api()
    assert api is not None, why
    direct = arc2_qa.check_course(tmp_path / "out", manifest, ROOT, api)
    assert [f for f in direct if f["severity"] == "fail" and f["check"] != "qa.course.arc2_code"] == []
    assert report["result"] == "pass"


# ── the report and what ran ──────────────────────────────────────────────


def test_report_files_follow_arc2s_layout(tmp_path, base):
    doc = labbed(base)
    run(tmp_path, doc, pack_for(doc))
    md = (tmp_path / "out/06-qa/report.md").read_text()
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert heads[:5] == ["## Preview digest", "## Objectives", "## Findings", "## Human actions", "## Verdict"]
    assert "## Checks" in heads and "## Needs range support" in heads
    js = json.loads((tmp_path / "out/06-qa/report.json").read_text())
    assert js["schema"] == qa1.REPORT_SCHEMA and js["result"] == "pass"
    for sub in ("02-content", "04-artifacts/instructor", "06-qa"):
        assert (tmp_path / "out" / sub).is_dir()


def test_every_check_is_accounted_for_and_n_a_carries_a_reason(tmp_path, base):
    doc = theory(base)
    report = run(tmp_path, doc, pack_for(doc))
    rows = ledger(report)
    assert {r["status"] for r in rows.values()} <= {"ran", "not_applicable", "not_run"}
    for cid in ("cmi5.*", "schema.*", "qa.timeline_* / qa.author_required_marker", "trace.xapi_*", "gate.*", "stage.*"):
        assert rows[cid]["status"] == "not_applicable"
        assert rows[cid]["reason"].startswith("not applicable (TAZ course): ")
    for cid in ("qa.course.*", "qa.defang", "qa.catalogue_identity", "objective.observable_verb", "po.*"):
        assert rows[cid]["status"] == "ran" and rows[cid]["source"] == "arc2"
    md = (tmp_path / "out/06-qa/report.md").read_text()
    assert "not applicable (TAZ course)" in md and "`cmi5.*`" in md


def test_readiness_other_than_sme_signed_is_an_open_human_action(tmp_path, base):
    doc = theory(base)
    report = run(tmp_path, doc, pack_for(doc))
    assert any(a["category"] == "standards" and a["blocks_promotion"] for a in report["human_actions"])


def test_api_unavailable_is_recorded_not_skipped(tmp_path, base, monkeypatch):
    monkeypatch.setattr(arc2_qa, "_api", lambda: (None, "boom"))
    doc = theory(base)
    report = run(tmp_path, doc, pack_for(doc))
    assert report["result"] == "fail"
    assert "qa.course_parse" in checks_failing(report)
    assert any("boom" in f["message"] for f in report["findings"])


# ── ARC²'s own checks fire ───────────────────────────────────────────────


def test_unpublished_rule_from_arc2_qa(tmp_path, base):
    doc = theory(base)
    doc["is_published"] = True
    assert "qa.course.unpublished" in checks_failing(run(tmp_path, doc, pack_for(doc)))


def test_unverified_reference_from_arc2_qa(tmp_path, base):
    doc = theory(base)
    doc["modules"][0]["refs"] = ["made-up-2024"]
    assert "qa.course.refs" in checks_failing(run(tmp_path, doc, pack_for(doc)))


def test_unobservable_objective_from_arc2_check(tmp_path, base):
    doc = theory(base)
    doc["modules"][0]["objectives"][0] = "Understand the confidentiality, integrity and availability triad"
    report = run(tmp_path, doc, pack_for(doc))
    assert "objective.observable_verb" in checks_failing(report)
    assert report["objectives"][0]["verdict"] == "FAIL"
    assert report["objectives"][3]["verdict"] == "PASS"  # module 2 is untouched


def test_unknown_catalogue_code_from_arc2_qa(tmp_path, base):
    doc = theory(base)
    course, pfile = write(tmp_path, doc, pack_for(doc))
    report = qa1.qa_course(course, pfile, tmp_path / "out", ROOT, catalogue_code="C999")
    assert "qa.catalogue_identity" in checks_failing(report)


def test_defang_scan_covers_the_pack(tmp_path, base):
    doc = theory(base)
    pack = pack_for(doc)
    pack["overview"] = "Run mimikatz on the domain controller."
    assert "qa.defang" in checks_failing(run(tmp_path, doc, pack))


def test_scenario_is_validated_by_the_engine_schema(tmp_path, base):
    doc = labbed(base)
    doc["modules"][0]["lab_range"]["scenario"] = "ransomware-lite"
    report = run(tmp_path, doc, pack_for(doc))
    assert ledger(report)["qa.engine_scenario"]["status"] == "ran"
    assert "qa.engine_scenario" not in checks_failing(report)


# ── TAZ course shape ─────────────────────────────────────────────────────


def test_missing_pack_fails_and_its_checks_say_not_run(tmp_path, base):
    report = run(tmp_path, theory(base), None)
    assert "taz.instructor" in checks_failing(report)
    assert ledger(report)["pack.answer_key_matches_quiz"]["status"] == "not_run"


def test_lab_modules_without_lab_range_fail(tmp_path, base):
    doc = labbed(base)
    del doc["modules"][2]["lab_range"]
    report = run(tmp_path, doc, pack_for(doc))
    assert "taz.modules" in checks_failing(report)


def test_pass_threshold_must_agree_with_the_quiz(tmp_path, base):
    doc = theory(base)
    doc["modules"][1]["quiz"]["pass_threshold"] = 80
    assert "content.pass_threshold_agrees" in checks_failing(run(tmp_path, doc, pack_for(doc)))


# ── the extra checks ─────────────────────────────────────────────────────


def test_a_host_the_template_lacks_fails(tmp_path, base):
    doc = labbed(base)
    doc["modules"][0]["lab_range"]["steps"][1]["hosts"] = ["jump-box", "dc-99"]
    report = run(tmp_path, doc, pack_for(doc))
    assert "lab.hosts_in_template" in checks_failing(report)
    assert report["objectives"][0]["verdict"] == "FAIL"
    assert "dc-99" not in report["lab_hosts_verified"].get("small-enterprise", [])


def test_a_host_word_in_the_action_the_template_lacks_fails(tmp_path, base):
    doc = labbed(base)
    doc["modules"][0]["lab_range"]["steps"][1]["action"] = "From jump-box, query dc01 for the password policy."
    assert "lab.hosts_in_template" in checks_failing(run(tmp_path, doc, pack_for(doc)))


def test_a_host_a_step_never_touches_fails(tmp_path, base):
    doc = labbed(base)
    doc["modules"][0]["lab_range"]["steps"][0]["hosts"] = ["jump-box", "ws-02"]
    report = run(tmp_path, doc, pack_for(doc))
    assert any(f["check"] == "lab.host_used" and "ws-02" in f["message"] for f in report["findings"])


def test_a_judgement_step_may_reuse_a_host_touched_earlier(tmp_path, base):
    doc = labbed(base)
    report = run(tmp_path, doc, pack_for(doc))
    assert "lab.host_used" not in checks_failing(report)  # step 4 lists jump-box with no hostname in its text


def test_an_unlisted_host_in_the_action_is_advisory(tmp_path, base):
    doc = labbed(base)
    doc["modules"][0]["lab_range"]["steps"][0]["action"] = "Log on to jump-box and ping ws-02."
    report = run(tmp_path, doc, pack_for(doc))
    assert any(f["check"] == "lab.host_unlisted" and f["severity"] == "advisory" for f in report["findings"])
    assert report["result"] == "pass"


def test_setup_the_range_lacks_is_unmarked_until_the_pack_lists_it(tmp_path, base):
    doc = labbed(base)
    doc["modules"][1]["lab_range"]["steps"][0]["action"] = "Log on to jump-box, then attach the spare disk to ws-01."
    doc["modules"][1]["lab_range"]["steps"][0]["hosts"] = ["jump-box", "ws-01"]
    report = run(tmp_path, doc, pack_for(doc))
    unmarked = [f for f in report["findings"] if f["check"] == "lab.range_support_unmarked"]
    assert unmarked and unmarked[0]["severity"] == "human" and report["result"] == "pass"
    assert report["needs_range_support"] == []


def test_needs_range_support_is_reported_and_never_fails_the_course(tmp_path, base):
    doc = labbed(base)
    doc["modules"][1]["lab_range"]["steps"][0]["action"] = "Log on to jump-box, then attach the spare disk to ws-01."
    doc["modules"][1]["lab_range"]["steps"][0]["hosts"] = ["jump-box", "ws-01"]
    pack = pack_for(doc)
    pack["lab_setup"][1]["setup"] = (
        "Revert the range. NEEDS RANGE SUPPORT: a spare 1 GB disk on ws-01; the template has one disk."
    )
    report = run(tmp_path, doc, pack)
    assert report["result"] == "pass" and checks_failing(report) == set()
    assert report["needs_range_support"] == [
        {"module": 2, "template": "small-enterprise", "item": "a spare 1 GB disk on ws-01"}
    ]
    finding = next(f for f in report["findings"] if f["check"] == "lab.needs_range_support")
    assert finding["severity"] == "advisory"
    assert not [f for f in report["findings"] if f["check"] == "lab.range_support_unmarked"]
    assert any(a["ask"] == "file a range ticket" for a in report["human_actions"])
    assert "a spare 1 GB disk on ws-01" in (tmp_path / "out/06-qa/report.md").read_text()


def test_answer_key_note_that_disagrees_with_the_quiz_fails(tmp_path, base):
    doc = theory(base)
    pack = pack_for(doc)
    key = doc["modules"][0]["quiz"]["questions"][0]["answer"]
    wrong = "A" if key != "A" else "B"
    pack["answer_key_notes"][0]["notes"] = pack["answer_key_notes"][0]["notes"].replace(f"Q1 {key}:", f"Q1 {wrong}:", 1)
    report = run(tmp_path, doc, pack)
    assert any(
        f["check"] == "pack.answer_key_matches_quiz" and "question 1" in f["message"] for f in report["findings"]
    )


def test_answer_key_note_that_skips_a_question_fails(tmp_path, base):
    doc = theory(base)
    pack = pack_for(doc)
    pack["answer_key_notes"][2]["notes"] = pack["answer_key_notes"][2]["notes"].split(" Q5 ")[0]
    report = run(tmp_path, doc, pack)
    assert any("do not cite question 5" in f["message"] for f in report["findings"])


def test_answer_key_note_that_cites_a_question_the_quiz_lacks_fails(tmp_path, base):
    doc = theory(base)
    pack = pack_for(doc)
    pack["answer_key_notes"][0]["notes"] += " Q6 A: a question that is not there."
    report = run(tmp_path, doc, pack)
    assert any("cite question 6" in f["message"] for f in report["findings"])


def test_pack_timing_that_disagrees_is_advisory(tmp_path, base):
    doc = theory(base)
    pack = pack_for(doc)
    pack["timings"][0]["minutes"] = 90
    report = run(tmp_path, doc, pack)
    assert any(f["check"] == "pack.timings_match" and f["severity"] == "advisory" for f in report["findings"])
    assert report["result"] == "pass"


# ── the CLI and the batch ────────────────────────────────────────────────


def test_cli_exit_codes(tmp_path, base, capsys):
    doc = theory(base)
    course, pfile = write(tmp_path, doc, pack_for(doc))
    args = [str(course), "--pack", str(pfile), "--out", str(tmp_path / "o"), "--catalogue-code", "C105"]
    assert qa1.main(args) == 0
    assert "PASS c105-harness.yaml" in capsys.readouterr().out
    assert qa1.main([str(tmp_path / "nope.yaml")]) == 2
    bad = copy.deepcopy(doc)
    bad["is_published"] = True
    course2, pfile2 = write(tmp_path, bad, pack_for(bad), "c105-bad.yaml")
    assert (
        qa1.main([str(course2), "--pack", str(pfile2), "--out", str(tmp_path / "o2"), "--catalogue-code", "C105"]) == 1
    )


def test_batch_summary_and_exit_code(tmp_path, base, monkeypatch):
    good, bad = labbed(base), labbed(base)
    bad["modules"][0]["lab_range"]["steps"][1]["hosts"] = ["jump-box", "dc-99"]
    write(tmp_path, good, pack_for(good), "c105-good.yaml")
    write(tmp_path, bad, pack_for(bad), "c105-bad.yaml")
    monkeypatch.setattr(qa1, "catalogue_code_for", lambda root, course, code: "C105")
    out = tmp_path / "batch"
    code = batch.main([str(tmp_path / "courses" / "*.yaml"), "--out", str(out)])
    assert code == 1
    summary = json.loads((out / "summary.json").read_text())
    rows = {r["file"]: r for r in summary["rows"]}
    assert set(rows) == {"c105-bad.yaml", "c105-good.yaml"}
    assert rows["c105-good.yaml"]["failures"] == 0 and rows["c105-good.yaml"]["result"] == "pass"
    assert rows["c105-bad.yaml"]["failing_checks"] == ["lab.host_used", "lab.hosts_in_template"]
    assert rows["c105-good.yaml"]["lab_hosts_verified"] == {"small-enterprise": sorted(HOSTS)}
    assert rows["c105-good.yaml"]["checks_ran"] > 15 and rows["c105-good.yaml"]["checks_not_applicable"] > 10
    md = (out / "summary.md").read_text()
    assert "small-enterprise: dc-01, fw-01, jump-box, ws-01" in md and "c105-bad.yaml" in md
    assert (out / "c105-good" / "06-qa" / "report.md").is_file()
    assert batch.main([str(tmp_path / "courses" / "c105-good.yaml"), "--out", str(tmp_path / "b2")]) == 0
    assert batch.main([str(tmp_path / "nothing-*.yaml")]) == 2
