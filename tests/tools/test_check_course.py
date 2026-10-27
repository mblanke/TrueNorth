"""scripts/check_course.py: the oracle a TAZ-drafted course must pass.

Each test builds a small content tree (one range template, two scenarios, a reference
library) with a course that passes every check, then breaks exactly one thing and expects
exactly that check to fail. The 44 real course files are deliberately not run here: they
are drafts that do not pass yet, and TAZ brings them up one at a time.
"""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_course", ROOT / "scripts" / "check_course.py")
cc = importlib.util.module_from_spec(_spec)
sys.modules["check_course"] = cc
_spec.loader.exec_module(cc)

BANNER = (
    "# C103 - Intro to Networking - DRAFT course content.\n"
    "# Unsourced generated draft: not validated by Standards, not for CAF learners.\n"
    "# `refs` are keys into content/catalogue/references.yaml.\n\n"
)
NOTES = ["Generated draft courseware.", "Unpublished pending Standards review."]
TOPIC_WORDS = ["routing", "firewall", "subnet", "logging", "switch", "tunnel"]


def _question(module: int, i: int) -> dict:
    word = TOPIC_WORDS[module - 1]
    return {
        "question": f"Module {module} question {i}: which {word} behaviour applies in case {i}?",
        "options": [f"A) {word} option one", "B) option two", "C) option three", "D) option four"],
        "answer": "A",
    }


def _module(n: int) -> dict:
    word = TOPIC_WORDS[n - 1]
    return {
        "ordinal": n,
        "title": f"Module {n} on {word}",
        "content_type": "reading",
        "duration_minutes": 60,
        "is_required": True,
        "pass_threshold": 70,
        "objectives": [f"Explain {word} decisions on a small network."],
        "topics": [f"{word.capitalize()} fundamentals."],
        "lab": f"Lab: configure {word} on the range and verify it.",
        "lab_range": {
            "template": "mini",
            "scenario": "drill",
            "steps": [
                {"hosts": ["jump01"], "action": "Log on to jump01 and open a terminal."},
                {"hosts": ["dc01", "ws01"], "action": f"Check {word} between dc01 and ws01."},
            ],
        },
        "refs": ["nist-sp-800-41r1"],
        "quiz": {"title": f"Quiz {n}", "pass_threshold": 70, "questions": [_question(n, i) for i in range(1, 6)]},
    }


def _course() -> dict:
    return {
        "course_code": "C103",
        "title": "Intro to Networking",
        "version": "1.0",
        "difficulty": "beginner",
        "duration_hours": 100,
        "provenance": "unsourced",
        "status": "draft",
        "is_published": False,
        "readiness": "draft",
        "qsp_code": "ALJQ",
        "source": {"origin": "test fixture", "notes": list(NOTES)},
        "modules": [_module(n) for n in range(1, 7)],
    }


def _pack() -> dict:
    return {
        "course_code": "C103",
        "audience": "staff_only",
        "overview": "Six modules; each ends on the mini range.",
        "timings": [{"module": n, "minutes": 60, "plan": "20 teach, 30 lab, 10 quiz"} for n in range(1, 7)],
        "answer_key_notes": [{"module": n, "notes": "A is keyed; B-D are distractors."} for n in range(1, 7)],
        "lab_setup": [{"module": n, "setup": "Revert the mini range to its baseline."} for n in range(1, 7)],
        "common_faults": [{"module": 1, "fault": "jump01 refuses SSH", "remedy": "Revert jump01."}],
    }


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    files = {
        "content/ranges/mini/template.yaml": {"id": "mini", "nodes": [{"id": h} for h in ("jump01", "dc01", "ws01")]},
        "content/ranges/other/template.yaml": {"id": "other", "nodes": [{"id": "web01"}]},
        "content/scenarios/drill/scenario.yaml": {"name": "drill", "range_template": "mini"},
        "content/scenarios/elsewhere/scenario.yaml": {"name": "elsewhere", "range_template": "other"},
        "content/catalogue/references.yaml": {"references": {"nist-sp-800-41r1": {"title": "Firewalls"}}},
    }
    for rel, doc in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


def _write(tree: Path, course: dict | None = None, pack: dict | None = None, *, banner: str = BANNER) -> Path:
    path = tree / "content/courses/c103-intro-to-networking.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(banner + yaml.safe_dump(course or _course(), sort_keys=False), encoding="utf-8")
    if pack is not False:
        (path.parent / "instructor").mkdir(exist_ok=True)
        (path.parent / "instructor" / path.name).write_text(yaml.safe_dump(pack or _pack()), encoding="utf-8")
    return path


