"""cmi5 emission tests: the derived block, the cmi5.xml writer, the structural and XSD ladder,
the package layout checks, and parity with the knowledge pack's AU runtime.

Builds on the fixtures in test_arc2_manifest: a packaged run is the valid one-module run
taken through QA pass, preview accept, ``arc2.cmi5 package`` and the package-builder merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arc2 import check, cmi5
from test_arc2_manifest import full_manifest, make_run, owners

KB_AU = check.REPO_ROOT / "docs" / "xAPI CMI5 Reference" / "xapi-cmi5-kb" / "au" / "cmi5-au.js"
KB_EXAMPLE = check.REPO_ROOT / "docs" / "xAPI CMI5 Reference" / "xapi-cmi5-kb" / "examples" / "cmi5.xml"
QUESTIONS = [
    {"id": f"q{i}", "stem": f"Question {i} stem?", "options": ["a", "b", "c", "d"], "answer": "B"} for i in range(1, 6)
]


def module_config(module: dict, code: str = "ARC2-ADLM") -> dict:
    ids = cmi5.publisher_ids(code, module["id"], module["objective_ids"])
    return {
        "schema": cmi5.CONFIG_SCHEMA,
        "module_id": module["id"],
        "ordinal": module["ordinal"],
        "title": module["title"],
        "au_id": ids["au_id"],
        "objective_ids": list(module["objective_ids"]),
        "objectives": [
            {"id": o, "text": "Detect lateral movement", "iri": ids["objective_iris"][o]}
            for o in module["objective_ids"]
        ],
        "moveOn": "Passed" if module["quiz"] else "Completed",
        "masteryScore": cmi5.mastery_score(module),
        "lang": "en-CA",
        "content": {"pages": [f"content/{Path(p).name}" for p in module["pages"]], "lab": None},
        "quiz": {"title": "Quiz 1", "pass_threshold": module["quiz"]["pass_threshold"], "questions": QUESTIONS}
        if module["quiz"]
        else None,
        "media": {"images": [], "videos": []},
    }


def write_module_files(run: Path, manifest: dict) -> None:
    for m in manifest["content"]["modules"]:
        cfg = run / m["config"]
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(module_config(m), indent=2))
        for page in m["pages"]:
            p = run / page
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"<h2>{m['title']}</h2><p>Authentication telemetry shows the admin share session.</p>")


def packaged_run(tmp_path: Path, repo: Path, manifest: dict | None = None, base_url: str | None = None) -> Path:
    manifest = manifest or full_manifest()
    run = make_run(tmp_path, manifest)
    write_module_files(run, manifest)
    (run / "06-qa" / "fragment.json").write_text("{}")
    check.merge_fragment(run, "qa-tester")
    m, findings = check.check_run(run, repo)
    assert m["qa"]["result"] == "pass", [f for f in findings if f.severity == "fail"]
    check.gate(run, "preview", "accept", repo_root=repo)
    block, files = cmi5.write_package(run, check.load_manifest(run), base_url)
    cmi5.update_fragment(run, block, files)
    fragment = json.loads((run / "07-bundle" / "fragment.json").read_text())
    fragment["bundle"] = {
        "promote_md": "07-bundle/PROMOTE.md",
        "destinations": [
            {"source": "02-content/arc2-adlm.yaml", "destination": "content/courses/arc2-adlm.yaml", "kind": "course"}
        ],
    }
    (run / "07-bundle" / "fragment.json").write_text(json.dumps(fragment))
    check.merge_fragment(run, "package-builder")
    return run


def fails(findings: list[check.Finding]) -> set[str]:
    return {f.check for f in findings if f.severity == "fail"}


def humans(findings: list[check.Finding]) -> set[str]:
    return {f.check for f in findings if f.severity == "human"}


def mutate_block(run: Path, fn) -> None:
    m = check.load_manifest(run)
    fn(m)
    check.save_manifest(run, m)


# ── the derived block and the writer ───────────────────────────────────


class TestBlockAndWriter:
    def test_ids_are_deterministic_and_lowercase(self):
        ids = cmi5.publisher_ids("ARC2-ADLM", "mod_001", ["M01-O01"])
        assert ids["course_id"] == "https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm"
        assert ids["au_id"] == "https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm/au/mod_001"
        assert ids["objective_iris"]["M01-O01"].endswith("/obj/M01-O01")

    def test_block_derives_moveon_and_mastery_from_the_quiz(self):
        m = full_manifest()
        m["content"]["modules"].append(
            {
                **m["content"]["modules"][0],
                "id": "mod_002",
                "ordinal": 2,
                "title": "Reading only",
                "quiz": None,
                "config": "02-content/mod_002/course-config.json",
                "pages": ["02-content/mod_002/content/page-01.html"],
            }
        )
        block = cmi5.build_block(m)
        assert [a["moveOn"] for a in block["aus"]] == ["Passed", "Completed"]
        assert block["aus"][0]["masteryScore"] == 0.7
        assert "masteryScore" not in block["aus"][1]
        assert block["id_scheme"] == "arc2-provisional"
        assert json.loads(block["aus"][0]["launchParameters"]) == {"module": "mod_001", "lang": "en-CA"}

    def test_block_needs_stages_one_and_two(self):
        m = full_manifest()
        del m["content"]
        with pytest.raises(cmi5.Cmi5Error, match="content"):
            cmi5.build_block(m)

    def test_writer_output_is_structurally_valid_and_matches_the_block(self):
        m = full_manifest()
        block = cmi5.build_block(m)
        xml = cmi5.write_course_structure(m, block)
        assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
        assert f'xmlns="{cmi5.NS}"' in xml
        assert 'moveOn="Passed" masteryScore="0.7" launchMethod="AnyWindow"' in xml
        assert "<url>mod_001/index.html</url>" in xml
        assert cmi5.check_xml_structure(xml, block) == []

    def test_base_url_is_prefixed_in_the_xml_only(self):
        m = full_manifest()
        block = cmi5.build_block(m, base_url="https://content.example.mil/arc2/")
        assert block["aus"][0]["url"] == "mod_001/index.html"
        xml = cmi5.write_course_structure(m, block)
        assert "<url>https://content.example.mil/arc2/mod_001/index.html</url>" in xml
        assert cmi5.check_xml_structure(xml, block) == []

    def test_kb_example_passes_the_structure_check(self):
        assert cmi5.check_xml_structure(KB_EXAMPLE.read_text()) == []

    def test_structure_check_catches_the_rules(self):
        base = full_manifest()
        block = cmi5.build_block(base)
        xml = cmi5.write_course_structure(base, block)
        assert any("moveOn" in e for e in cmi5.check_xml_structure(xml.replace('moveOn="Passed"', 'moveOn="Pased"')))
        assert any(
            "masteryScore" in e
            for e in cmi5.check_xml_structure(xml.replace('masteryScore="0.7"', 'masteryScore="1.2"'))
        )
        dangling = xml.replace(
            'idref="https://ccoe.forces.gc.ca/xapi/arc2/arc2-adlm/obj/M01-O01"', 'idref="https://x/obj/NOPE"'
        )
        assert any("idref" in e for e in cmi5.check_xml_structure(dangling))
        assert any(
            "from manifest" in e for e in cmi5.check_xml_structure(xml.replace("/au/mod_001", "/au/mod_009"), block)
        )
        assert cmi5.check_xml_structure("<nope/>") == [f"root is nope, expected {cmi5.q('courseStructure')}"]

    def test_writer_validates_against_the_xsd_when_vendored(self):
        pytest.importorskip("lxml")
        if not cmi5.XSD_PATH.is_file():
            pytest.skip("CourseStructure.xsd not vendored; see tools/arc2/vendor/README.md")
        m = full_manifest()
        status, errors = cmi5.validate_xsd(cmi5.write_course_structure(m, cmi5.build_block(m)))
        assert status == "validated", errors

    def test_cmi5_js_is_the_knowledge_pack_runtime(self):
        assert cmi5.sha256_file(cmi5.AU_DIR / "cmi5.js") == cmi5.sha256_file(KB_AU)


# ── the packaged run ────────────────────────────────────────────────────


class TestPackage:
    def test_valid_package_passes_with_only_human_findings(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        m, findings = check.check_run(run, repo)
        assert fails(findings) == set()
        assert "cmi5.publisher_id_provisional" in humans(findings)
        assert m["cmi5"]["xsd"]["status"] in {"validated", "xsd_not_vendored", "validator_unavailable"}
        assert m["qa"]["result"] == "pass"
        root = run / cmi5.PACKAGE_ROOT
        assert {p.name for p in root.iterdir()} == {
            "cmi5.xml",
            "cmi5.js",
            "course.js",
            "README.md",
            "images",
            "videos",
            "mod_001",
        }
        assert (root / "mod_001" / "content" / "page-01.html").is_file()
        assert "../cmi5.js" in (root / "mod_001" / "index.html").read_text()
        assert any(a["category"] == "package" for a in m["human_actions"])
        kinds = {(f["path"], f["kind"]) for f in m["files"] if f["path"].startswith("07-bundle/cmi5/")}
        assert ("07-bundle/cmi5/cmi5.js", "runtime") in kinds
        assert ("07-bundle/cmi5/cmi5.xml", "package") in kinds

    def test_missing_xsd_is_a_human_finding_not_a_pass(self, tmp_path, repo, monkeypatch):
        monkeypatch.setattr(cmi5, "XSD_PATH", tmp_path / "missing.xsd")
        run = packaged_run(tmp_path, repo)
        m, findings = check.check_run(run, repo)
        human = [f for f in findings if f.check == "cmi5.xml_xsd"]
        assert human and human[0].severity == "human"
        assert m["cmi5"]["xsd"]["status"] == "xsd_not_vendored"
        assert m["qa"]["result"] == "pass"

    def test_missing_module_files_stop_packaging(self, tmp_path, repo):
        run = make_run(tmp_path)
        (run / "02-content" / "mod_001" / "course-config.json").unlink()
        with pytest.raises(cmi5.Cmi5Error, match="course-config.json is missing"):
            cmi5.write_package(run, check.load_manifest(run))

    def test_au_with_no_objectives_fails(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        mutate_block(run, lambda m: m["cmi5"]["aus"][0].__setitem__("objective_ids", []))
        _, findings = check.check_run(run, repo)
        assert {"cmi5.au_objectives_nonempty", "cmi5.objective_covered"} <= fails(findings)

    def test_duplicate_au_id_fails(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)

        def dup(m):
            m["cmi5"]["aus"].append(dict(m["cmi5"]["aus"][0]))

        mutate_block(run, dup)
        _, findings = check.check_run(run, repo)
        assert {"cmi5.au_ids_unique", "cmi5.block_ids_unique", "cmi5.one_au_per_module"} <= fails(findings)

    def test_bad_moveon_fails(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        mutate_block(run, lambda m: m["cmi5"]["aus"][0].__setitem__("moveOn", "Pased"))
        _, findings = check.check_run(run, repo)
        assert "package-builder" in owners(findings, "schema.valid")

        def not_applicable(m):
            m["cmi5"]["aus"][0]["moveOn"] = "NotApplicable"
            del m["cmi5"]["aus"][0]["masteryScore"]

        mutate_block(run, not_applicable)
        _, findings = check.check_run(run, repo)
        assert owners(findings, "cmi5.moveon_allowed") == {"package-builder"}

    def test_objective_in_no_au_fails(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        mutate_block(
            run,
            lambda m: m["objectives"].append(
                {
                    "id": "M01-O02",
                    "module_id": "mod_001",
                    "text": "Reconstruct the attacker path from SMB logs",
                    "source": "objective",
                    "po": None,
                }
            ),
        )
        _, findings = check.check_run(run, repo)
        assert {"cmi5.objective_covered", "cmi5.objective_iris"} <= fails(findings)

    def test_mastery_score_rules(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        mutate_block(run, lambda m: m["cmi5"]["aus"][0].__setitem__("masteryScore", 1.2))
        _, findings = check.check_run(run, repo)
        assert "package-builder" in owners(findings, "schema.valid")

        mutate_block(run, lambda m: m["cmi5"]["aus"][0].__delitem__("masteryScore"))
        _, findings = check.check_run(run, repo)
        assert "package-builder" in owners(findings, "schema.valid")  # Passed needs a masteryScore

        mutate_block(run, lambda m: m["cmi5"]["aus"][0].__setitem__("masteryScore", 0.71))
        _, findings = check.check_run(run, repo)
        assert owners(findings, "cmi5.mastery_score") == {"package-builder"}

        def yaml_inconsistent(m):
            m["cmi5"]["aus"][0]["masteryScore"] = 0.7
            m["content"]["modules"][0]["pass_threshold"] = 60

        mutate_block(run, yaml_inconsistent)
        _, findings = check.check_run(run, repo)
        assert owners(findings, "cmi5.mastery_score") == {"code-generator"}

    def test_xml_disagreeing_with_the_manifest_fails(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        xml = run / cmi5.PACKAGE_ROOT / "cmi5.xml"
        xml.write_text(xml.read_text().replace("/au/mod_001", "/au/mod_009"))
        _, findings = check.check_run(run, repo)
        assert owners(findings, "cmi5.xml_structure") == {"package-builder"}

    def test_config_drift_is_caught(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        cfg = run / cmi5.PACKAGE_ROOT / "mod_001" / "course-config.json"
        data = json.loads(cfg.read_text())
        data["moveOn"] = "Completed"
        cfg.write_text(json.dumps(data))
        _, findings = check.check_run(run, repo)
        assert {"code-generator", "package-builder"} == owners(findings, "cmi5.config_matches_manifest")

    def test_framework_tokens_and_instructor_content_are_rejected(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        page = run / cmi5.PACKAGE_ROOT / "mod_001" / "content" / "page-01.html"
        page.write_text(page.read_text() + "<p>Maps to DCWF task T0023.</p>")
        (run / cmi5.PACKAGE_ROOT / "mod_001" / "instructor").mkdir()
        (run / cmi5.PACKAGE_ROOT / "mod_001" / "instructor" / "answers.md").write_text("B B B B B")
        _, findings = check.check_run(run, repo)
        assert owners(findings, "cmi5.no_framework_tokens") == {"code-generator"}
        assert owners(findings, "cmi5.no_instructor_content") == {"package-builder"}

    def test_tampered_runtime_is_caught(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        js = run / cmi5.PACKAGE_ROOT / "cmi5.js"
        js.write_text(js.read_text() + "\n// tampered\n")
        _, findings = check.check_run(run, repo)
        assert any(f.check == "cmi5.au_layout" and "cmi5.js differs" in f.message for f in findings)

    def test_package_command_updates_the_fragment_idempotently(self, tmp_path, repo):
        run = packaged_run(tmp_path, repo)
        assert cmi5.main(["package", str(run)]) == 0
        assert cmi5.main(["package", str(run)]) == 0
        fragment = json.loads((run / "07-bundle" / "fragment.json").read_text())
        assert "bundle" in fragment and "cmi5" in fragment
        paths = [f["path"] for f in fragment["files"]]
        assert len(paths) == len(set(paths))


class TestCli:
    def test_ids_prints_json(self, capsys):
        assert cmi5.main(["ids", "ARC2-ADLM", "mod_002", "--objectives", "M02-O01"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["au_id"].endswith("/au/mod_002") and "M02-O01" in out["objective_iris"]

    def test_validate_accepts_the_kb_example(self, capsys):
        assert cmi5.main(["validate", str(KB_EXAMPLE)]) == 0
        assert "xsd:" in capsys.readouterr().out

    def test_validate_rejects_a_broken_document(self, tmp_path, capsys):
        bad = tmp_path / "bad.xml"
        bad.write_text(KB_EXAMPLE.read_text().replace('moveOn="Completed"', 'moveOn="Later"'))
        assert cmi5.main(["validate", str(bad)]) == 1
        assert "moveOn" in capsys.readouterr().out
