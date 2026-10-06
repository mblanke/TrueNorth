"""The production register tracks every catalogue course exactly once, and only those."""

from __future__ import annotations

import csv
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "content/catalogue"


def _rows(name: str) -> list[dict[str, str]]:
    with (CATALOGUE / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_the_register_holds_each_catalogue_course_once():
    register = [r["catalogue_code"] for r in _rows("production_register.csv")]
    catalogue = [r["course_code"] for r in _rows("cyber_operator_programme.csv")]
    assert len(register) == len(set(register)) == 44
    assert sorted(register) == sorted(catalogue)


def test_patterns_waves_and_course_files_are_consistent():
    rows = _rows("production_register.csv")
    assert {r["pattern"] for r in rows} <= {"T", "P", "R"}
    assert {r["wave"] for r in rows} == {str(n) for n in range(1, 9)}
    for r in rows:
        if r["course_yaml"]:
            assert (ROOT / "content/courses" / r["course_yaml"]).is_file(), r["catalogue_code"]
        else:
            assert r["blockers"], f"{r['catalogue_code']} has no course file and no blocker recorded"
