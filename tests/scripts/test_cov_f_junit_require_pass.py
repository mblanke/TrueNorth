# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/junit_require_pass.py: passed is tests minus failures, errors and skipped, and
the gate fails on unreadable reports, failing tests, and on a green-looking report where
nothing actually passed (the all-skipped lane). Standard library only, so no fakes are
needed — reports are written to tmp_path. The --report argument is required, so argparse
handles the no-argument call itself."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_f_junit_require_pass", ROOT / "scripts" / "junit_require_pass.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def report(tmp_path: Path, attrs: dict, tag: str = "testsuite") -> Path:
    """A JUnit file with one suite carrying the given counters (or several, via a list)."""
    suites = attrs if isinstance(attrs, list) else [attrs]
    body = "".join(
        f'<{tag} tests="{s.get("tests", 0)}" failures="{s.get("failures", 0)}" '
        f'errors="{s.get("errors", 0)}" skipped="{s.get("skipped", 0)}"/>'
        for s in suites
    )
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites>{body}</testsuites>", encoding="utf-8")
    return path


def test_counts_sums_suites_and_derives_passed(tmp_path):
    path = report(
        tmp_path,
        [
            {"tests": 10, "failures": 2, "errors": 1, "skipped": 3},
            {"tests": 5, "failures": 0, "errors": 0, "skipped": 1},
        ],
    )
    assert mod.counts(path) == {
        "tests": 15,
        "failures": 2,
        "errors": 1,
        "skipped": 4,
        "passed": 8,
    }


def test_counts_takes_a_bare_testsuite_root_and_defaults_missing_counters(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite tests="4" failures="1"/>', encoding="utf-8")
    assert mod.counts(path) == {
        "tests": 4,
        "failures": 1,
        "errors": 0,
        "skipped": 0,
        "passed": 3,
    }


def test_main_passes_a_healthy_report(tmp_path, capsys):
    path = report(tmp_path, {"tests": 3})
    assert mod.main([str(path)]) == 0
    assert "3 passed, 0 failed, 0 errors, 0 skipped (of 3)" in capsys.readouterr().out


def test_main_fails_a_report_with_failures_even_when_passed_would_be_positive(tmp_path, capsys):
    path = report(tmp_path, {"tests": 9, "failures": 2, "skipped": 1})
    assert mod.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "failing or erroring tests in the report" in err


def test_main_fails_an_all_skipped_report(tmp_path, capsys):
    path = report(tmp_path, {"tests": 7, "skipped": 7})
    assert mod.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "zero tests passed; a required lane that runs nothing is a failure" in err


def test_main_fails_an_unreadable_report(tmp_path, capsys):
    missing = tmp_path / "nope.xml"
    assert mod.main([str(missing)]) == 1
    assert f"cannot read {missing}" in capsys.readouterr().err

    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuite tests=", encoding="utf-8")
    assert mod.main([str(broken)]) == 1
    assert f"cannot read {broken}" in capsys.readouterr().err
