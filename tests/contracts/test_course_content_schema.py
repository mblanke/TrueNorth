"""Every authored course file conforms to ``docs/interfaces/course-content.schema.json``,
and no two of them are the same course.

The schema is the structure ``app.course_content_ingest.parse_course_content`` reads, with
the value domains ``tools/arc2/qa.py`` admits. Until it existed the course YAML had no
structural check: the ingest maps an unknown ``content_type`` to ``reading`` and ignores
unknown keys, so a typo (``objective:``, ``content_type: lab``) loaded quietly as something
else. Policy rules that need the reference library or the whole corpus stay in
``tests/api/test_course_content_ingest.py``; the cross-field rules JSON Schema cannot
express are asserted here.
"""

from __future__ import annotations

import copy
import csv
import itertools
import json
import re
from pathlib import Path

import pytest
import yaml
from app.course_content_ingest import parse_course_content
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "docs/interfaces/course-content.schema.json"
COURSE_DIR = ROOT / "content/courses"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
COURSE_FILES = sorted(COURSE_DIR.glob("*.yaml"))

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft7Validator(SCHEMA)

# Two files may share a title only when a recorded decision says they are different
# courses. C206 and C207 were split by pattern on 2026-10-05 (docs/arc2-44-course-programme.md
# §10, content/catalogue/production_register.csv): C206 teaches IR concepts, roles and
# process (theory); C207 is an evidence-based tabletop that builds on it (practical). Their
# bodies share nothing (see test_no_two_course_bodies_are_near_duplicates); the shared
# title is the catalogue's, and renaming C207 is the programme owner's open decision.
SHARED_TITLES = {"Incident Response Foundations": {"C206", "C207"}}
# C304's draft predates the naming convention and keeps its recovered title; decided
# 2026-10-05 to draft it from this file under the catalogue title at outline review.
NAMING_EXCEPTIONS = {"iot-security-foundations.yaml": "C304"}
# Jaccard similarity of 5-word shingles over every module's title, objectives, topics,
# lab and questions. The most similar distinct pair in the library scores under 0.01;
# copy-paste filler with light edits scores well above this.
NEAR_DUPLICATE = 0.25


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _errors(doc: dict) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    ]


def _catalogue() -> dict[str, str]:
    with CATALOGUE.open(newline="", encoding="utf-8") as fh:
        return {r["course_code"]: r["course_title"] for r in csv.DictReader(fh)}


# -- the schema --------------------------------------------------------------------


def test_the_schema_is_a_valid_draft7_schema():
    Draft7Validator.check_schema(SCHEMA)


def test_the_corpus_is_not_empty():
    assert len(COURSE_FILES) == 44


@pytest.mark.parametrize("path", COURSE_FILES, ids=lambda p: p.name)
def test_every_course_file_conforms_to_the_schema(path):
    errors = _errors(_load(path))
    assert not errors, f"{path.name}:\n  " + "\n  ".join(errors)


@pytest.mark.parametrize("path", COURSE_FILES, ids=lambda p: p.name)
def test_every_course_file_parses_as_the_ingest_reads_it(path):
    """The schema and the parser agree: what validates also parses, module for module,
    with the declared content type kept (the parser would otherwise default it)."""
    doc = _load(path)
    parsed = parse_course_content(path.read_text(encoding="utf-8"))
    assert parsed["course_code"] == doc["course_code"]
    assert [m["ordinal"] for m in parsed["modules"]] == [m["ordinal"] for m in doc["modules"]]
    for raw, m in zip(doc["modules"], parsed["modules"], strict=True):
        assert m["content_type"].value == raw["content_type"]
        assert len(m["questions"]) == len((raw.get("quiz") or {}).get("questions") or [])
        assert all(q["correct"] for q in m["questions"]), f"{path.name} module {m['ordinal']} has a keyless question"


@pytest.mark.parametrize("path", COURSE_FILES, ids=lambda p: p.name)
def test_ordinals_are_contiguous_and_minutes_add_up(path):
    doc = _load(path)
    ordinals = [m["ordinal"] for m in doc["modules"]]
    assert ordinals == list(range(1, len(ordinals) + 1)), f"{path.name}: ordinals {ordinals}"
    for m in doc["modules"]:
        parts = m.get("minutes_breakdown")
        if parts:
            assert sum(parts.values()) == m["duration_minutes"], f"{path.name} module {m['ordinal']}"


@pytest.mark.parametrize("path", COURSE_FILES, ids=lambda p: p.name)
def test_file_name_and_title_follow_the_catalogue(path):
    doc = _load(path)
    code = doc["course_code"]
    catalogue = _catalogue()
    assert code in catalogue, f"{path.name}: {code} is not in the catalogue"
    if path.name in NAMING_EXCEPTIONS:
        assert NAMING_EXCEPTIONS[path.name] == code
        return
    assert path.name.startswith(re.sub(r"\s+", "-", code.lower()) + "-"), path.name
    assert doc["title"] == catalogue[code], f"{path.name}: title {doc['title']!r} != catalogue {catalogue[code]!r}"


# -- duplicates --------------------------------------------------------------------


def test_course_codes_are_unique():
    seen: dict[str, str] = {}
    for path in COURSE_FILES:
        code = _load(path)["course_code"]
        assert code not in seen, f"{code} is in both {seen[code]} and {path.name}"
        seen[code] = path.name


