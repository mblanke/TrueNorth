"""QA depth for ARC² runs: the repo's own content rules, applied before a promotion PR finds them.

``arc2.check`` calls :func:`check_run`. The rules mirror ``tests/api/test_course_content_ingest.py``
and the API's pure parsers (``app.course_content_ingest.parse_course_content``,
``app.programme_ingest.parse_programme``, ``app.engine_bridge.validate_yaml``), so a run that
passes here does not surprise the repo's tests later. A parity test runs :func:`course_rules`
over every file in ``content/courses/`` and expects nothing, which keeps this checker from
drifting stricter than the tests it mirrors.

Never a vacuous pass: when the API package, the reference library, the catalogue, the VM
catalogue or the engine schema cannot be loaded, the check records a ``human`` finding and
says which rules did not run.

Checks (all ``fail`` unless noted; owner in brackets):
  qa.api_unavailable [orchestrator, human]   the API parsers could not be imported
  qa.course_parse / qa.course.<rule>          the course YAML against the ingest rules [code-generator]
  qa.course_code_mismatch, qa.course_manifest_mismatch, qa.course_objectives_verbatim
  qa.catalogue_* [content-architect]           01-blueprint/catalogue_row.csv against programme_ingest
                                              (a run for an existing course instead names course.catalogue_code,
                                              which must be in the catalogue: qa.catalogue_identity)
  qa.engine_scenario [sensor-gateway]          05-sensor scenario against scenario-engine's JSON schema
  qa.timeline_* [range-engineer]               the timeline file against the manifest, vm_catalogue and validators
  qa.author_required_marker [range-engineer]   author_required injects carry the AUTHOR-REQUIRED placeholder
  qa.defang [stage of the directory]           offensive artefacts in any staged text file
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from arc2.cmi5 import FRAMEWORK_TOKENS, TEXT_SUFFIXES

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "control-plane" / "api"
COURSES_REL = Path("content/courses")
CATALOGUE_REL = Path("content/catalogue/cyber_operator_programme.csv")
REFERENCES_REL = Path("content/catalogue/references.yaml")
VM_CATALOGUE_REL = Path("truenorth-content-pack/truenorth-content/vm_catalogue.csv")
CROSSWALK_REL = Path("truenorth-content-pack/truenorth-content/crosswalk.csv")
CATALOGUE_ROW_REL = Path("01-blueprint/catalogue_row.csv")
ORCHESTRATOR = "orchestrator"
# tests/api/test_course_content_ingest.py::test_every_course_file_declares_a_qualification
KNOWN_QSPS = frozenset({"ALJQ", "TEMP67", "TEMP64", "ALRA"})
# ::test_the_miscited_publications_are_gone
MISCITED = ("8286", "IoT Threat Landscape", "T9 (Insecure Network Services)")
PLACEHOLDER = "AUTHOR-REQUIRED"
SCAN_DIRS = {
    "02-content": "code-generator",
    "03-range": "range-engineer",
    "04-artifacts": "artifact-creator",
    "05-sensor": "sensor-gateway",
}
# What an agent must never write into staged content. A line carrying the placeholder is the
# sanctioned way to say "a cleared author supplies this", so it is exempt.
DEFANG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("powershell encoded command", re.compile(r"(?i)(?<![\w-])-e(nc|ncodedcommand)?\s+[A-Za-z0-9+/=]{16,}")),
    ("base64 run", re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")),
    ("hex run", re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}|(?<![0-9a-fA-F])(?:[0-9a-fA-F]{2}[ :]){15,}[0-9a-fA-F]{2}")),
    ("metasploit", re.compile(r"(?i)\bmsfvenom\b|\bmsfconsole\b")),
    ("credential dumper", re.compile(r"(?i)\bmimikatz\b|\bsekurlsa\b")),
    ("Invoke-* cmdlet", re.compile(r"\bInvoke-[A-Z][A-Za-z]{3,}\b")),
    ("reverse shell", re.compile(r"(?i)\b(nc|ncat|netcat)\s+(-e|-c)\b|bash -i\s*>&|/dev/tcp/")),
)
MAX_DEFANG_PER_FILE = 20
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _finding(check: str, severity: str, message: str, owner: str, path: str | None = None) -> dict[str, Any]:
    return {"check": check, "severity": severity, "owner_stage": owner, "message": message, "path": path}


# ── the API's pure parsers ─────────────────────────────────────────────


def _api() -> tuple[Any | None, str | None]:
    """The API package's parsers, or (None, why). Importing ``app`` needs its directory on
    sys.path and two settings it reads at import time; the defaults are the ones the test
    suite uses (tests/conftest.py) and touch no database."""
    if str(API_DIR) not in sys.path:
        sys.path.insert(0, str(API_DIR))
    os.environ.setdefault("AUTH_DISABLED", "true")
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    try:
        from app import course_content_ingest, engine_bridge, programme_ingest
    except Exception as exc:  # any import failure is reported as a finding, never hidden
        return None, f"{type(exc).__name__}: {exc}"
    return {"course": course_content_ingest, "programme": programme_ingest, "engine": engine_bridge}, None


# ── course rules (mirrors tests/api/test_course_content_ingest.py) ────


def library_stems(repo_root: Path) -> dict[str, str]:
    """Every quiz stem in content/courses → the file that holds it."""
    stems: dict[str, str] = {}
    for path in sorted((repo_root / COURSES_REL).glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for module in doc.get("modules") or []:
            for q in ((module or {}).get("quiz") or {}).get("questions") or []:
                stems.setdefault(str(q.get("question") or "").strip(), path.name)
    return stems


def reference_keys(repo_root: Path) -> set[str] | None:
    path = repo_root / REFERENCES_REL
    try:
        lib = yaml.safe_load(path.read_text(encoding="utf-8"))["references"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return None
    return set(lib)


def known_qsps(repo_root: Path) -> frozenset[str]:
    codes = set(KNOWN_QSPS)
    path = repo_root / CROSSWALK_REL
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            codes.update(r["qsp_code"].strip() for r in csv.DictReader(fh) if r.get("qsp_code"))
    return frozenset(codes)


def course_rules(
    raw: dict[str, Any],
    parsed: dict[str, Any],
    *,
    own_name: str,
    references: set[str] | None,
    stems: dict[str, str],
    qsp_codes: frozenset[str] = KNOWN_QSPS,
    arc2: bool = True,
    bound_pos: set[tuple[str, str]] | None = None,
    no_lab: frozenset[int] = frozenset(),
) -> list[tuple[str, str]]:
    """(rule, message) pairs. ``references=None`` means the library could not be read and
    the refs rules are skipped (the caller records that). ``arc2=False`` applies only the
    rules the repo's tests apply to every course file (the parity test uses it).
    ``no_lab`` holds the ordinals of theory and practical modules: they must carry no range
    lab text, where every other module must."""
    out: list[tuple[str, str]] = []

    def bad(rule: str, message: str) -> None:
        out.append((rule, message))

    if raw.get("is_published") is not False:
        bad("course.unpublished", "is_published must be false")
    if raw.get("provenance") != "unsourced":
        bad("course.provenance", f"provenance must be 'unsourced', not {raw.get('provenance')!r}")
    if raw.get("status") not in {"draft", "proposed"}:
        bad("course.status", f"status must be draft or proposed, not {raw.get('status')!r}")
    if raw.get("qsp_code") not in qsp_codes:
        bad(
            "course.qsp_code",
            f"qsp_code {raw.get('qsp_code')!r} is not one of {sorted(qsp_codes)} (QSP-TODO is for the catalogue row only)",
        )
    notes = " ".join(str(n) for n in (raw.get("source") or {}).get("notes") or []).lower()
    if "proposed" not in notes or "not standards-validated" not in notes:
        bad("course.notes_proposed", "source.notes must record the mapping as 'proposed' and 'not standards-validated'")
    if not parsed["modules"]:
        bad("course.modules", "the course has no modules")
    blob = yaml.safe_dump(raw.get("modules") or [], allow_unicode=True).lower()
    for token in FRAMEWORK_TOKENS:
        if token in blob:
            bad("course.framework_tokens", f"modules contain framework token {token!r}")
    whole = yaml.safe_dump(raw, allow_unicode=True)
    for text in MISCITED:
        if text in whole:
            bad("course.miscited", f"cites the withdrawn/miscited publication {text!r}")
    raw_modules = raw.get("modules") or []
    for i, m in enumerate(parsed["modules"]):
        where = f"module {m['ordinal']}"
        if not m["objectives"]:
            bad("course.module_objectives", f"{where} has no objectives")
        if not m["topics"]:
            bad("course.module_topics", f"{where} has no topics")
        if m["ordinal"] in no_lab:
            if m["lab"]:
                bad("course.module_lab_unexpected", f"{where} is not a range activity but has lab text")
        elif not m["lab"]:
            bad("course.module_lab", f"{where} has no lab")
        if references is not None:
            if not m["refs"]:
                bad("course.refs", f"{where} cites no references")
            for key in m["refs"]:
                if key not in references:
                    bad("course.refs", f"{where} cites unverified reference {key!r}")
        raw_questions = (((raw_modules[i] if i < len(raw_modules) else {}) or {}).get("quiz") or {}).get(
            "questions"
        ) or []
        for j, q in enumerate(m["questions"]):
            head = q["stem"][:50]
            if len(q["options"]) != 4:
                bad("course.quiz_options", f"{where} question {head!r} has {len(q['options'])} options, not 4")
            answer = str((raw_questions[j] if j < len(raw_questions) else {}).get("answer") or "")
            if answer not in {"A", "B", "C", "D"}:
                bad("course.quiz_answer", f"{where} question {head!r} answer {answer!r} is not one of A-D")
            if not q["correct"]:
                bad("course.quiz_answer", f"{where} question {head!r} has no answer key")
            owner = stems.get(q["stem"])
            if owner and owner != own_name:
                bad("course.duplicate_stem", f"{where} question {head!r} already exists in {owner}")
        if arc2 and m["po"] and (m["po"]["qsp_code"], m["po"]["po_code"]) not in (bound_pos or set()):
            bad(
                "course.po_unbound",
                f"{where} binds {m['po']['qsp_code']}/{m['po']['po_code']}, which the manifest does not bind",
            )
    if arc2 and not re.fullmatch(r"ARC2-[A-Z0-9]{2,8}", parsed["course_code"]):
        bad("course.arc2_code", f"course_code {parsed['course_code']!r} must be ARC2-<2-8 uppercase alphanumerics>")
    return out


def check_course(run: Path, manifest: dict[str, Any], repo_root: Path, api: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    content = manifest["content"]
    rel = content["course_yaml"]
    path = run / rel
    if not path.is_file():
        return out  # trace.file_exists already reports it
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text) or {}
        parsed = api["course"].parse_course_content(text)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        return [_finding("qa.course_parse", "fail", f"{rel}: {exc}", "code-generator", rel)]
    references = reference_keys(repo_root)
    if references is None:
        out.append(
            _finding(
                "qa.references_unavailable",
                "human",
                f"{REFERENCES_REL} could not be read; the refs rules did not run",
                ORCHESTRATOR,
                rel,
            )
        )
    course = manifest.get("course") or {}
    bound = set()
    if course.get("po"):
        bound.add((course["po"]["qsp_code"], course["po"]["po_code"]))
    bound.update((o["po"]["qsp_code"], o["po"]["po_code"]) for o in manifest.get("objectives", []) if o.get("po"))
    for rule, message in course_rules(
        raw,
        parsed,
        own_name=path.name,
        references=references,
        stems=library_stems(repo_root),
        qsp_codes=known_qsps(repo_root),
        bound_pos=bound,
        no_lab=frozenset(
            m["ordinal"] for m in content["modules"] if (m.get("activity") or {}).get("kind", "range") != "range"
        ),
    ):
        out.append(_finding(f"qa.{rule}", "fail", f"{rel}: {message}", "code-generator", rel))

    if course and parsed["course_code"] != course.get("code"):
        out.append(
            _finding(
                "qa.course_code_mismatch",
                "fail",
                f"{rel}: course_code {parsed['course_code']!r} != manifest {course.get('code')!r}",
                "code-generator",
                rel,
            )
        )
    by_ordinal = {m["ordinal"]: m for m in content["modules"]}
    if len(parsed["modules"]) != len(content["modules"]):
        out.append(
            _finding(
                "qa.course_manifest_mismatch",
                "fail",
                f"{rel}: {len(parsed['modules'])} modules in the file, {len(content['modules'])} in the manifest",
                "code-generator",
                rel,
            )
        )
    objectives_by_module: dict[str, list[str]] = {}
    for o in manifest.get("objectives", []):
        objectives_by_module.setdefault(o["module_id"], []).append(o["text"])
    for m in parsed["modules"]:
        want = by_ordinal.get(m["ordinal"])
        if want is None:
            out.append(
                _finding(
                    "qa.course_manifest_mismatch",
                    "fail",
                    f"{rel}: module {m['ordinal']} is not in the manifest",
                    "code-generator",
                    rel,
                )
            )
            continue
        problems = []
        if m["title"] != want["title"]:
            problems.append(f"title {m['title']!r} != {want['title']!r}")
        if m["pass_threshold"] != want["pass_threshold"]:
            problems.append(f"pass_threshold {m['pass_threshold']} != {want['pass_threshold']}")
        if bool(m["questions"]) != bool(want["quiz"]):
            problems.append("quiz present in one but not the other")
        elif want["quiz"] and (
            len(m["questions"]) != want["quiz"]["question_count"] or m["quiz_pass"] != want["quiz"]["pass_threshold"]
        ):
            problems.append(
                f"quiz has {len(m['questions'])} questions / threshold {m['quiz_pass']}, manifest says {want['quiz']}"
            )
        if problems:
            out.append(
                _finding(
                    "qa.course_manifest_mismatch",
                    "fail",
                    f"{rel}: module {m['ordinal']}: " + "; ".join(problems),
                    "code-generator",
                    rel,
                )
            )
        expected = objectives_by_module.get(want["id"], [])
        if [str(o).strip() for o in m["objectives"]] != expected:
            out.append(
                _finding(
                    "qa.course_objectives_verbatim",
                    "fail",
                    f"{rel}: module {m['ordinal']} objectives differ from the blueprint's ({want['id']}); copy them byte-for-byte",
                    "code-generator",
                    rel,
                )
            )
    return out


# ── catalogue row ─────────────────────────────────────────────────────


def check_catalogue(run: Path, manifest: dict[str, Any], repo_root: Path, api: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rel = CATALOGUE_ROW_REL.as_posix()
    path = run / CATALOGUE_ROW_REL
    owner = "content-architect"
    if not path.is_file():
        return [
            _finding(
                "qa.catalogue_row_missing", "fail", f"{rel} is missing; the content-architect writes it", owner, rel
            )
        ]
    text = path.read_text(encoding="utf-8-sig")
    header = next(csv.reader(io.StringIO(text)), [])
    expected = list(api["programme"].EXPECTED_COLUMNS)
    if header != expected:
        out.append(_finding("qa.catalogue_columns", "fail", f"{rel}: columns {header} != {expected}", owner, rel))
    rows = api["programme"].parse_programme(text)
    if len(rows) != 1:
        out.append(
            _finding("qa.catalogue_one_row", "fail", f"{rel}: {len(rows)} rows parsed, expected exactly 1", owner, rel)
        )
        return out
    row = rows[0]
    course = manifest.get("course") or {}
    checks = (
        ("course_code", row["course_code"], course.get("code")),
        ("course_title", row["course_title"], course.get("title")),
        ("programme", row["programme"], "cyber-operator"),
        ("status", row["status"], "proposed"),
        ("provenance", row["provenance"], "unsourced"),
        ("dp_order", row["dp_order"], course.get("dp_order")),
    )
    for name, got, want in checks:
        if got != want:
            out.append(_finding("qa.catalogue_row", "fail", f"{rel}: {name} {got!r} != {want!r}", owner, rel))
    if row["qsp_code"] is not None and row["qsp_code"] not in known_qsps(repo_root):
        out.append(
            _finding(
                "qa.catalogue_row",
                "fail",
                f"{rel}: qsp_code {row['qsp_code']!r} is neither QSP-TODO nor a known QSP",
                owner,
                rel,
            )
        )
    if course.get("duration_hours") and row["duration_hours"] != int(course["duration_hours"]):
        out.append(
            _finding(
                "qa.catalogue_row",
                "fail",
                f"{rel}: duration_hours {row['duration_hours']} != {course['duration_hours']}",
                owner,
                rel,
            )
        )
    existing = repo_root / CATALOGUE_REL
    if not existing.is_file():
        out.append(
            _finding(
                "qa.catalogue_unavailable",
                "human",
                f"{CATALOGUE_REL} could not be read; the code-uniqueness rule did not run",
                ORCHESTRATOR,
                rel,
            )
        )
    else:
        codes = {r["course_code"] for r in api["programme"].parse_programme(existing.read_text(encoding="utf-8-sig"))}
        if row["course_code"] in codes:
            out.append(
                _finding(
                    "qa.catalogue_code_unique",
                    "fail",
                    f"{rel}: course_code {row['course_code']!r} already exists in {CATALOGUE_REL}",
                    owner,
                    rel,
                )
            )
    return out


def check_catalogue_identity(manifest: dict[str, Any], repo_root: Path, api: dict[str, Any]) -> list[dict[str, Any]]:
    """A run that produces content for an existing catalogue course names it; the release and
    the importer key on that code, so a typo here would create nothing or the wrong course."""
    code = manifest["course"]["catalogue_code"]
    path = repo_root / CATALOGUE_REL
    if not path.is_file():
        return [
            _finding(
                "qa.catalogue_unavailable",
                "human",
                f"{CATALOGUE_REL} could not be read; catalogue_code {code!r} was not checked",
                ORCHESTRATOR,
            )
        ]
    codes = {r["course_code"] for r in api["programme"].parse_programme(path.read_text(encoding="utf-8-sig"))}
    if code not in codes:
        return [
            _finding(
                "qa.catalogue_identity",
                "fail",
                f"course.catalogue_code {code!r} is not a course in {CATALOGUE_REL}",
                "content-architect",
            )
        ]
    return []


# ── engine scenario ───────────────────────────────────────────────────


def check_engine_scenario(run: Path, manifest: dict[str, Any], api: dict[str, Any]) -> list[dict[str, Any]]:
    rel = manifest["scenario"]["path"]
    path = run / rel
    if not path.is_file():
        return []
    try:
        result = api["engine"].validate_yaml("scenario", path.read_text(encoding="utf-8"))
    except Exception as exc:  # HTTPException (503) when the engine schema is unavailable, or OSError
        return [
            _finding(
                "qa.engine_schema_unavailable",
                "human",
                f"the scenario-engine schema could not be loaded ({type(exc).__name__}); {rel} was not validated",
                ORCHESTRATOR,
                rel,
            )
        ]
    claimed = manifest["scenario"]["engine_validate"]["status"]
    out = []
    for err in result["errors"]:
        where = f" at {err['path']}" if err.get("path") else ""
        out.append(
            _finding(
                "qa.engine_scenario",
                "fail",
                f"{rel}{where}: {err['message']}"
                + (" (engine_validate claimed passed)" if claimed == "passed" else ""),
                "sensor-gateway",
                rel,
            )
        )
    return out


# ── timeline ──────────────────────────────────────────────────────────


def enabled_templates(repo_root: Path) -> set[str] | None:
    path = repo_root / VM_CATALOGUE_REL
    if not path.is_file():
        return None
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {
            r["template_id"].strip() for r in csv.DictReader(fh) if str(r.get("enabled", "")).strip().lower() == "yes"
        }


def check_timeline(run: Path, manifest: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    injects = manifest["injects"]
    rel = injects["timeline"]
    path = run / rel
    owner = "range-engineer"
    if not path.is_file():
        return out
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return [_finding("qa.timeline_parse", "fail", f"{rel}: {exc}", owner, rel)]
    sc = doc.get("scenario") if isinstance(doc, dict) and isinstance(doc.get("scenario"), dict) else doc
    if not isinstance(sc, dict):
        return [_finding("qa.timeline_parse", "fail", f"{rel}: expected a mapping with a 'scenario' block", owner, rel)]
    file_injects = {str(i.get("id")): i for i in (sc.get("injects") or []) if isinstance(i, dict)}
    manifest_injects = {i["id"]: i for i in injects["items"]}
    if set(file_injects) != set(manifest_injects):
        out.append(
            _finding(
                "qa.timeline_injects_match",
                "fail",
                f"{rel}: inject ids {sorted(file_injects)} != manifest {sorted(manifest_injects)}",
                owner,
                rel,
            )
        )
    for iid, item in manifest_injects.items():
        fi = file_injects.get(iid)
        if fi is not None and bool(fi.get("critical", False)) != bool(item["critical"]):
            out.append(
                _finding(
                    "qa.timeline_injects_match",
                    "fail",
                    f"{rel}: inject {iid} critical={fi.get('critical')} in the file, {item['critical']} in the manifest",
                    owner,
                    rel,
                )
            )
        if item["author_required"] and PLACEHOLDER not in text:
            out.append(
                _finding(
                    "qa.author_required_marker",
                    "fail",
                    f"{rel}: inject {iid} is author_required but the file has no {PLACEHOLDER} placeholder",
                    owner,
                    rel,
                )
            )
    if len(sc.get("noise_floor") or []) < 2:
        out.append(_finding("qa.timeline_noise_floor", "fail", f"{rel}: fewer than 2 noise_floor entries", owner, rel))
    enabled = enabled_templates(repo_root)
    templates = [str(t) for t in (sc.get("golden_templates") or [])]
    if enabled is None:
        out.append(
            _finding(
                "qa.vm_catalogue_unavailable",
                "human",
                f"{VM_CATALOGUE_REL} could not be read; golden templates were not checked",
                ORCHESTRATOR,
                rel,
            )
        )
    else:
        for t in templates:
            if t not in enabled:
                out.append(
                    _finding(
                        "qa.timeline_templates_enabled",
                        "fail",
                        f"{rel}: golden template {t!r} is not enabled in vm_catalogue.csv",
                        owner,
                        rel,
                    )
                )
    listed = {Path(str(v)).name for v in (sc.get("validators") or [])}
    for v in manifest.get("validators", []):
        if v["kind"] == "crit" and Path(v["path"]).name not in listed:
            out.append(
                _finding(
                    "qa.timeline_validators_listed",
                    "fail",
                    f"{rel}: crit validator {Path(v['path']).name} is not in the timeline's validators list",
                    owner,
                    rel,
                )
            )
    return out


# ── defang scan ───────────────────────────────────────────────────────


def check_defang(run: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sub, owner in SCAN_DIRS.items():
        base = run / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            rel = p.relative_to(run).as_posix()
            hits = 0
            for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if PLACEHOLDER in line:
                    continue
                for label, pattern in DEFANG_PATTERNS:
                    # A sha256 digest (64 lowercase hex, e.g. files[].sha256 in a fragment) is a
                    # base64-alphabet run but not a payload; it must not fail a stage.
                    if any(not SHA256_HEX.fullmatch(m.group()) for m in pattern.finditer(line)):
                        hits += 1
                        if hits <= MAX_DEFANG_PER_FILE:
                            out.append(
                                _finding(
                                    "qa.defang",
                                    "fail",
                                    f"{rel}:{lineno}: {label}; replace with '{PLACEHOLDER}: <what a cleared author supplies>'",
                                    owner,
                                    rel,
                                )
                            )
            if hits > MAX_DEFANG_PER_FILE:
                out.append(
                    _finding(
                        "qa.defang",
                        "fail",
                        f"{rel}: {hits - MAX_DEFANG_PER_FILE} more offensive artefacts not listed",
                        owner,
                        rel,
                    )
                )
    return out


# ── entry point ───────────────────────────────────────────────────────


def check_run(run: Path, manifest: dict[str, Any], repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    api, why = _api()
    if api is None:
        out.append(
            _finding(
                "qa.api_unavailable",
                "human",
                f"the API parsers could not be imported ({why}); course, catalogue and engine rules did not run",
                ORCHESTRATOR,
            )
        )
    if "content" in manifest and api is not None:
        out += check_course(run, manifest, repo_root, api)
        if (manifest.get("course") or {}).get("catalogue_code"):
            out += check_catalogue_identity(manifest, repo_root, api)
        else:
            out += check_catalogue(run, manifest, repo_root, api)
    if "scenario" in manifest and api is not None:
        out += check_engine_scenario(run, manifest, api)
    if "injects" in manifest:
        out += check_timeline(run, manifest, repo_root)
    out += check_defang(run)
    return out
