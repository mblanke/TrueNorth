#!/usr/bin/env python3
"""The course oracle: is one authored course file technically proven?

    python scripts/check_course.py content/courses/c103-intro-to-networking.yaml [...]
    python scripts/check_course.py --base origin/main content/courses/c103-*.yaml

Exit 0 when every check passes for every file, 1 otherwise (2 on bad arguments). Each
check prints one PASS or FAIL line with its reasons. This is the bar TAZ's coder must clear
before its foreman sends a course patch, and the bar Claude re-runs before setting
`readiness: technically_proven` (tools/taz/courses/AGENTS-courses.md).

Checks, per course file:

  held          the course is not one of the seven held for cleared authors (on by default;
                --no-held turns it off for a cleared author's own run).
  schema        the file validates against docs/interfaces/course-content.schema.json.
  readiness     `readiness` is set, and is not `sme_signed` unless the base already was
                (only SME/Standards sign-off sets it).
  banner        the provenance banner (the leading "Unsourced ... not validated by
                Standards" comment), `provenance: unsourced`, `is_published: false` and the
                "Unpublished pending Standards review." note are kept; with --base, every
                banner line and source note from the base revision is still there.
  modules       exactly 6 modules, each with objectives, topics, a lab brief and a lab_range.
  lab_range     the template resolves to content/ranges/<id>/template.yaml; a scenario, if
                given, resolves to content/scenarios/<id>/scenario.yaml and names the same
                range_template.
  hosts         every host a step lists is a node id in that template, and a step's action
                text names no host-shaped token (dc01, ws-01, v-ws01) that is not.
  refs          every module ref is a key in content/catalogue/references.yaml.
  quiz          each module's quiz has 5 questions with 4 options lettered "A) " to "D) "
                in order and an answer among A-D; no question stem repeats in the course.
  quiz_terms    HEURISTIC, documented as such: each question shares at least one key term
                with its module's objectives or topics. Key terms are lower-cased words of
                4+ letters from the stem and the keyed option, minus stop words, compared on
                their first 5 letters (a crude stem), plus acronyms written in capitals. It catches a question about another
                module or another course; it does not prove the key is correct, which stays
                Claude's and the SME's review.
  instructor    content/courses/instructor/<file stem>.yaml exists, validates against
                docs/interfaces/course-instructor-pack.schema.json, names the same
                course_code, and has timings, answer-key notes and lab setup for every
                module, plus at least one common fault. Each module's timing equals its
                duration_minutes, and its answer-key notes give "Q1 <letter>" .. "Q5 <letter>"
                with the letter the quiz keys.

House-style checks (tools/taz/courses/ARC2-STYLE.md). Where ARC² applies the rule itself the
source is named; the rest are TAZ house rules, marked (house):

  objectives    no objective starts understand / be aware / appreciate / know / be familiar
                (tools/arc2/check.py UNOBSERVABLE, rule objective.observable_verb), and each is
                at least 10 characters.
  wording       the course text and pack carry no framework token (tools/arc2/cmi5.py
                FRAMEWORK_TOKENS, bare NICE), none of the miscited publications (tools/arc2/qa.py
                MISCITED), no defanged-pattern line (qa.py DEFANG_PATTERNS) unless it carries
                AUTHOR-REQUIRED, and no "Learner"/"Member" for a Student (house).
  quiz_style    module and quiz pass_threshold agree (arc2 cmi5.mastery_score); no stem is
                already in another content/courses file (qa.py course.duplicate_stem); no option
                is "all/none of the above" or "both X and Y" (house); no option repeats within a
                question (house); the keys are spread, at most 2 of a letter per module and at
                least 3 different letters (house); HEURISTIC: the keyed option is not the
                strictly longest in 4 or more of the 5 questions (house).
  lab_style     (house) each `lab` reads "Lab: ... Evidence: ... Pass: ...", and each lab_range has
                3 to 8 steps with an action.

Standard library plus PyYAML and jsonschema (both in requirements-test.txt).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[1]
COURSE_SCHEMA = ROOT / "docs/interfaces/course-content.schema.json"
PACK_SCHEMA = ROOT / "docs/interfaces/course-instructor-pack.schema.json"

# Held for cleared authors (owner decision 2026-10-27). TAZ never drafts these.
HELD_CODES = frozenset({"C302", "C305", "C306", "RMC C202", "RMC C205", "RMC C207", "RMC C211"})
HELD_FILES = frozenset(
    {
        "c302-red-team-fundamentals.yaml",
        "c305-advanced-malware-reverse-engineering.yaml",
        "c306-lab-full-scale-adversary-emulation.yaml",
        "rmc-c202-threat-hunting-advanced.yaml",
        "rmc-c205-red-team-operations.yaml",
        "rmc-c207-advanced-malware-analysis.yaml",
        "rmc-c211-advanced-iot-security.yaml",
    }
)
MODULE_COUNT = 6
QUESTIONS_PER_QUIZ = 5
LETTERS = ("A", "B", "C", "D")
BANNER = re.compile(r"^#.*\bUnsourced\b.*not validated by Standards", re.I)
UNPUBLISHED_NOTE = "Unpublished pending Standards review."
# Host-shaped words: letters (with hyphens) then exactly two digits, as range node ids are.
HOST_SHAPE = re.compile(r"(?<![\w.-])([a-z]+(?:-[a-z]+)*-?[0-9]{2})(?![\w-])")
# From tools/arc2/check.py (objective.observable_verb), tools/arc2/cmi5.py and tools/arc2/qa.py.
# Copied, not imported: this script must run on a bare checkout with only PyYAML and jsonschema.
UNOBSERVABLE = re.compile(r"^\s*(understand|be aware|appreciate|know|be familiar)\b", re.IGNORECASE)
FRAMEWORK_TOKENS = ("de-rs-", "dcwf", "cc-3", "nice_dcwf", "csf sub-categor")
MISCITED = ("8286", "IoT Threat Landscape", "T9 (Insecure Network Services)")
DEFANG_PATTERNS = (
    ("powershell encoded command", re.compile(r"(?i)(?<![\w-])-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{16,}")),
    ("base64 run", re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")),
    ("hex run", re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}|(?<![0-9a-fA-F])(?:[0-9a-fA-F]{2}[ :]){15,}[0-9a-fA-F]{2}")),
    ("metasploit", re.compile(r"(?i)\bmsfvenom\b|\bmsfconsole\b")),
    ("credential dumper", re.compile(r"(?i)\bmimikatz\b|\bsekurlsa\b")),
    ("Invoke-* cmdlet", re.compile(r"\bInvoke-[A-Z][A-Za-z]{3,}\b")),
    ("reverse shell", re.compile(r"(?i)\b(nc|ncat|netcat)\s+(-e|-c)\b|bash -i\s*>&|/dev/tcp/")),
)
# House rules.
WRONG_WORDS = re.compile(r"\blearners?\b|\b(?:course|class|student) members?\b", re.IGNORECASE)
NO_CHOICE_OPTION = re.compile(r"\b(?:all|none) of the (?:above|these)\b|\bboth of (?:the above|these)\b", re.I)
OPTION_LETTERS = re.compile(r"\b[A-D] and [A-D]\b")
MAX_SAME_KEY = 2
MIN_DISTINCT_KEYS = 3
MAX_LONGEST_KEYED = 3
LAB_STEPS = (3, 8)
NOT_HOSTS = frozenset({"x64", "x86", "win10", "win11", "tls10", "tls11", "tls12", "tls13", "http11", "utf16", "utf32"})
_STOP_WORDS = (
    "about above after again against also among and another any are because been before being below "
    "between both but can cannot could does doing down during each either every few for from further "
    "have having here how into its itself just least less more most must never none nor not now off "
    "once only other ought our out over own same should some such than that the their them then there "
    "these they this those through too under until upon very was were what when where which while who "
    "whom why will with within without would your following best primary main most likely true false "
    "used uses using correct describes statement"
)
STOP = frozenset(_STOP_WORDS.split())


class Result:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = False

    def check(self, name: str, problems: list[str]) -> None:
        if problems:
            self.failed = True
            self.lines.append(f"  FAIL {name}")
            self.lines.extend(f"       - {p}" for p in problems)
        else:
            self.lines.append(f"  PASS {name}")


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _schema_errors(schema_path: Path, doc) -> list[str]:
    validator = Draft7Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    ]


def _git_show(root: Path, ref: str, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _modules(doc) -> list[dict]:
    mods = doc.get("modules") if isinstance(doc, dict) else None
    return [m for m in mods if isinstance(m, dict)] if isinstance(mods, list) else []


def _words(text: str) -> set[str]:
    """Key terms: words of 4+ letters (minus stop words) cut to 5 letters, plus acronyms
    (TCP, IDS, XSS) as written in capitals."""
    words = {w[:5] for w in re.findall(r"[a-z][a-z0-9]{3,}", text.lower()) if w not in STOP}
    return words | {a.lower() for a in re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", text)}


def _option_body(option: str) -> str:
    return re.sub(r"^[A-D]\)\s*", "", str(option))


def template_nodes(root: Path, template: str) -> set[str] | None:
    path = root / "content/ranges" / template / "template.yaml"
    if not path.is_file():
        return None
    doc = _load_yaml(path) or {}
    return {str(n.get("id")) for n in doc.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


# -- checks ----------------------------------------------------------------------------


def check_banner(text: str, doc: dict, base_text: str | None) -> list[str]:
    problems = []
    head = [ln for ln in text.splitlines()[:10] if ln.startswith("#")]
    if not any(BANNER.match(ln) for ln in head):
        problems.append("the leading 'Unsourced ... not validated by Standards' banner comment is missing")
    if doc.get("provenance") != "unsourced":
        problems.append("provenance must stay 'unsourced'")
    if doc.get("is_published") is not False:
        problems.append("is_published must stay false")
    notes = list(((doc.get("source") or {}).get("notes")) or [])
    if UNPUBLISHED_NOTE not in notes:
        problems.append(f"source.notes lost {UNPUBLISHED_NOTE!r}")
    if base_text is not None:
        base = yaml.safe_load(base_text) or {}
        for ln in base_text.splitlines()[:10]:
            if ln.startswith("#") and ln not in head:
                problems.append(f"banner line removed: {ln!r}")
        for note in ((base.get("source") or {}).get("notes")) or []:
            if note not in notes:
                problems.append(f"source note removed: {note!r}")
    return problems


def check_readiness(doc: dict, base_text: str | None) -> list[str]:
    value = doc.get("readiness")
    if value is None:
        return ["readiness is not set (TAZ sets draft; Claude sets technically_proven after validation)"]
    if value == "sme_signed":
        base = (yaml.safe_load(base_text) or {}) if base_text else {}
        if base.get("readiness") != "sme_signed":
            return ["readiness sme_signed is set only by SME/Standards sign-off, never by an author"]
    return []


def check_modules(doc: dict) -> list[str]:
    mods = _modules(doc)
    problems = [] if len(mods) == MODULE_COUNT else [f"{len(mods)} modules, expected {MODULE_COUNT}"]
    for m in mods:
        n = m.get("ordinal", "?")
        for key in ("objectives", "topics"):
            if not m.get(key):
                problems.append(f"module {n}: no {key}")
        if not str(m.get("lab") or "").strip():
            problems.append(f"module {n}: no lab brief")
        if not isinstance(m.get("lab_range"), dict):
            problems.append(f"module {n}: no lab_range")
    return problems


def check_lab_range(root: Path, doc: dict) -> list[str]:
    problems = []
    for m in _modules(doc):
        lr = m.get("lab_range")
        if not isinstance(lr, dict):
            continue
        n = m.get("ordinal", "?")
        template = str(lr.get("template") or "")
        if template_nodes(root, template) is None:
            problems.append(f"module {n}: template {template!r} is not content/ranges/{template}/template.yaml")
        scenario = lr.get("scenario")
        if scenario:
            spath = root / "content/scenarios" / str(scenario) / "scenario.yaml"
            if not spath.is_file():
                problems.append(f"module {n}: scenario {scenario!r} is not content/scenarios/{scenario}/scenario.yaml")
            else:
                wants = (_load_yaml(spath) or {}).get("range_template")
                if wants and wants != template:
                    problems.append(f"module {n}: scenario {scenario!r} runs on {wants!r}, not {template!r}")
    return problems


def check_hosts(root: Path, doc: dict) -> list[str]:
    problems = []
    for m in _modules(doc):
        lr = m.get("lab_range")
        if not isinstance(lr, dict):
            continue
        n = m.get("ordinal", "?")
        nodes = template_nodes(root, str(lr.get("template") or ""))
        if nodes is None:
            continue  # reported by lab_range
        for i, step in enumerate(lr.get("steps") or [], start=1):
            if not isinstance(step, dict):
                problems.append(f"module {n} step {i}: not a {{hosts, action}} mapping")
                continue
            for host in step.get("hosts") or []:
                if host not in nodes:
                    problems.append(f"module {n} step {i}: host {host!r} is not a node of {lr['template']!r}")
            for token in HOST_SHAPE.findall(str(step.get("action") or "")):
                if token not in nodes and token not in NOT_HOSTS:
                    problems.append(
                        f"module {n} step {i}: action names {token!r}, which is not a node of {lr['template']!r}"
                    )
    return problems


def check_refs(root: Path, doc: dict) -> list[str]:
    lib_path = root / "content/catalogue/references.yaml"
    try:
        keys = set((_load_yaml(lib_path) or {}).get("references") or {})
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot read {lib_path}: {exc}"]
    problems = []
    for m in _modules(doc):
        for ref in m.get("refs") or []:
            if ref not in keys:
                problems.append(f"module {m.get('ordinal', '?')}: ref {ref!r} is not in references.yaml")
    return problems


def check_quiz(doc: dict) -> list[str]:
    problems = []
    seen: dict[str, int] = {}
    for m in _modules(doc):
        n = m.get("ordinal", "?")
        questions = ((m.get("quiz") or {}).get("questions")) or []
        if len(questions) != QUESTIONS_PER_QUIZ:
            problems.append(f"module {n}: {len(questions)} questions, expected {QUESTIONS_PER_QUIZ}")
        for i, q in enumerate(questions, start=1):
            options = [str(o) for o in q.get("options") or []]
            if len(options) != len(LETTERS) or any(
                not o.startswith(f"{letter}) ") for letter, o in zip(LETTERS, options, strict=False)
            ):
                problems.append(f"module {n} question {i}: options must be four, lettered 'A) ' to 'D) ' in order")
            if q.get("answer") not in LETTERS[: len(options)]:
                problems.append(f"module {n} question {i}: answer {q.get('answer')!r} is not one of its option letters")
            stem = " ".join(str(q.get("question") or "").lower().split())
            if stem in seen:
                problems.append(f"module {n} question {i}: duplicates the stem of a question in module {seen[stem]}")
            seen.setdefault(stem, n)
    return problems


def check_quiz_terms(doc: dict) -> list[str]:
    problems = []
    for m in _modules(doc):
        n = m.get("ordinal", "?")
        vocab = _words(" ".join(str(x) for x in (m.get("objectives") or []) + (m.get("topics") or [])))
        for i, q in enumerate(((m.get("quiz") or {}).get("questions")) or [], start=1):
            options = q.get("options") or []
            idx = LETTERS.index(q["answer"]) if q.get("answer") in LETTERS else -1
            keyed = _option_body(options[idx]) if 0 <= idx < len(options) else ""
            if not (_words(f"{q.get('question') or ''} {keyed}") & vocab):
                problems.append(
                    f"module {n} question {i}: shares no key term with the module's objectives or topics "
                    f"({str(q.get('question'))[:60]!r})"
                )
    return problems


# -- house-style checks ------------------------------------------------------------------


def check_objectives(doc: dict) -> list[str]:
    problems = []
    for m in _modules(doc):
        for i, text in enumerate(m.get("objectives") or [], start=1):
            text = str(text)
            if UNOBSERVABLE.search(text):
                problems.append(
                    f"module {m.get('ordinal', '?')} objective {i}: starts with a verb nobody can observe "
                    f"({text[:40]!r}); use Identify, Classify, Explain, Configure, Detect, ..."
                )
            elif len(text.strip()) < 10:
                problems.append(f"module {m.get('ordinal', '?')} objective {i}: shorter than 10 characters")
    return problems


def _pack_text(course: Path) -> str:
    path = instructor_path(course)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _strings(node):
    """Every string value in a parsed YAML tree, so a scalar that YAML folds across lines is read whole."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def check_wording(text: str, doc: dict, pack_text: str) -> list[str]:
    problems = []
    try:
        pack = yaml.safe_load(pack_text) if pack_text else None
    except yaml.YAMLError:
        pack = None
    blob = yaml.safe_dump(_modules(doc), allow_unicode=True)
    for token in FRAMEWORK_TOKENS:
        if token in blob.lower():
            problems.append(
                f"modules contain the framework token {token!r}; mappings belong to the crosswalk, not here"
            )
    if re.search(r"\bNICE\b|\bDCWF\b", blob):
        problems.append("modules name NICE or DCWF; mappings belong to the crosswalk, not here")
    for name, body in (("course file", text), ("instructor pack", pack_text)):
        for bad in MISCITED:
            if bad in body:
                problems.append(f"{name} cites the withdrawn or miscited publication {bad!r}")
    # The author may not touch source.notes or the banner, so only module text and the pack are read.
    for name, node in (("module text", _modules(doc)), ("instructor pack", pack)):
        for value in _strings(node):
            for m in WRONG_WORDS.finditer(value):
                problems.append(f"{name}: {m.group(0)!r}; say 'Student' for a person taking a course")
            if "AUTHOR-REQUIRED" in value:
                continue
            for label, pattern in DEFANG_PATTERNS:
                if pattern.search(value):
                    problems.append(
                        f"{name}: {value[:40]!r} looks like offensive material ({label}); describe it, do not supply it"
                    )
    return problems


