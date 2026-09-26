"""cmi5 package emission and checks for ARC².

The Package Builder does not hand-write ``cmi5.xml``. This module derives the package from the
manifest and the code-generator's module files, so the package cannot disagree with the
manifest by accident, and ``arc2.check`` re-derives everything it verifies.

    python -m arc2.cmi5 ids      <course_code> <module_id> [--objectives M01-O01 ...]
    python -m arc2.cmi5 package  <run> [--base-url https://host/path]
    python -m arc2.cmi5 validate <cmi5.xml>

Layout written under ``<run>/07-bundle/cmi5/`` (zip it with ``cmi5.xml`` at the root)::

    cmi5.xml  cmi5.js  course.js  README.md  images/  videos/
    mod_NNN/index.html  mod_NNN/course-config.json  mod_NNN/content/*.html

One ``<block>`` and one ``<au>`` per module. A module with a quiz gets ``moveOn="Passed"`` and
``masteryScore = quiz.pass_threshold / 100``; one without gets ``moveOn="Completed"``.
Publisher IDs are provisional (``https://ccoe.forces.gc.ca/xapi/arc2/<course code>``) until
Standards binds a performance objective, and every run says so as a human finding.

XSD validation is a ladder that never passes vacuously: the stdlib structural check always
runs; ``CourseStructure.xsd`` is checked when lxml is importable and the schema is vendored
under ``tools/arc2/vendor/``, and otherwise reported as a ``human`` finding. lxml is not a
declared dependency of this package: ``python-docx==1.1.2`` (control-plane/api/requirements.txt)
installs it, so it is present wherever the API tests run, and its absence is reported, not
assumed away.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

NS = "https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd"
ARC2_ID_BASE = "https://ccoe.forces.gc.ca/xapi/arc2"
LANG = "en-CA"
PACKAGE_ROOT = "07-bundle/cmi5"
CONFIG_SCHEMA = "arc2/course-config/0.1"
STAGE = "package-builder"
MOVE_ON = ("NotApplicable", "Passed", "Completed", "CompletedAndPassed", "CompletedOrPassed")
MASTERY_MOVE_ON = ("Passed", "CompletedAndPassed", "CompletedOrPassed")
LAUNCH_METHODS = ("AnyWindow", "OwnWindow")
AU_CHILD_ORDER = ("title", "description", "objectives", "url", "launchParameters", "entitlementKey")
# Mirrors tests/api/test_course_content_ingest.py::test_no_fabricated_framework_mappings_are_present.
FRAMEWORK_TOKENS = ("de-rs-", "dcwf", "cc-3", "nice_dcwf", "csf sub-categor")
TEXT_SUFFIXES = frozenset({".xml", ".html", ".htm", ".js", ".json", ".md", ".txt", ".css", ".csv", ".yaml", ".yml"})
AU_DIR = Path(__file__).with_name("au")
VENDOR_DIR = Path(__file__).with_name("vendor")
XSD_PATH = VENDOR_DIR / "CourseStructure.xsd"


class Cmi5Error(Exception):
    """The package cannot be written from this run."""


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── identifiers ───────────────────────────────────────────────────────


def course_iri(course_code: str) -> str:
    return f"{ARC2_ID_BASE}/{course_code.lower()}"


def publisher_ids(course_code: str, module_id: str, objective_ids: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Deterministic publisher IRIs, so stage 2 can write them and stage 7 re-derives the same."""
    base = course_iri(course_code)
    return {
        "course_id": base,
        "block_id": f"{base}/block/{module_id}",
        "au_id": f"{base}/au/{module_id}",
        "objective_iris": {o: f"{base}/obj/{o}" for o in objective_ids},
    }


def mastery_score(module: dict[str, Any]) -> float | None:
    quiz = module.get("quiz")
    return round(quiz["pass_threshold"] / 100, 4) if quiz else None