def _failed(tree: Path, path: Path, **kw) -> set[str]:
    res = cc.check_file(path, root=tree, base=kw.get("base"), held=kw.get("held", True))
    return {ln.split()[1] for ln in res.lines if ln.strip().startswith("FAIL")}


def _mutated(fn) -> dict:
    doc = copy.deepcopy(_course())
    fn(doc)
    return doc


def _m(doc: dict, n: int = 1) -> dict:
    return doc["modules"][n - 1]


def test_a_complete_course_passes_every_check(tree):
    assert _failed(tree, _write(tree)) == set()


def test_the_cli_exits_zero_on_pass_and_one_on_fail(tree, capsys):
    path = _write(tree)
    assert cc.main(["--root", str(tree), str(path)]) == 0
    assert "PASS" in capsys.readouterr().out
    _write(tree, _mutated(lambda d: d.pop("readiness")))
    assert cc.main(["--root", str(tree), str(path)]) == 1
    assert "FAIL readiness" in capsys.readouterr().out


MUTATIONS = {
    "schema": lambda d: _m(d).update(content_type="lab"),
    "readiness": lambda d: d.update(readiness="sme_signed"),
    "banner": lambda d: d["source"].update(notes=NOTES[:1]),
    "modules": lambda d: d["modules"].pop(),
    "lab_range": lambda d: _m(d)["lab_range"].update(template="nowhere"),
    "hosts": lambda d: _m(d)["lab_range"]["steps"][0].update(hosts=["dc99"]),
    "refs": lambda d: _m(d).update(refs=["invented-2026"]),
    "quiz": lambda d: _m(d)["quiz"]["questions"].pop(),
    "quiz_terms": lambda d: _m(d, 2)["quiz"]["questions"][0].update(
        question="Which colour is the sky on a clear day?", options=["A) Blue", "B) Green", "C) Red", "D) Grey"]
    ),
}


@pytest.mark.parametrize("check", sorted(MUTATIONS))
def test_each_check_fails_its_own_breakage(tree, check):
    failed = _failed(tree, _write(tree, _mutated(MUTATIONS[check])))
    assert check in failed
    # A structural breakage may trip the schema too (and a dropped module leaves the pack
    # covering one too many); nothing unrelated fails.
    assert failed <= {check, "schema"} | ({"instructor"} if check == "modules" else set())


def test_a_module_without_a_lab_range_fails(tree):
    assert "modules" in _failed(tree, _write(tree, _mutated(lambda d: _m(d, 3).pop("lab_range"))))


def test_a_module_without_a_lab_brief_fails(tree):
    assert "modules" in _failed(tree, _write(tree, _mutated(lambda d: _m(d).update(lab=""))))


def test_an_unknown_scenario_fails(tree):
    assert "lab_range" in _failed(tree, _write(tree, _mutated(lambda d: _m(d)["lab_range"].update(scenario="nope"))))


def test_a_scenario_for_another_range_fails(tree):
    failed = _failed(tree, _write(tree, _mutated(lambda d: _m(d)["lab_range"].update(scenario="elsewhere"))))
    assert "lab_range" in failed


def test_a_host_named_only_in_the_action_text_must_exist(tree):
    failed = _failed(
        tree,
        _write(tree, _mutated(lambda d: _m(d)["lab_range"]["steps"][1].update(action="Pivot from ws01 to web01."))),
    )
    assert failed == {"hosts"}


def test_host_shaped_words_that_are_not_hosts_are_ignored(tree):
    fine = "Check the x64 build and the SHA-256 digest on ws01 over TLS 1.3."
    assert _failed(tree, _write(tree, _mutated(lambda d: _m(d)["lab_range"]["steps"][1].update(action=fine)))) == set()


def test_options_must_be_lettered_in_order(tree):
    def unlettered(d):
        _m(d)["quiz"]["questions"][0]["options"] = ["routing one", "B) two", "C) three", "D) four"]

    assert "quiz" in _failed(tree, _write(tree, _mutated(unlettered)))


def test_a_duplicate_question_across_modules_fails(tree):
    def dup(d):
        _m(d, 2)["quiz"]["questions"][0] = copy.deepcopy(_m(d, 1)["quiz"]["questions"][0])

    assert "quiz" in _failed(tree, _write(tree, _mutated(dup)))


