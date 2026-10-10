"""scripts/junit_require_pass.py fails a lane that ran nothing, and one with failures."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("junit_require_pass", ROOT / "scripts/junit_require_pass.py")
jrp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jrp)


def _report(tmp_path: Path, tests: int, failures: int = 0, errors: int = 0, skipped: int = 0) -> str:
    p = tmp_path / "junit.xml"
    p.write_text(
        f'<testsuites><testsuite name="s" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}"/></testsuites>'
    )
    return str(p)


def test_all_passed_is_ok(tmp_path):
    assert jrp.main([_report(tmp_path, 4)]) == 0


def test_all_skipped_fails(tmp_path):
    assert jrp.main([_report(tmp_path, 4, skipped=4)]) == 1


def test_any_failure_fails_even_with_passes(tmp_path):
    assert jrp.main([_report(tmp_path, 4, failures=3)]) == 1


def test_any_error_fails_even_with_passes(tmp_path):
    assert jrp.main([_report(tmp_path, 4, errors=1)]) == 1


def test_unreadable_report_fails(tmp_path):
    assert jrp.main([str(tmp_path / "missing.xml")]) == 1
