# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/ci_shard.py: collection minus integration, longest-first dealing onto the least
loaded shard with a median fallback for unrecorded files, the 1..total shard range check,
--refresh summing junit testcase seconds per file, and the argument handling in main().
The synthetic trees live under tmp_path; refresh's TIMINGS write is redirected there too,
so the repo's own scripts/ci_shard_timings.json is never touched."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_f_ci_shard", ROOT / "scripts" / "ci_shard.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_test_files_collects_pytest_defaults_and_skips_integration(tmp_path):
    tests = tmp_path / "tests"
    (tests / "unit").mkdir(parents=True)
    (tests / "integration").mkdir()
    (tests / "unit" / "test_a.py").write_text("", encoding="utf-8")
    (tests / "test_b.py").write_text("", encoding="utf-8")
    (tests / "suffix_c_test.py").write_text("", encoding="utf-8")
    (tests / "integration" / "test_live.py").write_text("", encoding="utf-8")
    (tests / "unit" / "helper.py").write_text("", encoding="utf-8")
    (tests / "unit" / "test_d.txt").write_text("", encoding="utf-8")
    assert mod.test_files(tmp_path) == [
        "tests/suffix_c_test.py",
        "tests/test_b.py",
        "tests/unit/test_a.py",
    ]


def test_load_timings_reads_the_json_or_returns_empty(tmp_path):
    path = tmp_path / "timings.json"
    assert mod.load_timings(path) == {}
    path.write_text('{"tests/test_b.py": 12.5}', encoding="utf-8")
    assert mod.load_timings(path) == {"tests/test_b.py": 12.5}


def test_assign_deals_longest_first_onto_the_least_loaded_shard():
    files = ["a.py", "b.py", "c.py", "d.py"]
    timings = {"a.py": 100.0, "b.py": 60.0, "c.py": 40.0, "d.py": 20.0}
    shards = mod.assign(files, 2, timings)
    # a→shard1, b→shard2, c→shard2 (60<100), d→shard1 (80<100); each shard is sorted.
    assert shards == [["a.py", "d.py"], ["b.py", "c.py"]]


def test_assign_ties_go_to_the_lowest_shard_and_unknown_files_count_the_median():
    shards = mod.assign(["a.py", "b.py", "c.py"], 3, {"a.py": 10.0, "b.py": 10.0})
    # a and b tie on 10 and land on shards 1 and 2; c has no recorded time so it counts
    # as the median (10) and the tie break sends it to the empty shard 3.
    assert shards == [["a.py"], ["b.py"], ["c.py"]]
    # With no known times at all the fallback weight is 1.0 for every file.
    assert mod.assign(["x.py", "y.py"], 2, {}) == [["x.py"], ["y.py"]]


def test_shard_validates_the_one_based_range_and_defaults_to_the_recorded_timings():
    with pytest.raises(ValueError, match="shard 0 is not in 1..2"):
        mod.shard(["a.py"], 0, 2, timings={})
    with pytest.raises(ValueError, match="shard 3 is not in 1..2"):
        mod.shard(["a.py"], 3, 2, timings={})
    # No timings argument: reads the repo's real recorded times (read-only).
    assert mod.shard(mod.test_files(), 1, 1) == mod.test_files()


def test_refresh_sums_testcase_seconds_into_the_file_and_writes_the_timings(tmp_path, monkeypatch, capsys):
    timings_out = tmp_path / "timings.json"
    monkeypatch.setattr(mod, "TIMINGS", timings_out)
    junit_dir = tmp_path / "junit"
    junit_dir.mkdir()
    # classname is the dotted module path; the suffix walk must find the real collected
    # file tests/test_log_level_env.py, and two cases for it are summed.
    (junit_dir / "junit.xml").write_text(
        '<testsuite tests="3">'
        '<testcase classname="tests.test_log_level_env" name="a" time="1.25"/>'
        '<testcase classname="tests.test_log_level_env" name="b" time="0.75"/>'
        '<testcase classname="not.collected.anywhere" name="c" time="9.0"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    assert mod.refresh(junit_dir) == 0
    assert json.loads(timings_out.read_text()) == {"tests/test_log_level_env.py": 2.0}
    out = capsys.readouterr().out
    assert "1 of" in out and "files timed" in out


def test_refresh_fails_without_any_junit_xml(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "TIMINGS", tmp_path / "timings.json")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mod.refresh(empty) == 1
    assert f"no junit.xml under {empty}" in capsys.readouterr().err


def test_main_refreshes_or_prints_one_shard_or_rejects_the_arguments(tmp_path, monkeypatch, capsys):
    # --refresh goes through main() too; the timings write stays in tmp_path.
    junit_dir = tmp_path / "junit"
    junit_dir.mkdir()
    (junit_dir / "junit.xml").write_text(
        '<testsuite><testcase classname="tests.test_log_level_env" name="a" time="2.0"/></testsuite>',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "TIMINGS", tmp_path / "timings.json")
    assert mod.main(["ci_shard.py", "--refresh", str(junit_dir)]) == 0
    assert capsys.readouterr().out.startswith("1 of ")

    assert mod.main(["ci_shard.py", "1"]) == 2  # doc is printed on stderr
    assert mod.main(["ci_shard.py"]) == 2
    assert "Print the test files one CI shard runs" in capsys.readouterr().err

    all_files = mod.test_files()
    assert mod.main(["ci_shard.py", "1", "1"]) == 0
    assert capsys.readouterr().out.split() == all_files

    # More shards than files: the last shard is empty and the run fails loudly.
    assert mod.main(["ci_shard.py", "9999", "9999"]) == 1
    assert "shard 9999/9999 has no files" in capsys.readouterr().err