def test_the_banner_comment_must_stay(tree):
    assert "banner" in _failed(tree, _write(tree, banner="# C103 course\n\n"))


def test_is_published_true_fails(tree):
    assert {"banner", "schema"} <= _failed(tree, _write(tree, _mutated(lambda d: d.update(is_published=True))))


def test_a_missing_instructor_pack_fails(tree):
    assert _failed(tree, _write(tree, pack=False)) == {"instructor"}


@pytest.mark.parametrize(
    "breakage",
    [
        lambda p: p.pop("common_faults"),
        lambda p: p.update(audience="students"),
        lambda p: p.update(course_code="C104"),
        lambda p: p["timings"].pop(),
        lambda p: p["answer_key_notes"].pop(0),
        lambda p: p["lab_setup"].append({"module": 1, "setup": "twice"}),
    ],
    ids=["no-faults", "not-staff-only", "wrong-course", "timing-missing", "notes-missing", "setup-repeated"],
)
def test_an_incomplete_instructor_pack_fails(tree, breakage):
    pack = _pack()
    breakage(pack)
    assert _failed(tree, _write(tree, pack=pack)) == {"instructor"}


@pytest.mark.parametrize("code", sorted(cc.HELD_CODES))
def test_held_courses_are_refused(tree, code):
    path = _write(tree, _mutated(lambda d: d.update(course_code=code)))
    assert _failed(tree, path) == {"held"}
    assert "held" not in _failed(tree, path, held=False)


def test_the_held_list_names_seven_real_files():
    assert len(cc.HELD_FILES) == len(cc.HELD_CODES) == 7
    for name in cc.HELD_FILES:
        doc = yaml.safe_load((ROOT / "content/courses" / name).read_text(encoding="utf-8"))
        assert doc["course_code"] in cc.HELD_CODES, name


def test_the_queue_is_the_37_unheld_courses():
    """One course per line: its path, then the suggested range template (a hint TAZ checks)."""
    rows = [ln.split() for ln in (ROOT / "tools/taz/courses/queue.txt").read_text(encoding="utf-8").splitlines()]
    queue = [Path(r[0]) for r in rows]
    every = {p.name for p in (ROOT / "content/courses").glob("*.yaml")}
    assert all(p.parent == Path("content/courses") and (ROOT / p).is_file() for p in queue)
    assert len(queue) == len({p.name for p in queue}) == 37
    assert {p.name for p in queue} == every - cc.HELD_FILES
    assert all(len(r) == 2 and (ROOT / "content/ranges" / r[1] / "template.yaml").is_file() for r in rows)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def test_base_notes_and_signed_readiness_are_kept(tree):
    path = _write(tree, _mutated(lambda d: d.update(readiness="sme_signed")))
    _git(tree, "init", "-q")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base")
    # Already signed on the base: keeping it is not an author's claim.
    assert _failed(tree, path, base="HEAD") == set()

    def dropped_note(d):
        d.update(readiness="sme_signed")
        d["source"]["notes"].insert(0, "Extra note")

    _write(tree, _mutated(dropped_note))
    _git(tree, "commit", "-qam", "note added")
    _write(tree, _mutated(lambda d: d.update(readiness="sme_signed")))
    assert _failed(tree, path, base="HEAD") == {"banner"}


def _make_card():
    spec = importlib.util.spec_from_file_location("make_card", ROOT / "tools/taz/courses/make_card.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_make_card_scopes_one_course_and_its_pack(tmp_path):
    card = _make_card().card_for("content/courses/c103-intro-to-networking.yaml", repo_root=str(tmp_path))
    assert card["allowed_paths"] == [
        "content/courses/c103-intro-to-networking.yaml",
        "content/courses/instructor/c103-intro-to-networking.yaml",
    ]
    assert card["repo_root"] == str(tmp_path.resolve())
    assert "scripts/check_course.py" in card["oracle_tests"]


@pytest.mark.parametrize(
    "bad",
    [
        "content/courses/c302-red-team-fundamentals.yaml",
        "content/courses/*.yaml",
        "content/courses/instructor/c103-intro-to-networking.yaml",
        "/abs/c103.yaml",
    ],
)
def test_make_card_refuses_held_courses_and_odd_paths(bad, capsys):
    assert _make_card().main([bad]) == 1
    assert "make_card:" in capsys.readouterr().err