def _other_stems(root: Path, own: str) -> dict[str, str]:
    stems: dict[str, str] = {}
    for path in sorted((root / "content/courses").glob("*.yaml")):
        if path.name == own:
            continue
        try:
            other = _load_yaml(path) or {}
        except (OSError, yaml.YAMLError):
            continue
        for m in _modules(other):
            for q in ((m.get("quiz") or {}).get("questions")) or []:
                if isinstance(q, dict):
                    stems.setdefault(" ".join(str(q.get("question") or "").lower().split()), path.name)
    return stems


def check_quiz_style(root: Path, path: Path, doc: dict) -> list[str]:
    problems = []
    library = _other_stems(root, path.name)
    for m in _modules(doc):
        n = m.get("ordinal", "?")
        quiz = m.get("quiz") or {}
        if m.get("pass_threshold") != quiz.get("pass_threshold"):
            problems.append(
                f"module {n}: pass_threshold {m.get('pass_threshold')!r} differs from its quiz's "
                f"{quiz.get('pass_threshold')!r}"
            )
        keys, longest = [], 0
        for i, q in enumerate(quiz.get("questions") or [], start=1):
            if not isinstance(q, dict):
                continue
            stem = " ".join(str(q.get("question") or "").lower().split())
            if stem in library:
                problems.append(f"module {n} question {i}: the stem is already in {library[stem]}; write a new one")
            bodies = [_option_body(o).strip().lower() for o in q.get("options") or []]
            if len(set(bodies)) != len(bodies):
                problems.append(f"module {n} question {i}: two options say the same thing")
            for o in q.get("options") or []:
                if NO_CHOICE_OPTION.search(str(o)) or OPTION_LETTERS.search(str(o)):
                    problems.append(
                        f"module {n} question {i}: option {str(o)[:30]!r} refers to other options; make each stand alone"
                    )
            if q.get("answer") in LETTERS:
                keys.append(q["answer"])
                idx = LETTERS.index(q["answer"])
                if idx < len(bodies) and all(len(bodies[idx]) > len(b) for j, b in enumerate(bodies) if j != idx):
                    longest += 1
        if keys:
            counts = {k: keys.count(k) for k in set(keys)}
            if max(counts.values()) > MAX_SAME_KEY or len(counts) < MIN_DISTINCT_KEYS:
                problems.append(
                    f"module {n}: answer keys {''.join(keys)} are bunched; use at least {MIN_DISTINCT_KEYS} different "
                    f"letters and none more than {MAX_SAME_KEY} times (move the right option, not the stem)"
                )
        if longest > MAX_LONGEST_KEYED:
            problems.append(
                f"module {n}: the keyed option is the longest in {longest} of 5 questions; "
                f"lengthen the distractors to the keyed option's length"
            )
    return problems


