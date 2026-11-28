# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/withdraw_fixture_course.py: the dry-run report and the applied report rendered
from the withdraw endpoint's body, and run()'s exit contract — 0 clean, 1 when the API
reported errors, 2 when the call was refused — with --json and a tenant filter. The API is
a recording fake; main() is not covered here: it needs the /srcapp checkout and the
in-process API harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_f_withdraw_fixture_course", ROOT / "scripts" / "withdraw_fixture_course.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class FakeApi:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {}
        self.calls = []

    def post(self, path, params=None):
        self.calls.append((path, params))
        return self.status, self.body


def body(**over):
    base = {
        "code": "C105",
        "tenant_id": "t-1",
        "dry_run": True,
        "changed": False,
        "courses": [
            {
                "id": "c-9",
                "name": "C105 — Release Fixture",
                "is_published": True,
                "enrolments_total": 3,
                "enrolments_graded": 1,
                "module_progress": 2,
                "quiz_attempts": 4,
                "moodle_results": 1,
                "quizzes": [
                    {
                        "id": "q-1",
                        "title": "Phishing check",
                        "is_published": True,
                        "attempts": 4,
                        "submitted_attempts": 2,
                    }
                ],
                "moodle": [
                    {"platform_name": "moodle-1", "live_idnumber": "c105-site", "visibility": "hidden", "error": None}
                ],
            }
        ],
        "changes": ["unpublish quiz q-1", "unpublish course c-9"],
        "preserved": {"enrolments": 3, "attempts": 4},
        "errors": [],
    }
    base.update(over)
    return base


def test_summarise_renders_a_dry_run_with_changes_and_the_rerun_hint():
    lines = mod.summarise(body())
    assert lines[0] == "C105 in tenant t-1: DRY RUN: nothing changed"
    assert "  course c-9 'C105 — Release Fixture': published" in lines
    assert "    quiz q-1 'Phishing check': published, 4 attempts (2 graded)" in lines
    assert any(
        "enrolments 3 (1 with a grade), module progress 2, quiz attempts 4, Moodle results 1" in line for line in lines
    )
    assert "    moodle moodle-1 course c105-site: hidden" in lines
    assert "  would change: 2" in lines and "    unpublish quiz q-1" in lines
    assert "  kept as they are: enrolments 3, attempts 4" in lines
    assert lines[-1] == "  re-run with --apply to make these changes"


def test_summarise_renders_an_applied_run_without_the_rerun_hint():
    lines = mod.summarise(body(dry_run=False, changed=True, changes=["unpublish course c-9"]))
    assert lines[0] == "C105 in tenant t-1: APPLIED"
    assert "  changed: 1" in lines
    assert not any("--apply" in line for line in lines)


def test_summarise_handles_no_course_no_moodle_and_an_erroring_moodle_row():
    lines = mod.summarise(body(courses=[], errors=["tenant not found"]))
    assert "  no course with catalogue code C105 in this tenant" in lines
    assert any(line.startswith("  kept as they are: ") for line in lines)
    assert "  ERROR tenant not found" in lines

    draft = body(
        courses=[
            {
                "id": "c-9",
                "name": "C105 — Release Fixture",
                "is_published": False,
                "enrolments_total": 0,
                "enrolments_graded": 0,
                "module_progress": 0,
                "quiz_attempts": 0,
                "moodle_results": 0,
                "quizzes": [],
                "moodle": [
                    {
                        "platform_name": "moodle-1",
                        "live_idnumber": "x",
                        "visibility": "deleted",
                        "error": "course vanished",
                    }
                ],
            }
        ],
        changes=[],
    )
    lines = mod.summarise(draft)
    assert "  course c-9 'C105 — Release Fixture': draft" in lines
    assert "    moodle moodle-1 course x: deleted (course vanished)" in lines
    assert "  would change: 0" in lines

    lines = mod.summarise(body(courses=[{**body()["courses"][0], "moodle": []}]))
    assert "    moodle: never published" in lines


def test_run_posts_a_dry_run_by_default_and_prints_the_report(capsys):
    api = FakeApi(body=body())
    assert mod.run(api, "C105", apply=False, tenant=None, as_json=False) == 0
    assert api.calls == [("/admin/fixture-courses/C105/withdraw", {"dry_run": "true"})]
    out = capsys.readouterr().out
    assert "DRY RUN: nothing changed" in out and "would change: 2" in out


def test_run_applies_and_forwards_the_tenant(capsys):
    api = FakeApi(body=body(dry_run=False, changed=True))
    assert mod.run(api, "C105", apply=True, tenant="t-2", as_json=False) == 0
    assert api.calls == [("/admin/fixture-courses/C105/withdraw", {"dry_run": "false", "tenant_id": "t-2"})]
    assert "APPLIED" in capsys.readouterr().out


def test_run_json_prints_the_raw_body(capsys):
    api = FakeApi(body=body(dry_run=False))
    assert mod.run(api, "C105", apply=False, tenant=None, as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["code"] == "C105"


def test_run_reports_api_errors_with_exit_1(capsys):
    api = FakeApi(body=body(errors=["course is not a fixture"]))
    assert mod.run(api, "C105", apply=False, tenant=None, as_json=False) == 1
    assert "ERROR course is not a fixture" in capsys.readouterr().out


def test_run_refusal_is_exit_2(capsys):
    api = FakeApi(status=403, body={"detail": "not a platform administrator"})
    assert mod.run(api, "C105", apply=False, tenant=None, as_json=False) == 2
    assert "refused: 403 not a platform administrator" in capsys.readouterr().err

    api = FakeApi(status=200, body="gateway text")  # a non-dict success body is also refused
    assert mod.run(api, "C105", apply=False, tenant=None, as_json=False) == 2
    assert "refused: 200 gateway text" in capsys.readouterr().err
