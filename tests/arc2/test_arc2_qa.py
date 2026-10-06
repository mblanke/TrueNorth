"""QA depth tests: the repo's content rules applied to a run (course YAML, catalogue row, engine
scenario, timeline, defang scan), plus parity with the repo's own course files."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from arc2 import check, qa
from conftest import QUESTIONS
from test_arc2_manifest import full_manifest, make_run


def qa_run(tmp_path: Path, manifest: dict | None = None) -> tuple[Path, dict]:
    """make_run() already writes the course YAML, catalogue row, timeline and scenario."""
    manifest = manifest or full_manifest()
    return make_run(tmp_path, manifest), manifest


def fails(findings: list[dict]) -> set[str]:
    return {f["check"] for f in findings if f["severity"] == "fail"}


def owners(findings: list[dict], name: str) -> set[str]:
    return {f["owner_stage"] for f in findings if f["check"] == name}


def rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


class TestValid:
    def test_valid_run_has_no_failures(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        findings = qa.check_run(run, m, repo)
        assert fails(findings) == set(), findings
        assert {f["check"] for f in findings if f["severity"] == "human"} == set()

    def test_api_unavailable_is_a_human_finding(self, tmp_path, repo, monkeypatch):
        run, m = qa_run(tmp_path)
        monkeypatch.setattr(qa, "_api", lambda: (None, "boom"))
        findings = qa.check_run(run, m, repo)
        assert any(f["check"] == "qa.api_unavailable" and f["severity"] == "human" for f in findings)
        assert "qa.timeline_injects_match" not in fails(findings)  # the non-API checks still ran


class TestCourse:
    def test_published_course_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["content"]["course_yaml"], "is_published: false", "is_published: true")
        assert owners(qa.check_run(run, m, repo), "qa.course.unpublished") == {"code-generator"}

    def test_qsp_todo_is_not_a_course_qsp(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["content"]["course_yaml"], "qsp_code: ALJQ", "qsp_code: QSP-TODO")
        assert "qa.course.qsp_code" in fails(qa.check_run(run, m, repo))

    def test_unverified_reference_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["content"]["course_yaml"], "- nist-sp-800-92", "- made-up-2024")
        assert "qa.course.refs" in fails(qa.check_run(run, m, repo))

    def test_missing_reference_library_is_a_human_finding(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        (repo / qa.REFERENCES_REL).unlink()
        findings = qa.check_run(run, m, repo)
        assert any(f["check"] == "qa.references_unavailable" and f["severity"] == "human" for f in findings)
        assert "qa.course.refs" not in fails(findings)

    def test_duplicate_stem_across_the_library_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        other = {
            "course_code": "C999",
            "modules": [
                {
                    "ordinal": 1,
                    "quiz": {
                        "questions": [{"question": QUESTIONS[0][0], "options": ["a", "b", "c", "d"], "answer": "A"}]
                    },
                }
            ],
        }
        (repo / check.COURSES_REL / "c999.yaml").write_text(yaml.safe_dump(other))
        findings = qa.check_run(run, m, repo)
        assert any(f["check"] == "qa.course.duplicate_stem" and "c999.yaml" in f["message"] for f in findings)

    def test_framework_token_in_a_module_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["content"]["course_yaml"], "- SMB admin shares", "- SMB admin shares (DCWF T0023)")
        assert "qa.course.framework_tokens" in fails(qa.check_run(run, m, repo))

    def test_objectives_must_match_the_blueprint(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(
            run / m["content"]["course_yaml"],
            "- Detect lateral movement from authentication telemetry",
            "- Detect lateral movement",
        )
        assert owners(qa.check_run(run, m, repo), "qa.course_objectives_verbatim") == {"code-generator"}

    def test_module_shape_must_match_the_manifest(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(
            run / m["content"]["course_yaml"], "pass_threshold: 70\n  objectives", "pass_threshold: 60\n  objectives"
        )
        findings = qa.check_run(run, m, repo)
        assert any(f["check"] == "qa.course_manifest_mismatch" and "pass_threshold" in f["message"] for f in findings)

    def test_quiz_rules(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["content"]["course_yaml"], "answer: B", "answer: E")
        assert "qa.course.quiz_answer" in fails(qa.check_run(run, m, repo))

    def test_course_code_must_be_arc2(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["content"]["course_yaml"], "course_code: ARC2-ADLM", "course_code: C204")
        findings = qa.check_run(run, m, repo)
        assert {"qa.course.arc2_code", "qa.course_code_mismatch"} <= fails(findings)


class TestCatalogue:
    def test_missing_row_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        (run / qa.CATALOGUE_ROW_REL).unlink()
        assert owners(qa.check_run(run, m, repo), "qa.catalogue_row_missing") == {"content-architect"}

    def test_row_must_match_the_manifest(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / qa.CATALOGUE_ROW_REL, "ARC2-ADLM,Detecting", "ARC2-OTHER,Detecting")
        assert any(
            f["check"] == "qa.catalogue_row" and "course_code" in f["message"] for f in qa.check_run(run, m, repo)
        )

    def test_existing_course_code_is_rejected(self, tmp_path, repo):
        m = full_manifest()
        m["course"]["code"] = "C101"
        run, m = qa_run(tmp_path, m)
        rewrite(run / qa.CATALOGUE_ROW_REL, "ARC2-ADLM,Detecting", "C101,Detecting")
        assert "qa.catalogue_code_unique" in fails(qa.check_run(run, m, repo))

    def test_wrong_columns_fail(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / qa.CATALOGUE_ROW_REL, "programme,institution", "program,institution")
        assert "qa.catalogue_columns" in fails(qa.check_run(run, m, repo))


class TestEngineScenario:
    def test_schema_violation_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["scenario"]["path"], "objectives:", "goals:")
        assert owners(qa.check_run(run, m, repo), "qa.engine_scenario") == {"sensor-gateway"}

    def test_unavailable_schema_is_a_human_finding(self, tmp_path, repo, monkeypatch):
        run, m = qa_run(tmp_path)
        api, why = qa._api()
        assert api is not None, why

        def unavailable(*_args, **_kwargs):
            raise OSError("scenario.schema.json: no such file")

        monkeypatch.setattr(api["engine"], "validate_yaml", unavailable)  # what a missing engine dir raises through
        findings = qa.check_run(run, m, repo)
        assert any(f["check"] == "qa.engine_schema_unavailable" and f["severity"] == "human" for f in findings)


class TestTimeline:
    def test_disabled_template_fails(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["injects"]["timeline"], "[srv2019, win10-22h2, securityonion]", "[srv2019, win-xp-sp3]")
        assert owners(qa.check_run(run, m, repo), "qa.timeline_templates_enabled") == {"range-engineer"}

    def test_noise_floor_and_inject_ids(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        path = run / m["injects"]["timeline"]
        rewrite(
            path,
            "    - id: n2\n      description: Helpdesk RDP session\n      why_plausible: Ticketed remote support\n",
            "",
        )
        rewrite(path, "- id: inj-01", "- id: inj-99")
        findings = qa.check_run(run, m, repo)
        assert {"qa.timeline_noise_floor", "qa.timeline_injects_match"} <= fails(findings)

    def test_crit_validator_must_be_listed(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        rewrite(run / m["injects"]["timeline"], "    - validators/crit_lateral_movement.md\n", "")
        assert "qa.timeline_validators_listed" in fails(qa.check_run(run, m, repo))

    def test_author_required_needs_the_placeholder(self, tmp_path, repo):
        m = full_manifest()
        m["injects"]["items"][0]["author_required"] = True
        run, m = qa_run(tmp_path, m)
        assert "qa.author_required_marker" in fails(qa.check_run(run, m, repo))
        rewrite(
            run / m["injects"]["timeline"],
            'description: "Pre-captured',
            'description: "AUTHOR-REQUIRED: a cleared author supplies the capture. Pre-captured',
        )
        assert "qa.author_required_marker" not in fails(qa.check_run(run, m, repo))


class TestDefang:
    def test_encoded_command_in_the_timeline_routes_to_range_engineer(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        path = run / m["injects"]["timeline"]
        path.write_text(path.read_text() + "  # powershell -enc SGVsbG8gV29ybGQgSGVsbG8gV29ybGQgSGVsbG8=\n")
        assert owners(qa.check_run(run, m, repo), "qa.defang") == {"range-engineer"}

    def test_placeholder_lines_are_exempt(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        path = run / m["injects"]["timeline"]
        path.write_text(
            path.read_text()
            + "  # AUTHOR-REQUIRED: the operator's own powershell -enc SGVsbG8gV29ybGQgSGVsbG8gV29ybGQgSGVsbG8= stays with the author\n"
        )
        assert "qa.defang" not in fails(qa.check_run(run, m, repo))

    def test_tool_names_in_pages_route_to_code_generator(self, tmp_path, repo):
        run, m = qa_run(tmp_path)
        page = run / m["content"]["modules"][0]["pages"][0]
        page.write_text("<p>Run mimikatz to dump credentials.</p>")
        assert owners(qa.check_run(run, m, repo), "qa.defang") == {"code-generator"}


class TestParity:
    def test_course_rules_accept_every_repo_course_file(self):
        courses = sorted((check.REPO_ROOT / qa.COURSES_REL).glob("*.yaml"))
        if not courses:
            pytest.skip("no content/courses in this checkout")
        api, why = qa._api()
        assert api is not None, why
        references = qa.reference_keys(check.REPO_ROOT)
        stems = qa.library_stems(check.REPO_ROOT)
        qsps = qa.known_qsps(check.REPO_ROOT)
        problems = []
        for path in courses:
            text = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(text)
            parsed = api["course"].parse_course_content(text)
            for rule, message in qa.course_rules(
                raw, parsed, own_name=path.name, references=references, stems=stems, qsp_codes=qsps, arc2=False
            ):
                problems.append(f"{path.name}: {rule}: {message}")
        assert problems == [], "\n".join(problems)