def check_lab_style(doc: dict) -> list[str]:
    problems = []
    for m in _modules(doc):
        n = m.get("ordinal", "?")
        lab = str(m.get("lab") or "")
        if lab.strip() and not (lab.lstrip().startswith("Lab:") and "Evidence:" in lab and "Pass:" in lab):
            problems.append(
                f"module {n}: lab must read 'Lab: <task>. Evidence: <what is recorded>. Pass: <P/F items>.'"
            )
        lr = m.get("lab_range")
        if isinstance(lr, dict):
            steps = lr.get("steps") or []
            if not LAB_STEPS[0] <= len(steps) <= LAB_STEPS[1]:
                problems.append(f"module {n}: {len(steps)} lab_range steps, expected {LAB_STEPS[0]} to {LAB_STEPS[1]}")
            for i, step in enumerate(steps, start=1):
                if isinstance(step, dict) and not (step.get("hosts") and str(step.get("action") or "").strip()):
                    problems.append(f"module {n} step {i}: needs hosts and an action")
    return problems


def instructor_path(course: Path) -> Path:
    return course.parent / "instructor" / course.name


def check_instructor(course: Path, doc: dict) -> list[str]:
    path = instructor_path(course)
    if not path.is_file():
        return [f"no instructor pack at {path}"]
    try:
        pack = _load_yaml(path)
    except yaml.YAMLError as exc:
        return [f"{path.name}: not YAML: {exc}"]
    problems = _schema_errors(PACK_SCHEMA, pack)
    if problems or not isinstance(pack, dict):
        return problems or ["the pack is not a mapping"]
    if pack.get("course_code") != doc.get("course_code"):
        problems.append(f"course_code {pack.get('course_code')!r} != the course's {doc.get('course_code')!r}")
    ordinals = sorted(m.get("ordinal") for m in _modules(doc))
    for key in ("timings", "answer_key_notes", "lab_setup"):
        got = sorted(e["module"] for e in pack[key])
        if got != ordinals:
            problems.append(f"{key} covers modules {got}, expected one entry for each of {ordinals}")
    modules = {m.get("ordinal"): m for m in _modules(doc)}
    for t in pack["timings"]:
        want = (modules.get(t["module"]) or {}).get("duration_minutes")
        if want is not None and t["minutes"] != want:
            problems.append(f"timings module {t['module']}: {t['minutes']} minutes, but the module is {want}")
    for entry in pack["answer_key_notes"]:
        questions = (((modules.get(entry["module"]) or {}).get("quiz") or {}).get("questions")) or []
        for i, q in enumerate(questions, start=1):
            letter = q.get("answer")
            if letter in LETTERS and not re.search(rf"\bQ{i}\b[\s:.(-]*{letter}\b", entry["notes"]):
                problems.append(
                    f"answer_key_notes module {entry['module']}: give 'Q{i} {letter}: <why it is right, "
                    f"what the distractors test>'"
                )
    return problems