def build_block(manifest: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
    """The manifest's ``cmi5`` key, derived from course, objectives and content."""
    for key in ("course", "objectives", "content"):
        if key not in manifest:
            raise Cmi5Error(f"manifest has no {key!r}; stages 1 and 2 must be merged before packaging")
    code = manifest["course"]["code"]
    objective_ids = [o["id"] for o in manifest["objectives"]]
    aus = []
    for m in sorted(manifest["content"]["modules"], key=lambda m: m["ordinal"]):
        ids = publisher_ids(code, m["id"])
        ms = mastery_score(m)
        au: dict[str, Any] = {
            "id": ids["au_id"],
            "module_id": m["id"],
            "block_id": ids["block_id"],
            "objective_ids": list(m["objective_ids"]),
            "moveOn": "Passed" if ms is not None else "Completed",
            "launchMethod": "AnyWindow",
            "url": f"{m['id']}/index.html",
            "launchParameters": json.dumps({"module": m["id"], "lang": LANG}),
            "config": f"{m['id']}/course-config.json",
        }
        if ms is not None:
            au["masteryScore"] = ms
        aus.append(au)
    return {
        "course_id": course_iri(code),
        "id_scheme": "arc2-provisional",
        "package_root": PACKAGE_ROOT,
        "base_url": base_url,
        "lang": LANG,
        "objective_iris": publisher_ids(code, "-", objective_ids)["objective_iris"],
        "aus": aus,
        "xsd": {"status": "not_run", "errors": []},
    }


def au_url(block: dict[str, Any], au: dict[str, Any]) -> str:
    base = block.get("base_url")
    return f"{base.rstrip('/')}/{au['url']}" if base else au["url"]


# ── cmi5.xml writer ───────────────────────────────────────────────────


def _langstring(parent: ET.Element, tag: str, text: str) -> None:
    el = ET.SubElement(parent, q(tag))
    ls = ET.SubElement(el, q("langstring"), {"lang": LANG})
    ls.text = text


def _decimal(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def write_course_structure(manifest: dict[str, Any], block: dict[str, Any] | None = None) -> str:
    block = block or manifest["cmi5"]
    course = manifest["course"]
    objective_text = {o["id"]: o["text"] for o in manifest["objectives"]}
    modules = {m["id"]: m for m in manifest["content"]["modules"]}
    ET.register_namespace("", NS)
    root = ET.Element(q("courseStructure"))
    c = ET.SubElement(root, q("course"), {"id": block["course_id"]})
    _langstring(c, "title", course["title"])
    _langstring(c, "description", f"{course['summary']} (ARC2 draft, proposed, not standards-validated)")
    objs = ET.SubElement(root, q("objectives"))
    for oid, iri in block["objective_iris"].items():
        o = ET.SubElement(objs, q("objective"), {"id": iri})
        _langstring(o, "title", objective_text.get(oid, oid))
        _langstring(o, "description", objective_text.get(oid, oid))
    for au in block["aus"]:
        m = modules.get(au["module_id"])
        if m is None:
            raise Cmi5Error(f"AU {au['id']} names module {au['module_id']}, which content has no module for")
        title = f"Module {m['ordinal']}: {m['title']}"
        b = ET.SubElement(root, q("block"), {"id": au["block_id"]})
        _langstring(b, "title", title)
        _langstring(b, "description", m["title"])
        attrs = {"id": au["id"], "moveOn": au["moveOn"]}
        if "masteryScore" in au:
            attrs["masteryScore"] = _decimal(au["masteryScore"])
        attrs["launchMethod"] = au["launchMethod"]
        a = ET.SubElement(b, q("au"), attrs)
        _langstring(a, "title", title)
        _langstring(a, "description", m["title"])
        refs = ET.SubElement(a, q("objectives"))
        for oid in au["objective_ids"]:
            if oid not in block["objective_iris"]:
                raise Cmi5Error(f"AU {au['id']} names objective {oid}, which has no IRI")
            ET.SubElement(refs, q("objective"), {"idref": block["objective_iris"][oid]})
        url = ET.SubElement(a, q("url"))
        url.text = au_url(block, au)
        if au.get("launchParameters"):
            lp = ET.SubElement(a, q("launchParameters"))
            lp.text = au["launchParameters"]
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ── structural check (stdlib, always runs) ────────────────────────────


def _ns_children(el: ET.Element) -> list[ET.Element]:
    return [k for k in el if k.tag.startswith("{" + NS + "}")]


def _check_titled(el: ET.Element, what: str, errs: list[str]) -> None:
    for tag in ("title", "description"):
        t = el.find(q(tag))
        if t is None or not [ls for ls in t.findall(q("langstring")) if ls.get("lang")]:
            errs.append(f"{what} {el.get('id') or '?'}: needs <{tag}> with at least one <langstring lang=...>")


def _check_au(au: ET.Element, objective_ids: list[str]) -> list[str]:
    errs: list[str] = []
    aid = au.get("id") or "?"
    order = [local(k.tag) for k in _ns_children(au)]
    expected = [t for t in AU_CHILD_ORDER if t in order]
    if order != expected or any(order.count(t) != 1 for t in ("title", "description", "url")):
        errs.append(
            f"au {aid}: children must be title, description, [objectives], url, [launchParameters], [entitlementKey]; got {order}"
        )
    if au.get("moveOn", "NotApplicable") not in MOVE_ON:
        errs.append(f"au {aid}: moveOn {au.get('moveOn')!r} is not one of {MOVE_ON}")
    ms = au.get("masteryScore")
    if ms is not None:
        try:
            value = float(ms)
        except ValueError:
            value = -1.0
        if not 0 <= value <= 1 or ("." in ms and len(ms.split(".", 1)[1]) > 4):
            errs.append(f"au {aid}: masteryScore {ms!r} must be a decimal in [0, 1] with at most 4 decimals")
    if au.get("launchMethod", "AnyWindow") not in LAUNCH_METHODS:
        errs.append(f"au {aid}: launchMethod {au.get('launchMethod')!r} is not one of {LAUNCH_METHODS}")
    url = au.find(q("url"))
    if url is None or not (url.text or "").strip():
        errs.append(f"au {aid}: <url> is missing or empty")
    for ref in au.iterfind(f"{q('objectives')}/{q('objective')}"):
        if ref.get("idref") not in objective_ids:
            errs.append(f"au {aid}: objective idref {ref.get('idref')!r} does not resolve")
    _check_titled(au, "au", errs)
    return errs


def check_xml_structure(xml_text: str, block: dict[str, Any] | None = None) -> list[str]:
    """Structural rules of CourseStructure.xsd (§9.14) with the stdlib; and, given the manifest
    block, that the document says exactly what the manifest says."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return [f"not well-formed XML: {exc}"]
    if root.tag != q("courseStructure"):
        return [f"root is {root.tag}, expected {q('courseStructure')}"]
    errs: list[str] = []
    names = [local(k.tag) for k in _ns_children(root)]
    if not names or names[0] != "course" or names.count("course") != 1:
        errs.append("exactly one <course> must come first")
    rest = names[1:]
    if rest[:1] == ["objectives"]:
        rest = rest[1:]
    if not rest or any(n not in ("au", "block") for n in rest):
        errs.append("after <course> and an optional <objectives> there must be one or more <au>/<block>")
    course = root.find(q("course"))
    if course is not None:
        if not course.get("id"):
            errs.append("<course> needs an id")
        _check_titled(course, "course", errs)
    objective_ids = [o.get("id") or "" for o in root.iterfind(f"{q('objectives')}/{q('objective')}")]
    if any(not i for i in objective_ids) or len(set(objective_ids)) != len(objective_ids):
        errs.append("objective ids must be present and unique")
    ids: list[str] = []
    aus: list[ET.Element] = []

    def walk(el: ET.Element) -> None:
        for k in el:
            if k.tag == q("block"):
                ids.append(k.get("id") or "")
                if not [x for x in k if x.tag in (q("au"), q("block"))]:
                    errs.append(f"block {k.get('id') or '?'} has no <au>/<block>")
                _check_titled(k, "block", errs)
                walk(k)
            elif k.tag == q("au"):
                ids.append(k.get("id") or "")
                aus.append(k)

    walk(root)
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        errs.append("block and au ids must be present and unique across the document")
    for au in aus:
        errs += _check_au(au, objective_ids)
    if block is not None:
        errs += _compare_with_block(root, aus, objective_ids, block)
    return errs


def _compare_with_block(
    root: ET.Element, aus: list[ET.Element], objective_ids: list[str], block: dict[str, Any]
) -> list[str]:
    errs: list[str] = []
    course = root.find(q("course"))
    if course is not None and course.get("id") != block["course_id"]:
        errs.append(f"course id {course.get('id')!r} differs from manifest {block['course_id']!r}")
    if set(objective_ids) != set(block["objective_iris"].values()):
        errs.append("objective ids differ from the manifest's objective_iris")
    by_id = {a.get("id"): a for a in aus}
    want_ids = {a["id"] for a in block["aus"]}
    if set(by_id) != want_ids:
        errs.append(f"AU ids differ from manifest: xml={sorted(by_id)} manifest={sorted(want_ids)}")
    for a in block["aus"]:
        el = by_id.get(a["id"])
        if el is None:
            continue
        if el.get("moveOn", "NotApplicable") != a["moveOn"]:
            errs.append(f"au {a['id']}: moveOn {el.get('moveOn')!r} differs from manifest {a['moveOn']!r}")
        want, got = a.get("masteryScore"), el.get("masteryScore")
        if (want is None) != (got is None) or (want is not None and abs(float(got) - want) > 1e-9):
            errs.append(f"au {a['id']}: masteryScore {got!r} differs from manifest {want!r}")
        if (el.findtext(q("url")) or "").strip() != au_url(block, a):
            errs.append(f"au {a['id']}: url differs from manifest {au_url(block, a)!r}")
        refs = {r.get("idref") for r in el.iterfind(f"{q('objectives')}/{q('objective')}")}
        want_refs = {block["objective_iris"][o] for o in a["objective_ids"] if o in block["objective_iris"]}
        if refs != want_refs:
            errs.append(f"au {a['id']}: objective refs differ from manifest")
    return errs


# ── XSD ladder ────────────────────────────────────────────────────────


def validate_xsd(xml_text: str) -> tuple[str, list[str]]:
    """(status, errors): validated | invalid | xsd_not_vendored | validator_unavailable."""
    try:
        from lxml import etree
    except ImportError:
        return "validator_unavailable", ["lxml is not importable in this interpreter; the XSD check did not run"]
    if not XSD_PATH.is_file():
        return "xsd_not_vendored", [f"{XSD_PATH} is missing; see {VENDOR_DIR / 'README.md'} (human, off-box fetch)"]
    parser = etree.XMLParser(no_network=True, resolve_entities=False)
    try:
        schema = etree.XMLSchema(etree.parse(str(XSD_PATH), parser))
        doc = etree.fromstring(xml_text.encode("utf-8"), parser)
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError) as exc:
        return "invalid", [f"could not load the schema or the document: {exc}"]
    if schema.validate(doc):
        return "validated", []
    return "invalid", [f"line {e.line}: {e.message}" for e in schema.error_log]


# ── package writer ────────────────────────────────────────────────────


def write_package(
    run: Path, manifest: dict[str, Any], base_url: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Write ``<run>/07-bundle/cmi5/`` from the manifest and the code-generator's files.

    Returns the ``cmi5`` manifest block and the ``files`` entries for the fragment.
    """
    block = build_block(manifest, base_url)
    root = run / PACKAGE_ROOT
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    files: list[dict[str, Any]] = []
    all_objectives = list(block["objective_iris"])

    def add(rel: str, kind: str, objective_ids: list[str]) -> None:
        files.append(
            {
                "path": f"{PACKAGE_ROOT}/{rel}",
                "stage": STAGE,
                "kind": kind,
                "objective_ids": objective_ids,
                "sha256": sha256_file(root / rel),
            }
        )

    xml_text = write_course_structure(manifest, block)
    (root / "cmi5.xml").write_text(xml_text)
    add("cmi5.xml", "package", all_objectives)
    for name in ("cmi5.js", "course.js"):
        shutil.copyfile(AU_DIR / name, root / name)
        add(name, "runtime", [])
    course = manifest["course"]
    readme = (AU_DIR / "README.md.tmpl").read_text()
    for key, value in {
        "{{COURSE_CODE}}": course["code"],
        "{{COURSE_TITLE}}": course["title"],
        "{{COURSE_ID}}": block["course_id"],
        "{{AU_COUNT}}": str(len(block["aus"])),
    }.items():
        readme = readme.replace(key, value)
    (root / "README.md").write_text(readme)
    add("README.md", "runtime", [])
    for sub in ("images", "videos"):
        (root / sub).mkdir()
        (root / sub / ".gitkeep").write_text("")
        add(f"{sub}/.gitkeep", "runtime", [])
    for media in manifest.get("artifacts", {}).get("media", []):
        src = run / media["path"]
        if src.is_file():
            sub = "images" if media["kind"] == "image" else "videos"
            shutil.copyfile(src, root / sub / src.name)
            add(f"{sub}/{src.name}", "package", all_objectives)

    modules = {m["id"]: m for m in manifest["content"]["modules"]}
    index_tmpl = (AU_DIR / "index.html.tmpl").read_text()
    for au in block["aus"]:
        m = modules[au["module_id"]]
        mdir = root / au["module_id"]
        (mdir / "content").mkdir(parents=True)
        page_html = (
            index_tmpl.replace("{{TITLE}}", html.escape(f"Module {m['ordinal']}: {m['title']}"))
            .replace("{{MODULE_ID}}", m["id"])
            .replace("{{LANG}}", LANG)
        )
        (mdir / "index.html").write_text(page_html)
        add(f"{m['id']}/index.html", "package", list(au["objective_ids"]))
        src_cfg = run / m["config"]
        if not src_cfg.is_file():
            raise Cmi5Error(f"{m['id']}: {m['config']} is missing; the code-generator writes it")
        shutil.copyfile(src_cfg, mdir / "course-config.json")
        add(f"{m['id']}/course-config.json", "package", list(au["objective_ids"]))
        for page in m["pages"]:
            src = run / page
            if not src.is_file():
                raise Cmi5Error(f"{m['id']}: page {page} is missing; the code-generator writes it")
            shutil.copyfile(src, mdir / "content" / src.name)
            add(f"{m['id']}/content/{src.name}", "package", list(au["objective_ids"]))
    status, errors = validate_xsd(xml_text)
    block["xsd"] = {"status": status, "errors": errors}
    return block, files


def update_fragment(run: Path, block: dict[str, Any], files: list[dict[str, Any]]) -> Path:
    """Put the block and the package's file entries into 07-bundle/fragment.json, keeping the rest."""
    path = run / "07-bundle" / "fragment.json"
    fragment: dict[str, Any] = {}
    if path.is_file():
        try:
            fragment = json.loads(path.read_text())
        except ValueError as exc:
            raise Cmi5Error(f"{path} is not valid JSON: {exc}") from exc
    fragment["cmi5"] = block
    kept = [f for f in fragment.get("files", []) if not str(f.get("path", "")).startswith(PACKAGE_ROOT + "/")]
    fragment["files"] = kept + files
    path.write_text(json.dumps(fragment, indent=2) + "\n")
    return path


# ── checks (called by arc2.check) ─────────────────────────────────────


def _finding(check: str, severity: str, message: str, owner: str = STAGE, path: str | None = None) -> dict[str, Any]:
    return {"check": check, "severity": severity, "owner_stage": owner, "message": message, "path": path}


def _check_config(
    cfg: dict[str, Any], au: dict[str, Any], module: dict[str, Any], run: Path, root: Path, block: dict[str, Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    where = au["config"]

    def bad(msg: str, owner: str = "code-generator") -> None:
        out.append(_finding("cmi5.config_matches_manifest", "fail", f"{where}: {msg}", owner, where))

    if cfg.get("schema") != CONFIG_SCHEMA:
        bad(f"schema {cfg.get('schema')!r} != {CONFIG_SCHEMA!r}")
    for key, want in (
        ("module_id", au["module_id"]),
        ("au_id", au["id"]),
        ("moveOn", au["moveOn"]),
        ("masteryScore", au.get("masteryScore")),
        ("lang", block["lang"]),
    ):
        if cfg.get(key) != want:
            bad(f"{key} {cfg.get(key)!r} != manifest {want!r}")
    if list(cfg.get("objective_ids") or []) != list(au["objective_ids"]):
        bad(f"objective_ids {cfg.get('objective_ids')!r} != manifest {au['objective_ids']!r}")
    pages = [Path(p).name for p in (cfg.get("content") or {}).get("pages") or []]
    want_pages = [Path(p).name for p in module["pages"]]
    if pages != want_pages:
        bad(f"content.pages {pages} != manifest pages {want_pages}")
    quiz, want_quiz = cfg.get("quiz"), module.get("quiz")
    if bool(quiz) != bool(want_quiz):
        bad("quiz present in one of config and manifest but not the other")
    elif quiz and (
        len(quiz.get("questions") or []) != want_quiz["question_count"]
        or quiz.get("pass_threshold") != want_quiz["pass_threshold"]
    ):
        bad(
            f"quiz has {len(quiz.get('questions') or [])} questions / threshold {quiz.get('pass_threshold')}, manifest says {want_quiz}"
        )
    src = run / module["config"]
    if src.is_file() and sha256_file(src) != sha256_file(root / au["config"]):
        bad(f"packaged copy differs from {module['config']}", STAGE)
    return out


def check_package(run: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Every ``cmi5.*`` finding for the run, plus the XSD status to record in the manifest."""
    block = manifest.get("cmi5")
    if block is None:
        return [], None
    out: list[dict[str, Any]] = []
    objectives = {o["id"] for o in manifest.get("objectives", [])}
    modules = {m["id"]: m for m in manifest.get("content", {}).get("modules", [])}
    root = run / block["package_root"]

    if set(block["objective_iris"]) != objectives:
        out.append(
            _finding(
                "cmi5.objective_iris",
                "fail",
                f"objective_iris keys {sorted(block['objective_iris'])} differ from objectives {sorted(objectives)}",
            )
        )
    seen_ids: set[str] = set()
    seen_blocks: set[str] = set()
    seen_modules: set[str] = set()
    covered: set[str] = set()
    for au in block["aus"]:
        aid = au["id"]
        if not au["objective_ids"]:
            out.append(_finding("cmi5.au_objectives_nonempty", "fail", f"AU {aid} has no objectives"))
        for oid in au["objective_ids"]:
            if oid not in objectives:
                out.append(_finding("cmi5.au_objective_resolves", "fail", f"AU {aid} names unknown objective {oid}"))
            covered.add(oid)
        if aid in seen_ids:
            out.append(_finding("cmi5.au_ids_unique", "fail", f"duplicate AU id {aid}"))
        seen_ids.add(aid)
        if au["block_id"] in seen_blocks:
            out.append(_finding("cmi5.block_ids_unique", "fail", f"duplicate block id {au['block_id']}"))
        seen_blocks.add(au["block_id"])
        if au["module_id"] in seen_modules:
            out.append(_finding("cmi5.one_au_per_module", "fail", f"module {au['module_id']} has more than one AU"))
        seen_modules.add(au["module_id"])
        module = modules.get(au["module_id"])
        if module is None:
            out.append(
                _finding(
                    "cmi5.module_exists",
                    "fail",
                    f"AU {aid} names module {au['module_id']}, which content has no module for",
                )
            )
            continue
        if au["moveOn"] == "NotApplicable" and module["is_required"]:
            out.append(
                _finding(
                    "cmi5.moveon_allowed",
                    "fail",
                    f"AU {aid}: NotApplicable is satisfied immediately; not allowed on a required module",
                )
            )
        want, got = mastery_score(module), au.get("masteryScore")
        if got is not None and round(got, 4) != got:
            out.append(_finding("cmi5.mastery_score", "fail", f"AU {aid}: masteryScore {got} has more than 4 decimals"))
        if want is None and got is not None:
            out.append(
                _finding(
                    "cmi5.mastery_score", "fail", f"AU {aid} has a masteryScore but module {module['id']} has no quiz"
                )
            )
        elif want is not None and got is not None and abs(got - want) > 1e-9:
            out.append(
                _finding(
                    "cmi5.mastery_score", "fail", f"AU {aid}: masteryScore {got} != quiz.pass_threshold/100 = {want}"
                )
            )
        if module.get("quiz") and module["pass_threshold"] != module["quiz"]["pass_threshold"]:
            out.append(
                _finding(
                    "cmi5.mastery_score",
                    "fail",
                    f"module {module['id']}: pass_threshold {module['pass_threshold']} != quiz.pass_threshold {module['quiz']['pass_threshold']}",
                    "code-generator",
                )
            )
        index = root / au["url"]
        if not index.is_file():
            out.append(
                _finding(
                    "cmi5.url_resolves",
                    "fail",
                    f"AU {aid}: {block['package_root']}/{au['url']} does not exist",
                    path=au["url"],
                )
            )
        else:
            text = index.read_text(errors="replace")
            for ref in ("../cmi5.js", "../course.js"):
                if ref not in text:
                    out.append(_finding("cmi5.au_layout", "fail", f"{au['url']} does not load {ref}", path=au["url"]))
        cfg_path = root / au["config"]
        if not cfg_path.is_file():
            out.append(_finding("cmi5.au_layout", "fail", f"AU {aid}: {au['config']} is missing", path=au["config"]))
            continue
        try:
            cfg = json.loads(cfg_path.read_text())
        except ValueError as exc:
            out.append(
                _finding("cmi5.au_layout", "fail", f"{au['config']} is not valid JSON: {exc}", path=au["config"])
            )
            continue
        out += _check_config(cfg, au, module, run, root, block)
    for oid in sorted(objectives - covered):
        out.append(_finding("cmi5.objective_covered", "fail", f"objective {oid} is in no AU"))

    for name in ("cmi5.js", "course.js"):
        p = root / name
        if not p.is_file():
            out.append(_finding("cmi5.au_layout", "fail", f"{name} is missing from the package", path=name))
        elif sha256_file(p) != sha256_file(AU_DIR / name):
            out.append(_finding("cmi5.au_layout", "fail", f"{name} differs from tools/arc2/au/{name}", path=name))

    xsd: dict[str, Any] = {"status": "not_run", "errors": []}
    xml_path = root / "cmi5.xml"
    if not xml_path.is_file():
        out.append(_finding("cmi5.xml_structure", "fail", "cmi5.xml is missing", path="cmi5.xml"))
    else:
        text = xml_path.read_text()
        for err in check_xml_structure(text, block):
            out.append(_finding("cmi5.xml_structure", "fail", err, path="cmi5.xml"))
        status, errors = validate_xsd(text)
        xsd = {"status": status, "errors": errors}
        if status == "invalid":
            for err in errors:
                out.append(_finding("cmi5.xml_xsd", "fail", err, path="cmi5.xml"))
        elif status != "validated":
            out.append(_finding("cmi5.xml_xsd", "human", errors[0] if errors else status, path="cmi5.xml"))

    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if "instructor" in {part.lower() for part in rel.parts}:
                out.append(
                    _finding(
                        "cmi5.no_instructor_content",
                        "fail",
                        f"{rel.as_posix()} is instructor-only content inside the learner package",
                        path=rel.as_posix(),
                    )
                )
            if p.suffix.lower() in TEXT_SUFFIXES:
                blob = p.read_text(errors="replace").lower()
                for token in FRAMEWORK_TOKENS:
                    if token in blob:
                        out.append(
                            _finding(
                                "cmi5.no_framework_tokens",
                                "fail",
                                f"{rel.as_posix()} contains framework token {token!r}",
                                "code-generator",
                                rel.as_posix(),
                            )
                        )
    if block["id_scheme"] == "arc2-provisional":
        out.append(
            _finding(
                "cmi5.publisher_id_provisional",
                "human",
                f"publisher IDs are provisional ({block['course_id']}); re-mint under the MITE scheme when Standards binds the PO",
            )
        )
    return out, xsd


# ── cli ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arc2.cmi5", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ids", help="print the publisher IRIs for a module (for course-config.json)")
    s.add_argument("course_code")
    s.add_argument("module_id")
    s.add_argument("--objectives", nargs="*", default=[])
    s = sub.add_parser("package", help="write 07-bundle/cmi5/ and put the cmi5 block into 07-bundle/fragment.json")
    s.add_argument("run", type=Path)
    s.add_argument("--base-url")
    s = sub.add_parser("validate", help="structural check and XSD ladder on a cmi5.xml")
    s.add_argument("xml", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "ids":
            print(json.dumps(publisher_ids(args.course_code, args.module_id, args.objectives), indent=2))
            return 0
        if args.cmd == "package":
            manifest_path = args.run / "manifest.json"
            if not manifest_path.is_file():
                raise Cmi5Error(f"no manifest at {manifest_path}")
            manifest = json.loads(manifest_path.read_text())
            block, files = write_package(args.run, manifest, args.base_url)
            fragment = update_fragment(args.run, block, files)
            print(
                f"wrote {args.run / PACKAGE_ROOT}: {len(block['aus'])} AU(s), {len(files)} files; xsd {block['xsd']['status']}"
            )
            print(f"cmi5 block and files added to {fragment}")
            return 0
        if args.cmd == "validate":
            text = args.xml.read_text()
            errs = check_xml_structure(text)
            for err in errs:
                print(f"structure: {err}")
            status, errors = validate_xsd(text)
            print(f"xsd: {status}" + (f" ({errors[0]})" if errors else ""))
            return 1 if errs or status == "invalid" else 0
    except Cmi5Error as exc:
        print(f"arc2.cmi5: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
