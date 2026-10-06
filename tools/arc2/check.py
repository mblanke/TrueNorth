"""ARC² run contract: init, merge, check, gate, status.

The manifest (``build/arc2/<slug>/manifest.json``) is the only shared state between the
seven agents. Each agent writes ``NN-*/fragment.json`` with just the keys it owns; this
module merges them, so no agent ever edits the manifest by hand, and no agent can write a
key another stage owns. ``check`` walks the golden thread and writes ``qa``; ``gate``
records the two human decisions (outline, preview) so a resumed run knows where it stopped.

    python -m arc2.check init   <run> --slug <slug> --request-file <txt> [--enclave]
    python -m arc2.check merge  <run> <stage>
    python -m arc2.check check  <run> [--json] [--dry-run] [--repo-root <dir>]
    python -m arc2.check gate   <run> outline|preview accept|feedback [--text ..] [--route <stage>]
    python -m arc2.check gate   <run> --verify
    python -m arc2.check skip   <run> range-engineer|sensor-gateway --reason <why>
    python -m arc2.check status <run>

Exit codes: 0 ok / pass, 1 fail or rejected, 2 STOP (a stage refused to continue),
3 HUMAN-TAKEOVER (three QA cycles failed).

Findings never pass vacuously: a stage that has not run is itself a finding, and a check
that cannot run (missing tool, missing file) records ``human`` rather than nothing.

Activities (schema 0.2): every module is a ``theory``, ``practical`` or ``range`` activity,
declared in ``01-blueprint/outline.yaml`` (so the outline gate covers it) and mirrored in
``content.modules[].activity``. A run with no range module may mark the range-engineer and
sensor-gateway stages ``not_applicable`` with ``skip``; one with a range module must carry
range evidence (critical events, crit validators, injects, a validated new range template
and a lab profile). A 0.1 manifest has no activities and is read as all-range, its old meaning.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from arc2 import __version__, cmi5, lab_profile
from arc2 import qa as content_qa

SCHEMA_VERSION = "arc2/manifest/0.2"
LEGACY_SCHEMA_VERSION = "arc2/manifest/0.1"  # no activities: every module is a range activity
SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")
REPO_ROOT = Path(__file__).resolve().parents[2]
CROSSWALK_REL = Path("truenorth-content-pack/truenorth-content/crosswalk.csv")
COURSES_REL = Path("content/courses")

ORCHESTRATOR = "orchestrator"
# `qa` is written by `check`, never by a fragment: a stage that could write its own verdict
# could also forge the cycle budget and the preview gate's precondition.
ORCHESTRATOR_KEYS = ("schema_version", "run_id", "slug", "provenance", "stages", "gates", "qa")
SHARED_KEYS = ("files", "human_actions")
# The only files allowed to carry no objective ids: runtime shipped inside the package, never
# course content, so only the package-builder may declare them and only under 07-bundle/.
RUNTIME_BASENAMES = frozenset({"cmi5.js", "course.js", "README.md", "PROMOTE.md", ".gitkeep"})
RUNTIME_PREFIX = "07-bundle/"
PO_BINDABLE_STATUSES = frozenset({"todo", "example"})
# Content-architect rule: objectives use observable verbs; "understand" and "be aware of" are
# banned as the leading verb. Anchored to the start so a later clause cannot trip it.
UNOBSERVABLE = re.compile(r"^\s*(understand|be aware|appreciate|know|be familiar)\b", re.IGNORECASE)
PREVIEW_DIRS = ("02-content", "03-range", "04-artifacts", "05-sensor")
MAX_QA_CYCLES = 3
OUTLINE_REL = Path("01-blueprint/outline.yaml")
RANGE = "range"
ACTIVITY_KINDS = ("theory", "practical", RANGE)
# The stages that only a range activity needs; a run without one marks them not_applicable.
RANGE_STAGES = ("range-engineer", "sensor-gateway")
COMPLETE = frozenset({"done", "not_applicable"})


@dataclass(frozen=True)
class Stage:
    name: str
    dir: str
    keys: tuple[str, ...]


STAGES: tuple[Stage, ...] = (
    Stage("content-architect", "01-blueprint", ("request", "course", "objectives", "critical_events")),
    Stage("code-generator", "02-content", ("content",)),
    Stage("range-engineer", "03-range", ("range", "injects")),
    Stage("artifact-creator", "04-artifacts", ("artifacts",)),
    Stage("sensor-gateway", "05-sensor", ("validators", "scenario", "telemetry_xapi")),
    Stage("qa-tester", "06-qa", ()),  # writes 06-qa/report.md and files entries only
    Stage("package-builder", "07-bundle", ("bundle", "cmi5")),
)
STAGE_ORDER = [s.name for s in STAGES]
AGENT_STAGES = STAGE_ORDER[:5]  # the stages preview feedback can be routed to
PREVIEW_KEYS = tuple(k for s in STAGES[:5] for k in s.keys)  # what the preview gate accepts
KEY_OWNER: dict[str, str] = {k: ORCHESTRATOR for k in ORCHESTRATOR_KEYS}
for _stage in STAGES:
    for _key in _stage.keys:
        KEY_OWNER[_key] = _stage.name


@dataclass
class Finding:
    check: str
    severity: str  # fail | human
    owner_stage: str
    message: str
    path: str | None = None


class ContractError(Exception):
    """A merge or gate request that the contract refuses."""


# ── helpers ───────────────────────────────────────────────────────────


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_tree(root: Path, subdirs: tuple[str, ...]) -> str:
    """Stable digest over (relative path, sha256) of every file in the given subdirs."""
    lines = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                lines.append(f"{p.relative_to(root).as_posix()} {sha256_file(p)}")
    return sha256_bytes("\n".join(lines).encode())


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def outline_digest(run: Path, manifest: dict[str, Any]) -> str | None:
    """What the outline gate accepts: outline.yaml plus the architect's manifest keys, so a
    re-merged blueprint with an unchanged outline.yaml still reopens the gate."""
    outline = run / "01-blueprint" / "outline.yaml"
    if not outline.is_file():
        return None
    keys = {k: manifest.get(k) for k in STAGES[0].keys}
    return sha256_bytes(outline.read_bytes() + b"\n" + canonical(keys))


def preview_digest(run: Path, manifest: dict[str, Any]) -> str:
    """What the preview gate accepts: every file under 02..05 plus the keys stages 1-5 own."""
    keys = {k: manifest.get(k) for k in PREVIEW_KEYS}
    return sha256_bytes(sha256_tree(run, PREVIEW_DIRS).encode() + b"\n" + canonical(keys))


def stage_by_name(name: str) -> Stage:
    for s in STAGES:
        if s.name == name:
            return s
    raise ContractError(f"unknown stage {name!r}; expected one of {', '.join(STAGE_ORDER)}")


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def is_legacy(manifest: dict[str, Any]) -> bool:
    return manifest.get("schema_version") == LEGACY_SCHEMA_VERSION


def outline_activities(run: Path) -> dict[str, str]:
    """module id → activity kind as the outline declares it; tolerant of a hand-broken file
    (whatever cannot be read is simply absent, and the activity checks report it)."""
    try:
        doc = yaml.safe_load((run / OUTLINE_REL).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    modules = doc.get("modules") if isinstance(doc, dict) else None
    out: dict[str, str] = {}
    for m in modules if isinstance(modules, list) else []:
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("activity") in ACTIVITY_KINDS:
            out[m["id"]] = m["activity"]
    return out


def content_activities(manifest: dict[str, Any]) -> dict[str, str]:
    """module id → activity kind from content; a 0.1 module without one is a range activity."""
    out: dict[str, str] = {}
    for m in (manifest.get("content") or {}).get("modules") or []:
        act = m.get("activity")
        if act:
            out[m["id"]] = act["kind"]
        elif is_legacy(manifest):
            out[m["id"]] = RANGE
    return out


def run_activities(run: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """The run's activities: the outline is the design decision, content fills in only what the
    outline does not say (and a 0.1 run is all range)."""
    acts = content_activities(manifest)
    acts.update(outline_activities(run))
    return acts


def has_range(run: Path, manifest: dict[str, Any]) -> bool:
    """True unless the run positively declares no range module. A 0.2 run whose activities are
    not declared yet is treated as needing a range: nothing is skipped by omission."""
    acts = run_activities(run, manifest)
    if is_legacy(manifest) or not acts:
        return True
    return RANGE in acts.values()


def manifest_path(run: Path) -> Path:
    return run / "manifest.json"


def load_manifest(run: Path) -> dict[str, Any]:
    path = manifest_path(run)
    if not path.is_file():
        raise ContractError(f"no manifest at {path}; run `init` first")
    return json.loads(path.read_text())


def save_manifest(run: Path, manifest: dict[str, Any]) -> None:
    manifest_path(run).write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")


def schema_errors(manifest: dict[str, Any]) -> list[jsonschema.ValidationError]:
    validator = jsonschema.Draft7Validator(load_schema())
    return sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path))


def git_state(repo_root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        return head or "nohead", dirty
    except (OSError, subprocess.CalledProcessError):
        return "nohead", False


# ── init ──────────────────────────────────────────────────────────────


def empty_stage() -> dict[str, Any]:
    return {"state": "pending", "attempts": 0, "started_at": None, "finished_at": None, "stop_reason": None}


def empty_gate(preview: bool = False) -> dict[str, Any]:
    gate: dict[str, Any] = {"state": "n/a", "ts": None, "accepted_sha256": None, "feedback": []}
    if preview:
        gate["rework_count"] = 0
    return gate


def init_run(run: Path, slug: str, request_text: str, repo_root: Path, enclave: bool) -> dict[str, Any]:
    if manifest_path(run).exists():
        raise ContractError(
            f"{manifest_path(run)} already exists; resume it with `/arc2 --resume {slug}` or pick a new slug"
        )
    request_text = request_text.strip()
    if not request_text:
        raise ContractError("the course request is empty")
    run.mkdir(parents=True, exist_ok=True)
    for stage in STAGES:
        (run / stage.dir).mkdir(exist_ok=True)
    (run / "request.txt").write_text(request_text + "\n")
    head, dirty = git_state(repo_root)
    created = now()
    inputs = {"request.txt": sha256_bytes(request_text.encode())}
    crosswalk = repo_root / CROSSWALK_REL
    if crosswalk.is_file():
        inputs["crosswalk.csv"] = sha256_file(crosswalk)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": sha256_bytes(f"{slug}{created}".encode())[:12],
        "slug": slug,
        "provenance": {
            "git_head": head,
            "git_dirty": dirty,
            "enclave": enclave,
            "created_at": created,
            "tool_version": __version__,
            "inputs": inputs,
        },
        "stages": {s.name: empty_stage() for s in STAGES},
        "gates": {"outline": empty_gate(), "preview": empty_gate(preview=True)},
        "files": [],
        "human_actions": [],
    }
    errors = schema_errors(manifest)
    if errors:
        raise ContractError("init produced an invalid manifest: " + "; ".join(e.message for e in errors))
    save_manifest(run, manifest)
    return manifest


# ── merge ─────────────────────────────────────────────────────────────


def merge_fragment(run: Path, stage_name: str) -> dict[str, Any]:
    """Fold ``<stage dir>/fragment.json`` into the manifest. Refuses keys the stage does not own."""
    stage = stage_by_name(stage_name)
    manifest = load_manifest(run)
    frag_path = run / stage.dir / "fragment.json"
    if not frag_path.is_file():
        raise ContractError(f"{stage.name}: no fragment at {frag_path}")
    try:
        fragment = json.loads(frag_path.read_text())
    except ValueError as exc:
        raise ContractError(f"{stage.name}: fragment is not valid JSON: {exc}") from exc
    if not isinstance(fragment, dict):
        raise ContractError(f"{stage.name}: fragment must be a JSON object")
    unowned = sorted(k for k in fragment if k not in stage.keys and k not in SHARED_KEYS and k != "stop")
    if unowned:
        owners = ", ".join(f"{k} (owned by {KEY_OWNER.get(k, 'nobody')})" for k in unowned)
        raise ContractError(f"{stage.name} may not write: {owners}")
    for key in SHARED_KEYS:
        for item in fragment.get(key, []):
            if not isinstance(item, dict) or item.get("stage") != stage.name:
                raise ContractError(f"{stage.name}: every {key} entry must carry stage={stage.name!r}")
    _require_upstream(manifest, stage)

    entry = manifest["stages"][stage.name]
    entry["attempts"] += 1
    entry["started_at"] = entry["started_at"] or now()

    if "stop" in fragment:
        entry["state"] = "failed"
        entry["stop_reason"] = str(fragment["stop"]) or "stopped without a reason"
        entry["finished_at"] = now()
        save_manifest(run, manifest)
        raise ContractError(f"STOP from {stage.name}: {entry['stop_reason']}")

    candidate = json.loads(json.dumps(manifest))  # deep copy; only saved if valid
    for key in stage.keys:
        if key in fragment:
            candidate[key] = fragment[key]
    for key in SHARED_KEYS:
        if key in fragment:
            kept = [it for it in candidate[key] if it.get("stage") != stage.name]
            candidate[key] = kept + list(fragment[key])
    errors = schema_errors(candidate)
    if errors:
        detail = "; ".join(f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors)
        raise ContractError(f"{stage.name}: fragment makes the manifest invalid: {detail}")

    candidate["stages"][stage.name].update({"state": "done", "finished_at": now(), "stop_reason": None})
    if candidate.get("qa"):
        candidate["qa"]["result"] = "not_run"  # any new fragment invalidates the last verdict
    if stage.name == "content-architect":
        _reopen_outline_gate(run, candidate)
    save_manifest(run, candidate)
    return candidate


def _require_upstream(manifest: dict[str, Any], stage: Stage) -> None:
    """A stage may only merge after everything before it is done and the gates it sits behind
    are accepted. Without this, a fragment could land ahead of the human decisions."""
    index = STAGE_ORDER.index(stage.name)
    for name in STAGE_ORDER[:index]:
        if manifest["stages"][name]["state"] not in COMPLETE:
            raise ContractError(f"{stage.name} cannot merge: {name} has not finished")
    if index >= 1 and manifest["gates"]["outline"]["state"] != "accepted":
        raise ContractError(f"{stage.name} cannot merge: the outline gate is not accepted")
    if stage.name == "package-builder":
        qa = manifest.get("qa") or {}
        # A failure only package-builder owns (found by the post-package check) is reworked by
        # re-running stage 7; the preview gate below still guards the accepted inputs.
        if qa.get("result") != "pass" and qa.get("rework_stage") != "package-builder":
            raise ContractError("package-builder cannot merge: QA has not passed")
        if manifest["gates"]["preview"]["state"] != "accepted":
            raise ContractError("package-builder cannot merge: the preview gate is not accepted")


def _reopen_outline_gate(run: Path, manifest: dict[str, Any]) -> None:
    gate = manifest["gates"]["outline"]
    current = outline_digest(run, manifest)
    if gate["state"] == "accepted" and current and current == gate["accepted_sha256"]:
        return
    gate["state"] = "pending"
    gate["ts"] = now()


def skip_stage(run: Path, stage_name: str, reason: str) -> dict[str, Any]:
    """Mark a range stage not_applicable. Only the orchestrator calls this, only for a run whose
    accepted outline declares no range module, and never over output the stage already wrote:
    a skipped stage must not leave range keys behind for a check to mistake for evidence."""
    if stage_name not in RANGE_STAGES:
        raise ContractError(f"only {', '.join(RANGE_STAGES)} can be not_applicable, not {stage_name}")
    reason = (reason or "").strip()
    if not reason:
        raise ContractError("skip needs --reason")
    manifest = load_manifest(run)
    if is_legacy(manifest):
        raise ContractError(f"{LEGACY_SCHEMA_VERSION} runs have no activities; every module is a range activity")
    stage = stage_by_name(stage_name)
    _require_upstream(manifest, stage)
    acts = outline_activities(run)
    if not acts:
        raise ContractError(f"{OUTLINE_REL} declares no module activities; a stage is never skipped by omission")
    ranged = sorted(m for m, k in acts.items() if k == RANGE)
    if ranged:
        raise ContractError(f"{stage_name} is needed: {', '.join(ranged)} are range activities")
    present = [k for k in stage.keys if k in manifest]
    if present:
        raise ContractError(f"{stage_name} already wrote {', '.join(present)}; it cannot be not_applicable")
    entry = manifest["stages"][stage_name]
    entry.update({"state": "not_applicable", "finished_at": now(), "stop_reason": reason})
    entry["started_at"] = entry["started_at"] or entry["finished_at"]
    if manifest.get("qa"):
        manifest["qa"]["result"] = "not_run"
    save_manifest(run, manifest)
    return manifest


# ── check ─────────────────────────────────────────────────────────────


def load_crosswalk(repo_root: Path) -> dict[tuple[str, str], dict[str, str]]:
    path = repo_root / CROSSWALK_REL
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {(r["qsp_code"].strip(), r["po_id"].strip()): r for r in csv.DictReader(fh)}


def claimed_pos(repo_root: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
    """(qsp_code, po_code) → course file for every module in content/courses that binds a PO,
    plus the course files that could not be read (their claims are unknown, not absent)."""
    claims: dict[tuple[str, str], str] = {}
    unreadable: list[str] = []
    for path in sorted((repo_root / COURSES_REL).glob("*.yaml")):
        try:
            with path.open(encoding="utf-8-sig") as fh:
                doc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            unreadable.append(path.name)
            continue
        for module in doc.get("modules") or [] if isinstance(doc, dict) else []:
            po = (module or {}).get("po") or {}
            if po.get("qsp_code") and po.get("po_code"):
                claims[(str(po["qsp_code"]).strip(), str(po["po_code"]).strip())] = path.name
    return claims, unreadable


def _owner_for_error(err: jsonschema.ValidationError, manifest: dict[str, Any]) -> str:
    path = list(err.absolute_path)
    top = str(path[0]) if path else ""
    if top in SHARED_KEYS and len(path) > 1 and isinstance(path[1], int):
        entry = manifest.get(top, [])[path[1]] if path[1] < len(manifest.get(top, [])) else {}
        return entry.get("stage") if isinstance(entry, dict) and entry.get("stage") in STAGE_ORDER else ORCHESTRATOR
    return KEY_OWNER.get(top, ORCHESTRATOR)


def _check_schema(manifest: dict[str, Any]) -> list[Finding]:
    out = []
    for err in schema_errors(manifest):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(Finding("schema.valid", "fail", _owner_for_error(err, manifest), f"{where}: {err.message}", where))
    return out


def _check_stages(run: Path, manifest: dict[str, Any]) -> list[Finding]:
    out = []
    ranged = has_range(run, manifest)
    for stage in STAGES:
        st = manifest["stages"][stage.name]
        if st["state"] != "not_applicable":
            continue
        if stage.name not in RANGE_STAGES or ranged:
            why = (
                "only a range stage of a run with no range module"
                if stage.name in RANGE_STAGES
                else "not a range stage"
            )
            out.append(
                Finding(
                    "stage.not_applicable_invalid",
                    "fail",
                    stage.name,
                    f"{stage.name} is not_applicable but must run ({why} may be skipped)",
                )
            )
        present = [k for k in stage.keys if k in manifest]
        if present:
            out.append(
                Finding(
                    "stage.not_applicable_has_output",
                    "fail",
                    stage.name,
                    f"{stage.name} is not_applicable yet {', '.join(present)} is present",
                )
            )
    for stage in STAGES[:5]:
        st = manifest["stages"][stage.name]
        if st["state"] == "not_applicable" and stage.name in RANGE_STAGES and not ranged:
            continue
        if st["state"] != "done":
            msg = f"{stage.name} is {st['state']}" + (f": {st['stop_reason']}" if st.get("stop_reason") else "")
            out.append(Finding("stage.not_done", "fail", stage.name, msg))
    for stage in STAGES:
        if manifest["stages"][stage.name]["state"] != "done":
            continue
        for key in stage.keys:
            if key not in manifest:
                out.append(
                    Finding("stage.fragment_missing", "fail", stage.name, f"{stage.name} is done but {key} is absent")
                )
    return out


def _check_activities(run: Path, manifest: dict[str, Any]) -> list[Finding]:
    """Every module's activity is declared, in the outline and in content, and they agree."""
    out: list[Finding] = []
    if is_legacy(manifest):
        return out
    gate = manifest["gates"]["outline"]
    if gate["state"] == "accepted" and outline_digest(run, manifest) != gate["accepted_sha256"]:
        # The outline changed after its accept: gate.outline_unchanged sends it back to the
        # human, and judging content against an unaccepted outline would misroute the rework.
        return out
    outline = outline_activities(run)
    if manifest["stages"]["content-architect"]["state"] == "done" and not outline:
        out.append(
            Finding(
                "outline.activity_missing",
                "fail",
                "content-architect",
                f"{OUTLINE_REL} gives no module an activity (theory, practical or range)",
                OUTLINE_REL.as_posix(),
            )
        )
    for m in (manifest.get("content") or {}).get("modules") or []:
        act = m.get("activity")
        if not act:
            out.append(
                Finding("content.activity_missing", "fail", "code-generator", f"module {m['id']} declares no activity")
            )
            continue
        want = outline.get(m["id"])
        if outline and want is None:
            out.append(
                Finding(
                    "outline.activity_missing",
                    "fail",
                    "content-architect",
                    f"{OUTLINE_REL} gives module {m['id']} no activity",
                    OUTLINE_REL.as_posix(),
                )
            )
        elif want is not None and want != act["kind"]:
            out.append(
                Finding(
                    "content.activity_mismatch",
                    "fail",
                    "code-generator",
                    f"module {m['id']} is {act['kind']} in content but {want} in the accepted outline",
                )
            )
    return out