def test_titles_are_unique_unless_a_decision_says_otherwise():
    by_title: dict[str, set[str]] = {}
    for path in COURSE_FILES:
        doc = _load(path)
        by_title.setdefault(doc["title"].strip(), set()).add(doc["course_code"])
    shared = {t: codes for t, codes in by_title.items() if len(codes) > 1}
    assert shared == SHARED_TITLES


def test_module_titles_are_unique_within_a_course():
    for path in COURSE_FILES:
        titles = [m["title"].strip().lower() for m in _load(path)["modules"]]
        assert len(titles) == len(set(titles)), path.name


def _shingles(doc: dict, k: int = 5) -> set[str]:
    parts: list[str] = []
    for m in doc["modules"]:
        parts += [m["title"], *m["objectives"], *m["topics"], m.get("lab") or ""]
        parts += [q["question"] for q in (m.get("quiz") or {}).get("questions") or []]
    words = " ".join(parts).lower().split()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def test_no_two_course_bodies_are_near_duplicates():
    shingles = {p.name: _shingles(_load(p)) for p in COURSE_FILES}
    close = []
    for a, b in itertools.combinations(shingles, 2):
        score = len(shingles[a] & shingles[b]) / len(shingles[a] | shingles[b])
        if score >= NEAR_DUPLICATE:
            close.append(f"{a} ~ {b}: {score:.2f}")
    assert not close, "near-duplicate course bodies:\n  " + "\n  ".join(close)


def test_c206_and_c207_are_distinct_courses():
    """They share a title, not content: no module title, objective or question in common."""
    a, b = (_load(COURSE_DIR / f"c20{n}-incident-response-foundations.yaml") for n in (6, 7))

    def items(doc: dict) -> set[str]:
        out = set()
        for m in doc["modules"]:
            out |= {m["title"].lower(), *(o.lower() for o in m["objectives"])}
            out |= {q["question"].lower() for q in m["quiz"]["questions"]}
        return out

    assert items(a).isdisjoint(items(b))


# -- the schema refuses what the ingest would mis-load (error paths) ---------------


def _base() -> dict:
    return _load(COURSE_DIR / "c105-foundations-of-cybersecurity.yaml")


def _mutate(fn) -> dict:
    doc = copy.deepcopy(_base())
    fn(doc)
    return doc


def _m0(doc: dict) -> dict:
    return doc["modules"][0]


MUTATIONS = {
    "no course_code": lambda d: d.pop("course_code"),
    "course_code not a catalogue or ARC2 code": lambda d: d.update(course_code="c105"),
    "published from the file": lambda d: d.update(is_published=True),
    "status published": lambda d: d.update(status="published"),
    "sourced provenance claimed": lambda d: d.update(provenance="sourced"),
    "QSP-TODO placeholder as qsp_code": lambda d: d.update(qsp_code="QSP-TODO"),
    "duration_hours as text": lambda d: d.update(duration_hours="100"),
    "unknown top-level key": lambda d: d.update(competencies=["DE-RS-01"]),
    "no modules": lambda d: d.update(modules=[]),
    "unknown module key (typo)": lambda d: _m0(d).update(objective=["x"]),
    "content_type the ingest would default": lambda d: _m0(d).update(content_type="lab"),
    "empty objectives": lambda d: _m0(d).update(objectives=[]),
    "blank topic": lambda d: _m0(d).update(topics=["  "]),
    "no refs": lambda d: _m0(d).update(refs=[]),
    "pass_threshold over 100": lambda d: _m0(d).update(pass_threshold=170),
    "range module without a lab": lambda d: _m0(d).pop("lab"),
    "range module with a blank lab": lambda d: _m0(d).update(lab=""),
    "theory module with a lab": lambda d: _m0(d).update(activity="theory"),
    "unknown activity": lambda d: _m0(d).update(activity="workshop"),
    "PO_TODO placeholder bound": lambda d: _m0(d).update(po={"qsp_code": "ALJQ", "po_code": "PO_TODO"}),
    "three options": lambda d: _m0(d)["quiz"]["questions"][0]["options"].pop(),
    "answer outside A-D": lambda d: _m0(d)["quiz"]["questions"][0].update(answer="E"),
    "multi-letter answer": lambda d: _m0(d)["quiz"]["questions"][0].update(answer="A,C"),
    "quiz without questions": lambda d: _m0(d)["quiz"].update(questions=[]),
    "minutes as text": lambda d: _m0(d).update(minutes_breakdown={"contact": "45"}),
}


def test_the_base_document_is_valid():
    assert _errors(_base()) == []


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_the_schema_refuses(name):
    assert _errors(_mutate(MUTATIONS[name])), f"the schema accepted: {name}"


def test_a_theory_module_without_a_lab_is_valid():
    def theory(d: dict) -> None:
        _m0(d).update(activity="theory", practice=["Classify each event."])
        _m0(d).pop("lab")

    assert _errors(_mutate(theory)) == []


def test_arc2_extension_keys_are_valid():
    """ARC² course files carry these; the ingest ignores what it does not read."""

    def arc2(d: dict) -> None:
        d.update(course_code="ARC2-C105", difficulty="foundation", status="proposed")
        _m0(d).update(
            duration_minutes=150,
            minutes_breakdown={"contact": 45, "reading": 40, "practice": 45, "assessment": 20},
            tools=["Python 3"],
            source_gaps=["Saltzer and Schroeder"],
            critical_events=["CE-01"],
            lab_parts_minutes={"A orient": 15},
        )

    assert _errors(_mutate(arc2)) == []