def check_file(path: Path, *, root: Path, base: str | None, held: bool) -> Result:
    res = Result()
    try:
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        res.check("schema", [f"cannot read: {exc}"])
        return res
    if not isinstance(doc, dict):
        res.check("schema", ["not a YAML mapping"])
        return res
    if held:
        is_held = path.name in HELD_FILES or doc.get("course_code") in HELD_CODES
        res.check(
            "held", [f"{doc.get('course_code')} is held for cleared authors; TAZ does not draft it"] if is_held else []
        )
        if is_held:
            return res
    base_text = None
    if base:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = None
        base_text = _git_show(root, base, rel) if rel else None
    res.check("schema", _schema_errors(COURSE_SCHEMA, doc))
    res.check("readiness", check_readiness(doc, base_text))
    res.check("banner", check_banner(text, doc, base_text))
    res.check("modules", check_modules(doc))
    res.check("lab_range", check_lab_range(root, doc))
    res.check("hosts", check_hosts(root, doc))
    res.check("refs", check_refs(root, doc))
    res.check("quiz", check_quiz(doc))
    res.check("quiz_terms", check_quiz_terms(doc))
    res.check("objectives", check_objectives(doc))
    res.check("wording", check_wording(text, doc, _pack_text(path)))
    res.check("quiz_style", check_quiz_style(root, path, doc))
    res.check("lab_style", check_lab_style(doc))
    res.check("instructor", check_instructor(path, doc))
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check authored course files against the technically-proven bar.")
    ap.add_argument("courses", nargs="+", type=Path, help="content/courses/<file>.yaml")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root holding content/ (default: this repo)")
    ap.add_argument(
        "--base", default=None, help="git ref whose banner and source notes must be kept (e.g. origin/main)"
    )
    ap.add_argument(
        "--held",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="refuse the seven courses held for cleared authors (default on)",
    )
    args = ap.parse_args(argv)
    failed = False
    for path in args.courses:
        res = check_file(path, root=args.root, base=args.base, held=args.held)
        print(f"{'FAIL' if res.failed else 'PASS'} {path}")
        print("\n".join(res.lines))
        failed = failed or res.failed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