def _check_range_evidence(run: Path, manifest: dict[str, Any], repo_root: Path) -> list[Finding]:
    """A range activity needs the environment to exist and to have been checked: critical
    events with crit validators and injects (the old schema minimums), a validated range
    template when one is new, and (0.2) a lab profile. The engine scenario is validated by
    the QA depth checks on every run (``qa.engine_scenario``), not taken from a claim."""
    out: list[Finding] = []
    if not has_range(run, manifest):
        return out
    stages = manifest["stages"]

    def fail(check_name: str, owner: str, message: str, severity: str = "fail") -> None:
        out.append(Finding(check_name, severity, owner, message))

    if stages["content-architect"]["state"] == "done" and not manifest.get("critical_events"):
        fail(
            "range.evidence_critical_events", "content-architect", "a range activity needs at least one critical event"
        )
    if stages["range-engineer"]["state"] == "done":
        injects = manifest.get("injects") or {}
        if not injects.get("items"):
            fail("range.evidence_injects", "range-engineer", "a range activity needs at least one inject")
        if len(injects.get("noise_floor") or []) < 2:
            fail("range.evidence_injects", "range-engineer", "a range activity needs at least two noise-floor entries")
        rng = manifest.get("range") or {}
        tf = (rng.get("terraform_validate") or {}).get("status")
        if rng.get("mode") == "new":
            if tf == "not_installed":
                fail(
                    "range.evidence_terraform",
                    "range-engineer",
                    "terraform was not installed; the new range template is unvalidated",
                    "human",
                )
            elif tf != "passed":
                fail(
                    "range.evidence_terraform",
                    "range-engineer",
                    f"the new range template's validation is {tf}, not passed",
                )
        if not is_legacy(manifest):
            out += _check_lab_profile(run, manifest, repo_root)
    if stages["sensor-gateway"]["state"] == "done" and not manifest.get("validators"):
        fail("range.evidence_validators", "sensor-gateway", "a range activity needs at least one validator")
    return out


