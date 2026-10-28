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


KEYS = "ABCDA"  # a spread key: four different letters, none more than twice


def _question(module: int, i: int) -> dict:
    word = TOPIC_WORDS[module - 1]
    return {
        "question": f"Module {module} question {i}: which {word} behaviour applies in case {i}?",
        "options": [
            f"{letter}) {word} behaviour {name}"
            for letter, name in zip("ABCD", ("one", "two", "six", "ten"), strict=True)
        ],
        "answer": KEYS[i - 1],
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
        "lab": f"Lab: configure {word} on the range. Evidence: the command output. Pass: {word} verified on ws01.",
        "lab_range": {
            "template": "mini",
            "scenario": "drill",
            "steps": [
                {"hosts": ["jump01"], "action": "Log on to jump01 and open a terminal."},
                {"hosts": ["dc01", "ws01"], "action": f"Check {word} between dc01 and ws01."},
                {"hosts": ["ws01"], "action": f"Record the {word} result you see on ws01."},
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
        "answer_key_notes": [
            {
                "module": n,
                "notes": "Q1 A: right. Q2 B: right. Q3 C: right. Q4 D: right. Q5 A: right; B-D are distractors.",
            }
            for n in range(1, 7)
        ],
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


# -- house style (tools/taz/courses/ARC2-STYLE.md) ----------------------------------------


def _question_set(doc: dict, n: int, keys: str) -> None:
    for q, key in zip(_m(doc, n)["quiz"]["questions"], keys, strict=True):
        q["answer"] = key


STYLE_MUTATIONS = {
    "objectives": lambda d: _m(d)["objectives"].append("Understand how routing works on the range."),
    "wording-framework": lambda d: _m(d)["topics"].append("Mapped to a dcwf work role."),
    "wording-nice": lambda d: _m(d)["topics"].append("Aligned to the NICE framework."),
    "wording-defang": lambda d: _m(d)["topics"].append("Run Invoke-Mimikatz against the controller."),
    "wording-learner": lambda d: _m(d)["topics"].append("What every learner should log."),
    "quiz_style-bunched": lambda d: _question_set(d, 1, "BBBBB"),
    "quiz_style-two-letters": lambda d: _question_set(d, 1, "ABABA"),
    "quiz_style-all-of-above": lambda d: _m(d)["quiz"]["questions"][0]["options"].__setitem__(3, "D) All of the above"),
    "quiz_style-letter-options": lambda d: _m(d)["quiz"]["questions"][0]["options"].__setitem__(3, "D) Both A and B"),
    "quiz_style-repeated-option": lambda d: _m(d)["quiz"]["questions"][0]["options"].__setitem__(
        1, "B) routing behaviour one"
    ),
    "quiz_style-threshold": lambda d: _m(d)["quiz"].update(pass_threshold=80),
    "lab_style-evidence": lambda d: _m(d).update(lab="Lab: configure routing on the range."),
    "lab_style-too-few-steps": lambda d: _m(d)["lab_range"]["steps"].pop(),
    "lab_style-too-many-steps": lambda d: _m(d)["lab_range"]["steps"].extend(
        [{"hosts": ["ws01"], "action": f"Step {i} on ws01."} for i in range(7)]
    ),
}


@pytest.mark.parametrize("name", sorted(STYLE_MUTATIONS))
def test_each_house_style_rule_fails_its_own_breakage(tree, name):
    failed = _failed(tree, _write(tree, _mutated(STYLE_MUTATIONS[name])))
    check = name.split("-")[0]
    assert check in failed
    assert failed <= {check, "schema", "quiz", "quiz_terms", "instructor"}  # a re-keyed quiz stales the pack


def test_the_longest_option_tell_is_flagged_and_a_mixed_quiz_is_not(tree):
    def tell(d):
        for q in _m(d)["quiz"]["questions"][:4]:
            keyed = "ABCD".index(q["answer"])
            q["options"][keyed] += " and a long qualification no distractor has"

    assert "quiz_style" in _failed(tree, _write(tree, _mutated(tell)))

    def three(d):
        for q in _m(d)["quiz"]["questions"][:3]:
            keyed = "ABCD".index(q["answer"])
            q["options"][keyed] += " and a long qualification no distractor has"

    assert _failed(tree, _write(tree, _mutated(three))) == set()


def test_a_stem_copied_from_another_course_fails(tree):
    other = _course()
    other["course_code"] = "C104"
    (tree / "content/courses").mkdir(parents=True, exist_ok=True)
    (tree / "content/courses/c104-other.yaml").write_text(yaml.safe_dump(other), encoding="utf-8")
    assert "quiz_style" in _failed(tree, _write(tree))


def test_comments_and_the_banner_may_say_learner(tree):
    banner = BANNER.replace("not for CAF learners", "not for learners or members")
    assert _failed(tree, _write(tree, banner=banner)) == set()


def test_a_defanged_line_with_the_placeholder_is_allowed(tree):
    ok = _mutated(
        lambda d: _m(d)["topics"].append("AUTHOR-REQUIRED: a cleared author supplies the Invoke-Something line.")
    )
    assert _failed(tree, _write(tree, ok)) == set()


@pytest.mark.parametrize(
    "breakage",
    [
        lambda p: p["timings"][0].update(minutes=45),
        lambda p: p["answer_key_notes"][0].update(notes="Q1 B: wrong letter. Q2 B. Q3 C. Q4 D. Q5 A."),
        lambda p: p["answer_key_notes"][0].update(notes="Q1 A: right. Q2 B. Q3 C. Q4 D."),
        lambda p: p["answer_key_notes"][0].update(notes="A is keyed; B-D are distractors."),
        lambda p: p["common_faults"][0].update(fault="Every learner forgets the jump host."),
    ],
    ids=["timing-differs", "wrong-letter", "missing-question", "no-question-numbers", "learner-in-pack"],
)
def test_instructor_pack_style_rules(tree, breakage):
    pack = _pack()
    breakage(pack)
    assert _failed(tree, _write(tree, pack=pack)) <= {"instructor", "wording"}
    assert _failed(tree, _write(tree, pack=pack))


# -- the ARC2 reference set (tools/taz/courses/reference) ---------------------------------

REFERENCE = ROOT / "tools/taz/courses/reference"


def test_the_reference_set_stays_out_of_content_and_is_labelled_an_exemplar():
    files = sorted(REFERENCE.glob("*.yaml"))
    assert {f.name for f in files} == {
        "exemplar-arc2-c105.course.yaml",
        "exemplar-arc2-c105.instructor-pack.yaml",
        "exemplar-lab-module.yaml",
    }
    for f in files:
        assert f.read_text(encoding="utf-8").startswith("# EXEMPLAR - NOT FOR PUBLICATION"), f.name
    # scripts/load_content.py reads content/courses/*.yaml only; nothing of ours may sit there.
    assert not any(p.name.startswith("exemplar") for p in (ROOT / "content").rglob("*.yaml"))


def test_the_arc2_exemplar_course_and_pack_meet_the_style_rules():
    text = (REFERENCE / "exemplar-arc2-c105.course.yaml").read_text(encoding="utf-8")
    pack_text = (REFERENCE / "exemplar-arc2-c105.instructor-pack.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert cc._schema_errors(cc.COURSE_SCHEMA, doc) == []
    assert cc._schema_errors(cc.PACK_SCHEMA, yaml.safe_load(pack_text)) == []
    assert cc.check_objectives(doc) == []
    assert cc.check_wording(text, doc, pack_text) == []
    assert cc.check_quiz(doc) == [] and cc.check_quiz_terms(doc) == [] and cc.check_refs(ROOT, doc) == []
    assert cc.check_quiz_style(ROOT, Path("exemplar-arc2-c105.course.yaml"), doc) == []


def test_the_arc2_exemplar_pack_agrees_with_the_exemplar_course(tmp_path):
    doc_path = tmp_path / "e.yaml"
    doc_path.write_text((REFERENCE / "exemplar-arc2-c105.course.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "instructor").mkdir()
    (tmp_path / "instructor/e.yaml").write_text(
        (REFERENCE / "exemplar-arc2-c105.instructor-pack.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert cc.check_instructor(doc_path, yaml.safe_load(doc_path.read_text(encoding="utf-8"))) == []


def test_the_lab_exemplar_module_meets_every_module_level_check():
    module = yaml.safe_load((REFERENCE / "exemplar-lab-module.yaml").read_text(encoding="utf-8"))
    doc = {"modules": [module]}
    assert [p for p in cc.check_modules(doc) if "expected 6" not in p] == []  # one module of the six
    for fn in (cc.check_lab_range, cc.check_hosts, cc.check_refs):
        assert fn(ROOT, doc) == [], fn.__name__
    for fn in (cc.check_quiz, cc.check_quiz_terms, cc.check_objectives, cc.check_lab_style):
        assert fn(doc) == [], fn.__name__
    assert cc.check_quiz_style(ROOT, Path("exemplar-lab-module.yaml"), doc) == []
    assert cc._schema_errors(cc.COURSE_SCHEMA, {**_course(), "modules": [module] * 6}) == []


def test_the_style_guide_points_only_at_files_that_exist():
    guide = (ROOT / "tools/taz/courses/ARC2-STYLE.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 420
    import re

    for rel in sorted(set(re.findall(r"`((?:\.claude|docs|tools|scripts|tests|content)/[A-Za-z0-9_./*-]+)", guide))):
        rel = rel.rstrip(".,:;)")
        if "*" in rel or rel.endswith("/"):
            continue
        assert (ROOT / rel).exists(), f"ARC2-STYLE.md points at {rel}, which does not exist"


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


def test_a_course_without_qsp_code_passes(tree):
    """Courses outside a qualification (the upskilling courses) omit qsp_code."""
    assert _failed(tree, _write(tree, _mutated(lambda d: d.pop("qsp_code")))) == set()


@pytest.mark.parametrize("bad", ["alj", "", None])
def test_a_malformed_qsp_code_still_fails_the_schema(tree, bad):
    assert "schema" in _failed(tree, _write(tree, _mutated(lambda d: d.update(qsp_code=bad))))