def _check_lab_profile(run: Path, manifest: dict[str, Any], repo_root: Path) -> list[Finding]:
    out: list[Finding] = []
    rel = (manifest.get("range") or {}).get("lab_profile")
    if not rel:
        return [Finding("range.lab_profile", "fail", "range-engineer", "range.lab_profile names no lab profile file")]
    path = run / rel
    if not path.is_file():
        return out  # trace.file_exists reports it
    try:
        doc = lab_profile.load(path)
    except (OSError, yaml.YAMLError) as exc:
        return [Finding("range.lab_profile", "fail", "range-engineer", f"{rel}: {exc}", rel)]
    ranged = {m for m, k in run_activities(run, manifest).items() if k == RANGE}
    catalogue = lab_profile.catalogue_ids(repo_root)
    if catalogue is None:
        out.append(
            Finding(
                "range.lab_profile_catalogue",
                "human",
                ORCHESTRATOR,
                "no VM image catalogue found; the lab profile's images could not be checked",
                rel,
            )
        )
    for check_name, message in lab_profile.findings(doc, range_modules=ranged, catalogue=catalogue):
        out.append(Finding(f"range.{check_name}", "fail", "range-engineer", f"{rel}: {message}", rel))
    return out


def _check_trace(manifest: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    objectives = {o["id"]: o for o in manifest.get("objectives", [])}
    ces = {c["id"]: c for c in manifest.get("critical_events", [])}
    modules = {m["id"]: m for m in manifest.get("content", {}).get("modules", [])}
    validators = manifest.get("validators", [])
    files = manifest.get("files", [])
    injects = manifest.get("injects", {}).get("items", [])

    def resolve(ids: list[str], check: str, owner: str, what: str) -> None:
        for oid in ids:
            if oid not in objectives:
                out.append(Finding(check, "fail", owner, f"{what} names unknown objective {oid}"))

    for o in objectives.values():
        if UNOBSERVABLE.search(o["text"]):
            out.append(
                Finding(
                    "objective.observable_verb",
                    "fail",
                    "content-architect",
                    f"{o['id']} is not observable: {o['text']!r}",
                )
            )
        if modules and o["module_id"] not in modules:
            out.append(
                Finding(
                    "trace.objective_module_exists",
                    "fail",
                    "code-generator",
                    f"{o['id']} belongs to {o['module_id']}, which content has no module for",
                )
            )
    for m in modules.values():
        resolve(m["objective_ids"], "trace.module_objectives_resolve", "code-generator", f"module {m['id']}")
    for c in ces.values():
        resolve(c["objective_ids"], "trace.ce_objectives_resolve", "content-architect", f"critical event {c['id']}")

    for f in files:
        base = Path(f["path"]).name
        if f["kind"] == "runtime":
            if (
                base not in RUNTIME_BASENAMES
                or f["stage"] != "package-builder"
                or not f["path"].startswith(RUNTIME_PREFIX)
            ):
                out.append(
                    Finding(
                        "trace.runtime_allowlist",
                        "fail",
                        f["stage"],
                        f"{f['path']} is kind=runtime; only {sorted(RUNTIME_BASENAMES)} under {RUNTIME_PREFIX} from package-builder may be",
                        f["path"],
                    )
                )
            continue
        if not f["objective_ids"]:
            out.append(
                Finding("trace.file_objectives", "fail", f["stage"], f"{f['path']} links to no objective", f["path"])
            )
        resolve(f["objective_ids"], "trace.file_objectives", f["stage"], f["path"])

    covered_by_file = {oid for f in files if f["kind"] in ("content", "artifact") for oid in f["objective_ids"]}
    covered_by_validator = {oid for v in validators for oid in v["objective_ids"]}
    for oid in objectives:
        if oid not in covered_by_file:  # unconditional: an empty files list is not a pass
            out.append(
                Finding(
                    "trace.objective_has_content", "fail", "code-generator", f"{oid} has no content or artifact file"
                )
            )
        if validators and oid not in covered_by_validator:
            out.append(Finding("trace.objective_has_validator", "fail", "sensor-gateway", f"{oid} has no validator"))

    for v in validators:
        resolve(v["objective_ids"], "trace.validator_objectives_resolve", "sensor-gateway", f"validator {v['id']}")
        if v["critical_event_id"] and v["critical_event_id"] not in ces:
            out.append(
                Finding(
                    "trace.crit_validator_ce_resolves",
                    "fail",
                    "sensor-gateway",
                    f"validator {v['id']} names unknown critical event {v['critical_event_id']}",
                )
            )
    if validators:
        for cid in ces:
            ok = [
                v
                for v in validators
                if v["kind"] == "crit"
                and v["critical_event_id"] == cid
                and v["zero_hits_fails"]
                and v["must_pass"]
                and v["opensearch_query"]
            ]
            if not ok:
                out.append(
                    Finding(
                        "trace.ce_has_crit_validator",
                        "fail",
                        "sensor-gateway",
                        f"{cid} has no must-pass crit_ validator with zero_hits_fails",
                    )
                )
        if not any(v["summative"] for v in validators):
            out.append(
                Finding(
                    "trace.summative_manual_ack",
                    "fail",
                    "sensor-gateway",
                    "no summative manual_ack validator; the summative assessment is instructor-acknowledged",
                )
            )

    for inj in injects:
        resolve([inj["objective_id"]], "trace.inject_objectives_resolve", "range-engineer", f"inject {inj['id']}")
        if inj["critical"] and inj["critical_event_id"] not in ces:
            out.append(
                Finding(
                    "trace.critical_inject_has_ce",
                    "fail",
                    "range-engineer",
                    f"critical inject {inj['id']} names unknown critical event {inj['critical_event_id']}",
                )
            )
    if injects:
        hit = {inj["critical_event_id"] for inj in injects if inj["critical"]}
        for cid in ces:
            if cid not in hit:
                out.append(
                    Finding(
                        "trace.ce_has_inject", "fail", "range-engineer", f"{cid} has no critical inject in the timeline"
                    )
                )

    for c in ces.values():
        if not c["text"].strip() or c["text"].strip().upper().startswith("TODO"):
            out.append(
                Finding("ce.text_todo", "fail", "content-architect", f"{c['id']} has placeholder text {c['text']!r}")
            )

    telemetry = manifest.get("telemetry_xapi")
    if telemetry is not None and validators:
        mapped = {t["validator_id"] for t in telemetry}
        by_id = {v["id"]: v for v in validators}
        for v in validators:
            if v["kind"] == "crit" and v["id"] not in mapped:
                out.append(
                    Finding(
                        "trace.crit_validator_xapi",
                        "fail",
                        "sensor-gateway",
                        f"crit validator {v['id']} has no telemetry_xapi mapping",
                    )
                )
        for t in telemetry:
            v = by_id.get(t["validator_id"])
            if v is None:
                out.append(
                    Finding(
                        "trace.xapi_validator_resolves",
                        "fail",
                        "sensor-gateway",
                        f"telemetry_xapi names unknown validator {t['validator_id']}",
                    )
                )
            elif v["summative"] and t["auto_scored"]:
                out.append(
                    Finding(
                        "trace.summative_not_auto_scored",
                        "fail",
                        "sensor-gateway",
                        f"summative validator {v['id']} must not be auto-scored",
                    )
                )
    return out


def _check_files(run: Path, manifest: dict[str, Any], repo_root: Path) -> list[Finding]:
    """Every path the manifest declares must exist. A stage that emits a fragment and no files
    must not pass; a declared sha256 must match what is on disk."""
    out: list[Finding] = []

    def need(rel: str | None, owner: str, what: str, directory: bool = False, root: Path = run) -> None:
        if not rel:
            return
        p = root / rel
        ok = p.is_dir() if directory else p.is_file()
        if not ok:
            out.append(Finding("trace.file_exists", "fail", owner, f"{what} {rel} does not exist", rel))

    for f in manifest.get("files", []):
        need(f["path"], f["stage"], "declared file")
        p = run / f["path"]
        if f.get("sha256") and p.is_file() and sha256_file(p) != f["sha256"]:
            out.append(
                Finding(
                    "trace.file_sha", "fail", f["stage"], f"{f['path']} does not match its declared sha256", f["path"]
                )
            )
    content = manifest.get("content")
    if content:
        need(content["course_yaml"], "code-generator", "course YAML")
        for m in content["modules"]:
            need(m["config"], "code-generator", f"module {m['id']} config")
            for page in m["pages"]:
                need(page, "code-generator", f"module {m['id']} page")
    rng = manifest.get("range")
    if rng:
        need(
            rng["path"],
            "range-engineer",
            "range",
            directory=rng["mode"] == "reuse",
            root=repo_root if rng["mode"] == "reuse" else run,
        )
        need(rng.get("lab_profile"), "range-engineer", "lab profile")
    injects = manifest.get("injects")
    if injects:
        need(injects["timeline"], "range-engineer", "timeline")
    art = manifest.get("artifacts")
    if art:
        for key in ("rubric", "deliverable_template", "variant_b", "xapi_json"):
            need(art[key], "artifact-creator", key)
        need(art["instructor_dir"], "artifact-creator", "instructor dir", directory=True)
    for v in manifest.get("validators", []):
        need(v["path"], "sensor-gateway", f"validator {v['id']}")
    scenario = manifest.get("scenario")
    if scenario:
        need(scenario["path"], "sensor-gateway", "engine scenario")
    return out


def _check_po(manifest: dict[str, Any], repo_root: Path) -> list[Finding]:
    out: list[Finding] = []
    course = manifest.get("course")
    if course is None:
        return out
    crosswalk = load_crosswalk(repo_root)
    if not crosswalk:
        out.append(
            Finding(
                "po.crosswalk_readable",
                "human",
                "content-architect",
                f"crosswalk not found at {repo_root / CROSSWALK_REL}; PO checks could not run",
            )
        )
        return out
    claims, unreadable = claimed_pos(repo_root)
    if unreadable:
        out.append(
            Finding(
                "po.courses_unreadable",
                "human",
                "content-architect",
                f"could not read {', '.join(unreadable)} under {COURSES_REL}; their PO claims are unknown",
            )
        )

    def key(ref: dict[str, str]) -> tuple[str, str]:
        return (ref["qsp_code"], ref["po_code"])

    bound: list[tuple[str, dict[str, str]]] = []
    if course["po"]:
        bound.append(("course", course["po"]))
    bound.extend((o["id"], o["po"]) for o in manifest.get("objectives", []) if o.get("po"))
    for who, ref in bound:
        k = key(ref)
        row = crosswalk.get(k)
        if row is None:
            out.append(
                Finding(
                    "po.bound_exists",
                    "fail",
                    "content-architect",
                    f"{who} binds {k[0]}/{k[1]}, which is not in the crosswalk",
                )
            )
            continue
        if row["status"].strip() not in PO_BINDABLE_STATUSES:
            out.append(
                Finding(
                    "po.bound_status_allowed",
                    "fail",
                    "content-architect",
                    f"{who} binds {k[0]}/{k[1]} with status {row['status']!r}; only {sorted(PO_BINDABLE_STATUSES)} may be bound",
                )
            )
        if k in claims:
            out.append(
                Finding(
                    "po.bound_claimed",
                    "human",
                    "content-architect",
                    f"{k[0]}/{k[1]} is already delivered by {claims[k]}; Standards must release or re-home it before promotion",
                )
            )
    for cand in course["po_candidates"]:
        k = key(cand)
        if k not in crosswalk:
            out.append(
                Finding(
                    "po.candidate_exists",
                    "fail",
                    "content-architect",
                    f"candidate {k[0]}/{k[1]} is not in the crosswalk",
                )
            )
            continue
        held = f" (currently delivered by {claims[k]})" if k in claims else ""
        out.append(
            Finding(
                "po.candidate_needs_standards",
                "human",
                "content-architect",
                f"candidate {k[0]}/{k[1]} left unbound{held}: Standards decides the binding",
            )
        )
    for ce in manifest.get("critical_events", []):
        if ce["source"] != "crosswalk" or not ce["crosswalk_ref"]:
            continue
        row = crosswalk.get(key(ce["crosswalk_ref"]))
        if row is None:
            out.append(
                Finding(
                    "po.ce_ref_exists",
                    "fail",
                    "content-architect",
                    f"{ce['id']} cites a crosswalk row that does not exist",
                )
            )
            continue
        allowed = [s.strip() for s in row["critical_events"].split(";") if s.strip()]
        if ce["text"].strip() not in allowed:
            out.append(
                Finding(
                    "ce.text_verbatim",
                    "fail",
                    "content-architect",
                    f"{ce['id']} text {ce['text']!r} is not a verbatim item of the crosswalk row ({allowed})",
                )
            )
    return out


def _check_gates(run: Path, manifest: dict[str, Any]) -> list[Finding]:
    out = []
    gates = manifest["gates"]
    later_done = any(manifest["stages"][s]["state"] == "done" for s in STAGE_ORDER[1:])
    if later_done and gates["outline"]["state"] != "accepted":
        out.append(
            Finding("gate.outline_accepted", "fail", ORCHESTRATOR, "stages ran past the outline gate without an accept")
        )
    if gates["outline"]["state"] == "accepted" and outline_digest(run, manifest) != gates["outline"]["accepted_sha256"]:
        out.append(
            Finding(
                "gate.outline_unchanged",
                "fail",
                ORCHESTRATOR,
                "the outline or the blueprint keys changed after the outline was accepted; re-open the outline gate",
            )
        )
    if manifest["stages"]["package-builder"]["state"] == "done" and gates["preview"]["state"] != "accepted":
        out.append(
            Finding("gate.preview_accepted", "fail", ORCHESTRATOR, "package-builder ran without a preview accept")
        )
    if gates["preview"]["state"] == "accepted" and preview_digest(run, manifest) != gates["preview"]["accepted_sha256"]:
        out.append(
            Finding(
                "gate.preview_unchanged",
                "fail",
                ORCHESTRATOR,
                "preview files or stage keys changed after they were accepted; re-open the preview gate",
            )
        )
    for which in ("outline", "preview"):
        changed = activity_changes(run, manifest, which)
        if changed:
            out.append(
                Finding(
                    "gate.activity_changed",
                    "fail",
                    ORCHESTRATOR,
                    f"the {which} gate accepted different activities: {'; '.join(changed)}; re-open the {which} gate",
                )
            )
    return out


def activity_changes(run: Path, manifest: dict[str, Any], which: str) -> list[str]:
    """What changed in the module activities since the given gate was accepted, one entry per
    module, so the reviewer sees why the gate re-opened (a digest alone does not say)."""
    g = manifest["gates"][which]
    accepted = g.get("accepted_activities")
    if g["state"] != "accepted" or accepted is None:
        return []
    current = run_activities(run, manifest)
    return [
        f"{mid} {accepted.get(mid, 'absent')} → {current.get(mid, 'absent')}"
        for mid in sorted(set(accepted) | set(current))
        if accepted.get(mid) != current.get(mid)
    ]


def _safe(fn: Any, *args: Any) -> list[Finding]:
    """A hand-broken manifest must produce a finding, not a traceback and no qa."""
    try:
        return fn(*args)
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        return [
            Finding("check.crashed", "fail", ORCHESTRATOR, f"{fn.__name__} could not run on this manifest: {exc!r}")
        ]


def route(findings: list[Finding]) -> str | None:
    """The most upstream agent stage that owns a fail finding, or None."""
    owners = {f.owner_stage for f in findings if f.severity == "fail" and f.owner_stage in STAGE_ORDER}
    for name in STAGE_ORDER:
        if name in owners:
            return name
    return None


def check_run(run: Path, repo_root: Path = REPO_ROOT, write: bool = True) -> tuple[dict[str, Any], list[Finding]]:
    """Run every check and record the verdict. ``write=False`` computes the same verdict
    (the qa block as it would be written) but leaves ``manifest.json`` untouched: no qa,
    no human actions, no stage resets. The qa-tester reports from that; only the
    orchestrator's own ``check`` records and routes."""
    manifest = load_manifest(run)
    findings = _check_schema(manifest)
    findings += _safe(_check_stages, run, manifest)
    findings += _safe(_check_activities, run, manifest)
    findings += _safe(_check_range_evidence, run, manifest, repo_root)
    findings += _safe(_check_trace, manifest)
    findings += _safe(_check_files, run, manifest, repo_root)
    findings += _safe(_check_po, manifest, repo_root)
    findings += _safe(_check_gates, run, manifest)
    try:
        # The package is judged only once package-builder has run on the current inputs; a
        # reset stage 7 leaves a stale 07-bundle/cmi5/ behind that must not fail the rework.
        if manifest["stages"]["package-builder"]["state"] == "done":
            package_findings, xsd = cmi5.check_package(run, manifest)
        else:
            package_findings, xsd = cmi5.check_prepackage(run, manifest), None
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        package_findings, xsd = [_finding_dict("check.crashed", f"cmi5 checks could not run: {exc!r}")], None
    findings += [Finding(**f) for f in package_findings]
    if xsd is not None and isinstance(manifest.get("cmi5"), dict):
        manifest["cmi5"]["xsd"] = xsd  # the ladder's verdict is check's to record, not the agent's
    try:
        depth = content_qa.check_run(run, manifest, repo_root)
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        depth = [_finding_dict("check.crashed", f"content QA checks could not run: {exc!r}")]
    findings += [Finding(**f) for f in depth]

    # Human findings become orchestrator-owned actions. Agents cannot write entries stamped
    # `orchestrator` (merge refuses them), and every run re-asserts the open ones, so no agent
    # can close or delete the action a check raised against it. An action whose cause is gone
    # is closed here, by the check that raised it.
    raised: set[str] = set()
    for f in findings:
        if f.severity != "human":
            continue
        action_id = f"{f.check}:{f.path or f.message}"
        raised.add(action_id)
        category = "standards" if f.check.startswith("po.") else "package" if f.check.startswith("cmi5.") else "qa"
        action = {
            "id": action_id,
            "stage": ORCHESTRATOR,
            "category": category,
            "text": f.message,
            "blocks_promotion": True,
            "status": "open",
        }
        existing = next((a for a in manifest["human_actions"] if a["id"] == action_id), None)
        if existing is None:
            manifest["human_actions"].append(action)
        else:
            existing.update(action)
    for a in manifest["human_actions"]:
        if a["stage"] == ORCHESTRATOR and a["id"] not in raised:
            a["status"] = "done"

    qa = manifest.get("qa") or {
        "result": "not_run",
        "cycle": 0,
        "rework_stage": None,
        "findings": [],
        "checked_at": None,
    }
    fails = [f for f in findings if f.severity == "fail"]
    qa["findings"] = [asdict(f) for f in findings]
    qa["checked_at"] = now()
    if fails:
        qa["rework_stage"] = route(findings)
        # A cycle is a stage that ran and produced failing output. A stage that has not run,
        # or a finding only the orchestrator can fix, is not one: nothing is being re-run.
        counted = qa["rework_stage"] is not None and manifest["stages"][qa["rework_stage"]]["state"] == "done"
        if counted:
            qa["cycle"] = min(qa["cycle"] + 1, MAX_QA_CYCLES)
        qa["result"] = "human_takeover" if qa["cycle"] >= MAX_QA_CYCLES else "fail"
        if qa["rework_stage"]:
            reset_from(manifest, qa["rework_stage"], through="package-builder")
            preview = manifest["gates"]["preview"]
            if qa["rework_stage"] in AGENT_STAGES and preview["state"] == "accepted":
                # Upstream content will change: the human re-accepts after the next QA pass.
                preview.update({"state": "n/a", "ts": now(), "accepted_sha256": None})
    else:
        qa["result"] = "pass"
        qa["rework_stage"] = None
        qa["cycle"] = 0  # a pass closes the loop; the budget is for consecutive failures
        preview = manifest["gates"]["preview"]
        if (
            preview["state"] != "accepted"
            or preview["accepted_sha256"] != preview_digest(run, manifest)
            or activity_changes(run, manifest, "preview")
        ):
            preview["state"] = "pending"
            preview["ts"] = now()
    manifest["qa"] = qa
    if write:
        save_manifest(run, manifest)
    return manifest, findings


def _finding_dict(check: str, message: str) -> dict[str, Any]:
    return {"check": check, "severity": "fail", "owner_stage": ORCHESTRATOR, "message": message, "path": None}


def reset_from(manifest: dict[str, Any], stage_name: str, through: str = "package-builder") -> None:
    """Set ``stage_name`` and every later stage up to ``through`` back to pending."""
    start, stop = STAGE_ORDER.index(stage_name), STAGE_ORDER.index(through)
    for name in STAGE_ORDER[start : stop + 1]:
        st = manifest["stages"][name]
        st.update({"state": "pending", "finished_at": None, "stop_reason": None})


# ── gate ──────────────────────────────────────────────────────────────


def gate(
    run: Path,
    which: str,
    action: str,
    text: str | None = None,
    routed_to: str | None = None,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if which not in ("outline", "preview"):
        raise ContractError("gate must be 'outline' or 'preview'")
    if action not in ("accept", "feedback"):
        raise ContractError("action must be 'accept' or 'feedback'")
    if which == "preview" and action == "accept":
        # What the human accepts is what the checks saw: re-run them now rather than trust
        # a verdict recorded before the last edit.
        manifest, _ = check_run(run, repo_root)
        if manifest["qa"]["result"] != "pass":
            raise ContractError(f"preview gate: QA is {manifest['qa']['result']}, not pass")
    else:
        manifest = load_manifest(run)
    g = manifest["gates"][which]
    stages = manifest["stages"]

    if which == "outline":
        if stages["content-architect"]["state"] != "done":
            raise ContractError("outline gate: content-architect has not finished")
        digest = outline_digest(run, manifest)
        if digest is None:
            raise ContractError(f"outline gate: {run / '01-blueprint' / 'outline.yaml'} is missing")
        if action == "accept":
            try:
                yaml.safe_load((run / OUTLINE_REL).read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ContractError(f"outline gate: {OUTLINE_REL} is not valid YAML; nothing to accept ({exc})") from exc
            g.update(
                {
                    "state": "accepted",
                    "ts": now(),
                    "accepted_sha256": digest,
                    "accepted_activities": run_activities(run, manifest),
                }
            )
        else:
            _add_feedback(g, text)
            if manifest.get("qa"):
                manifest["qa"].update({"result": "not_run", "rework_stage": None})
            reset_from(manifest, "content-architect")
    else:
        if action == "accept":
            g.update(
                {
                    "state": "accepted",
                    "ts": now(),
                    "accepted_sha256": preview_digest(run, manifest),
                    "accepted_activities": run_activities(run, manifest),
                }
            )
        else:
            if routed_to not in AGENT_STAGES:
                raise ContractError(f"preview feedback needs --route <stage>, one of {', '.join(AGENT_STAGES)}")
            _add_feedback(g, text, routed_to)
            g["rework_count"] += 1
            if manifest.get("qa"):
                manifest["qa"].update({"cycle": 0, "result": "not_run", "rework_stage": None})
            reset_from(manifest, routed_to, through="package-builder")
    save_manifest(run, manifest)
    return manifest


def _add_feedback(g: dict[str, Any], text: str | None, routed_to: str | None = None) -> None:
    text = (text or "").strip()
    if not text:
        raise ContractError("feedback needs --text")
    entry: dict[str, Any] = {"text": text, "ts": now(), "round": len(g["feedback"]) + 1}
    if routed_to:
        entry["routed_to"] = routed_to
    g["feedback"].append(entry)
    g["state"] = "feedback"
    g["ts"] = entry["ts"]


def gate_verify(run: Path) -> list[str]:
    """Re-check accepted shas; flip a gate back to pending when its inputs changed."""
    manifest = load_manifest(run)
    flipped = []
    outline_gate = manifest["gates"]["outline"]
    if outline_gate["state"] == "accepted" and (
        outline_digest(run, manifest) != outline_gate["accepted_sha256"] or activity_changes(run, manifest, "outline")
    ):
        outline_gate.update({"state": "pending", "ts": now()})
        flipped.append("outline")
    preview_gate = manifest["gates"]["preview"]
    if preview_gate["state"] == "accepted" and (
        preview_digest(run, manifest) != preview_gate["accepted_sha256"] or activity_changes(run, manifest, "preview")
    ):
        preview_gate.update({"state": "pending", "ts": now()})
        flipped.append("preview")
    if flipped:
        save_manifest(run, manifest)
    return flipped


# ── status ────────────────────────────────────────────────────────────


def status_text(run: Path, manifest: dict[str, Any]) -> str:
    prov = manifest["provenance"]
    stages = manifest["stages"]
    gates = manifest["gates"]
    qa = manifest.get("qa") or {}
    course = manifest.get("course") or {}
    ces = manifest.get("critical_events", [])
    validators = manifest.get("validators", [])
    covered = sum(1 for c in ces if any(v["kind"] == "crit" and v["critical_event_id"] == c["id"] for v in validators))
    open_actions = [a for a in manifest["human_actions"] if a["status"] == "open"]
    promote = run / "07-bundle" / "PROMOTE.md"

    def stage_cell(i: int, s: Stage) -> str:
        st = stages[s.name]
        return f"{i} {s.name} {st['state']}({st['attempts']})"

    def gate_cell(name: str) -> str:
        g = gates[name]
        rounds = len(g["feedback"])
        extra = f" since {g['ts']}" if g["ts"] else ""
        if name == "preview":
            rounds = g.get("rework_count", rounds)
        return f"{name} {g['state'].upper()}{extra} (feedback rounds {rounds})"

    acts = run_activities(run, manifest)
    kinds = ", ".join(f"{k} {sum(1 for v in acts.values() if v == k)}" for k in ACTIVITY_KINDS)
    lines = [
        f"run {manifest['slug']}  {run}  HEAD {prov['git_head']} dirty:{'yes' if prov['git_dirty'] else 'no'}  enclave:{int(prov['enclave'])}",
        "stages  " + " | ".join(stage_cell(i, s) for i, s in enumerate(STAGES, 1)),
        f"gates   {gate_cell('outline')} | {gate_cell('preview')}",
        f"objectives {len(manifest.get('objectives', []))} | PO bound {1 if course.get('po') else 0} / candidates {len(course.get('po_candidates', []))} | "
        f"CE covered {covered}/{len(ces)} | QA {qa.get('result', 'not_run')} cycle {qa.get('cycle', 0)}/{MAX_QA_CYCLES}"
        + (f" rework→{qa['rework_stage']}" if qa.get("rework_stage") else "")
        + f" | human actions {len(open_actions)} | PROMOTE.md {'yes' if promote.is_file() else '-'}",
        f"modules {kinds}",
    ]
    slug = manifest["slug"]
    if gates["outline"]["state"] == "pending":
        lines += [
            f"next    review {run / '01-blueprint' / 'outline.yaml'}, then",
            f"        /arc2 --resume {slug} accept     |     /arc2 --resume {slug} <feedback>",
        ]
    elif gates["preview"]["state"] == "pending":
        lines += [
            f"next    review {run / '02-content'}, {run / '04-artifacts'}, {run / '06-qa' / 'report.md'}, then",
            f"        /arc2 --resume {slug} accept     |     /arc2 --resume {slug} <feedback>",
        ]
    elif qa.get("result") == "human_takeover":
        lines.append("next    HUMAN-TAKEOVER: three QA cycles failed; fix by hand, then /arc2 --resume " + slug)
    else:
        pending = [s.name for s in STAGES if stages[s.name]["state"] not in COMPLETE]
        lines.append(f"next    {'run ' + pending[0] if pending else 'promotion: follow ' + str(promote)}")
    for a in open_actions:
        lines.append(f"  action [{a['category']}] {a['text']}")
    return "\n".join(lines)


# ── cli ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arc2.check", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the run directory and skeleton manifest")
    s.add_argument("run", type=Path)
    s.add_argument("--slug", required=True)
    s.add_argument("--request-file", required=True, type=Path)
    s.add_argument("--enclave", action="store_true")
    s.add_argument("--repo-root", type=Path, default=REPO_ROOT)

    s = sub.add_parser("merge", help="fold a stage's fragment.json into the manifest")
    s.add_argument("run", type=Path)
    s.add_argument("stage", choices=STAGE_ORDER)

    s = sub.add_parser("check", help="run the contract checks and write qa")
    s.add_argument("run", type=Path)
    s.add_argument("--json", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="compute and print the verdict; write nothing")
    s.add_argument("--repo-root", type=Path, default=REPO_ROOT)

    s = sub.add_parser("gate", help="record a human decision at the outline or preview gate")
    s.add_argument("run", type=Path)
    s.add_argument("which", nargs="?", choices=["outline", "preview"])
    s.add_argument("action", nargs="?", choices=["accept", "feedback"])
    s.add_argument("--text")
    s.add_argument("--route", choices=AGENT_STAGES)
    s.add_argument("--verify", action="store_true")
    s.add_argument("--repo-root", type=Path, default=REPO_ROOT)

    s = sub.add_parser("skip", help="mark a range stage not_applicable for a run with no range module")
    s.add_argument("run", type=Path)
    s.add_argument("stage", choices=RANGE_STAGES)
    s.add_argument("--reason", required=True)

    s = sub.add_parser("status", help="print the one-screen run status")
    s.add_argument("run", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            m = init_run(args.run, args.slug, args.request_file.read_text(), args.repo_root, args.enclave)
            print(f"initialised {args.run} run_id={m['run_id']}")
            return 0
        if args.cmd == "merge":
            m = merge_fragment(args.run, args.stage)
            print(
                f"merged {args.stage}: {m['stages'][args.stage]['state']} (attempt {m['stages'][args.stage]['attempts']})"
            )
            return 0
        if args.cmd == "check":
            m, findings = check_run(args.run, args.repo_root, write=not args.dry_run)
            qa = m["qa"]
            if args.json:
                print(json.dumps(qa, indent=2))
            else:
                for f in findings:
                    print(f"{f.severity.upper():5} {f.check:34} {f.owner_stage:18} {f.message}")
                print(
                    f"QA {qa['result']} cycle {qa['cycle']}/{MAX_QA_CYCLES}"
                    + (f" rework→{qa['rework_stage']}" if qa["rework_stage"] else "")
                )
            return {"pass": 0, "fail": 1, "human_takeover": 3}[qa["result"]]
        if args.cmd == "gate":
            if args.verify:
                flipped = gate_verify(args.run)
                print("gates re-opened: " + ", ".join(flipped) if flipped else "gates unchanged")
                return 1 if flipped else 0
            if not args.which or not args.action:
                raise ContractError("gate needs <outline|preview> <accept|feedback> or --verify")
            m = gate(args.run, args.which, args.action, args.text, args.route, args.repo_root)
            print(f"gate {args.which}: {m['gates'][args.which]['state']}")
            return 0
        if args.cmd == "skip":
            skip_stage(args.run, args.stage, args.reason)
            print(f"{args.stage}: not_applicable")
            return 0
        if args.cmd == "status":
            print(status_text(args.run, load_manifest(args.run)))
            return 0
    except ContractError as exc:
        print(f"arc2: {exc}", file=sys.stderr)
        return 2 if str(exc).startswith("STOP") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
