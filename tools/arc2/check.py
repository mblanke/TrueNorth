"""ARC² run contract: init, merge, check, gate, status.

The manifest (``build/arc2/<slug>/manifest.json``) is the only shared state between the
seven agents. Each agent writes ``NN-*/fragment.json`` with just the keys it owns; this
module merges them, so no agent ever edits the manifest by hand, and no agent can write a
key another stage owns. ``check`` walks the golden thread and writes ``qa``; ``gate``
records the two human decisions (outline, preview) so a resumed run knows where it stopped.

    python -m arc2.check init   <run> --slug <slug> --request-file <txt> [--enclave]
    python -m arc2.check merge  <run> <stage>
    python -m arc2.check check  <run> [--json] [--repo-root <dir>]
    python -m arc2.check gate   <run> outline|preview accept|feedback [--text ..] [--route <stage>]
    python -m arc2.check gate   <run> --verify
    python -m arc2.check status <run>

Exit codes: 0 ok / pass, 1 fail or rejected, 2 STOP (a stage refused to continue),
3 HUMAN-TAKEOVER (three QA cycles failed).

Findings never pass vacuously: a stage that has not run is itself a finding, and a check
that cannot run (missing tool, missing file) records ``human`` rather than nothing.
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
from arc2 import __version__

SCHEMA_VERSION = "arc2/manifest/0.1"
SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")
REPO_ROOT = Path(__file__).resolve().parents[2]
CROSSWALK_REL = Path("truenorth-content-pack/truenorth-content/crosswalk.csv")
COURSES_REL = Path("content/courses")

ORCHESTRATOR = "orchestrator"
ORCHESTRATOR_KEYS = ("schema_version", "run_id", "slug", "provenance", "stages", "gates")
SHARED_KEYS = ("files", "human_actions")
# The only files allowed to carry no objective ids: they are runtime, not course content.
RUNTIME_BASENAMES = frozenset({"cmi5.js", "course.js", "README.md", "PROMOTE.md", ".gitkeep"})
PO_BINDABLE_STATUSES = frozenset({"todo", "example"})
# Objectives must be observable. These verbs are how a course claims learning it cannot see.
UNOBSERVABLE = re.compile(r"\b(understand|be aware|appreciate|know|familiar)\b", re.IGNORECASE)
PREVIEW_DIRS = ("02-content", "03-range", "04-artifacts", "05-sensor")
MAX_QA_CYCLES = 3


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
    Stage("qa-tester", "06-qa", ("qa",)),
    Stage("package-builder", "07-bundle", ("bundle", "cmi5")),
)
STAGE_ORDER = [s.name for s in STAGES]
AGENT_STAGES = STAGE_ORDER[:5]  # the stages preview feedback can be routed to
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


def stage_by_name(name: str) -> Stage:
    for s in STAGES:
        if s.name == name:
            return s
    raise ContractError(f"unknown stage {name!r}; expected one of {', '.join(STAGE_ORDER)}")


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


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
        raise ContractError(f"{manifest_path(run)} already exists; use --resume or a new slug")
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

    entry = manifest["stages"][stage.name]
    entry["attempts"] += 1
    entry["started_at"] = entry["started_at"] or now()

    if "stop" in fragment:
        entry["state"] = "failed"
        entry["stop_reason"] = str(fragment["stop"]) or "stopped without a reason"
        entry["finished_at"] = now()
        save_manifest(run, manifest)
        raise ContractError(f"STOP from {stage.name}: {entry['stop_reason']}")

    unowned = sorted(k for k in fragment if k not in stage.keys and k not in SHARED_KEYS)
    if unowned:
        owners = ", ".join(f"{k} (owned by {KEY_OWNER.get(k, 'nobody')})" for k in unowned)
        raise ContractError(f"{stage.name} may not write: {owners}")

    for key in SHARED_KEYS:
        for item in fragment.get(key, []):
            if not isinstance(item, dict) or item.get("stage") != stage.name:
                raise ContractError(f"{stage.name}: every {key} entry must carry stage={stage.name!r}")

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
    if stage.name == "content-architect":
        _reopen_outline_gate(run, candidate)
    save_manifest(run, candidate)
    return candidate


def _reopen_outline_gate(run: Path, manifest: dict[str, Any]) -> None:
    gate = manifest["gates"]["outline"]
    outline = run / "01-blueprint" / "outline.yaml"
    current = sha256_file(outline) if outline.is_file() else None
    if gate["state"] == "accepted" and current and current == gate["accepted_sha256"]:
        return
    gate["state"] = "pending"
    gate["ts"] = now()


# ── check ─────────────────────────────────────────────────────────────


def load_crosswalk(repo_root: Path) -> dict[tuple[str, str], dict[str, str]]:
    path = repo_root / CROSSWALK_REL
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {(r["qsp_code"].strip(), r["po_id"].strip()): r for r in csv.DictReader(fh)}


def claimed_pos(repo_root: Path) -> dict[tuple[str, str], str]:
    """(qsp_code, po_code) → course file, for every module in content/courses that binds a PO."""
    claims: dict[tuple[str, str], str] = {}
    for path in sorted((repo_root / COURSES_REL).glob("*.yaml")):
        try:
            with path.open(encoding="utf-8-sig") as fh:
                doc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            continue
        for module in doc.get("modules") or []:
            po = (module or {}).get("po") or {}
            if po.get("qsp_code") and po.get("po_code"):
                claims[(str(po["qsp_code"]).strip(), str(po["po_code"]).strip())] = path.name
    return claims


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


def _check_stages(manifest: dict[str, Any]) -> list[Finding]:
    out = []
    for stage in STAGES[:5]:
        st = manifest["stages"][stage.name]
        if st["state"] != "done":
            msg = f"{stage.name} is {st['state']}" + (f": {st['stop_reason']}" if st.get("stop_reason") else "")
            out.append(Finding("stage.not_done", "fail", stage.name, msg))
    for stage in STAGES:
        if manifest["stages"][stage.name]["state"] != "done":
            continue
        for key in stage.keys:
            if key == "qa":
                continue  # written by this module, not by the qa-tester fragment
            if key not in manifest:
                out.append(
                    Finding("stage.fragment_missing", "fail", stage.name, f"{stage.name} is done but {key} is absent")
                )
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
            if base not in RUNTIME_BASENAMES:
                out.append(
                    Finding(
                        "trace.runtime_allowlist",
                        "fail",
                        f["stage"],
                        f"{f['path']} is kind=runtime but only {sorted(RUNTIME_BASENAMES)} may be",
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
        if files and oid not in covered_by_file:
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
    claims = claimed_pos(repo_root)

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
    if gates["outline"]["state"] == "accepted":
        outline = run / "01-blueprint" / "outline.yaml"
        if not outline.is_file() or sha256_file(outline) != gates["outline"]["accepted_sha256"]:
            out.append(
                Finding(
                    "gate.outline_unchanged",
                    "fail",
                    ORCHESTRATOR,
                    "outline.yaml changed after it was accepted; re-open the outline gate",
                )
            )
    if manifest["stages"]["package-builder"]["state"] == "done" and gates["preview"]["state"] != "accepted":
        out.append(
            Finding("gate.preview_accepted", "fail", ORCHESTRATOR, "package-builder ran without a preview accept")
        )
    if (
        gates["preview"]["state"] == "accepted"
        and sha256_tree(run, PREVIEW_DIRS) != gates["preview"]["accepted_sha256"]
    ):
        out.append(
            Finding(
                "gate.preview_unchanged",
                "fail",
                ORCHESTRATOR,
                "preview files changed after they were accepted; re-open the preview gate",
            )
        )
    return out


def route(findings: list[Finding]) -> str | None:
    """The most upstream agent stage that owns a fail finding, or None."""
    owners = {f.owner_stage for f in findings if f.severity == "fail" and f.owner_stage in STAGE_ORDER}
    for name in STAGE_ORDER:
        if name in owners:
            return name
    return None


def check_run(run: Path, repo_root: Path = REPO_ROOT) -> tuple[dict[str, Any], list[Finding]]:
    manifest = load_manifest(run)
    findings = _check_schema(manifest)
    findings += _check_stages(manifest)
    findings += _check_trace(manifest)
    findings += _check_po(manifest, repo_root)
    findings += _check_gates(run, manifest)

    for f in findings:
        if f.severity != "human":
            continue
        action_id = f"{f.check}:{f.path or f.message}"
        if not any(a["id"] == action_id for a in manifest["human_actions"]):
            manifest["human_actions"].append(
                {
                    "id": action_id,
                    "stage": f.owner_stage,
                    "category": "standards" if f.check.startswith("po.") else "qa",
                    "text": f.message,
                    "blocks_promotion": True,
                    "status": "open",
                }
            )

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
        # A cycle is one full pass through stages 1-5; a check on a half-built run is not one.
        complete = all(manifest["stages"][s]["state"] == "done" for s in AGENT_STAGES)
        if complete:
            qa["cycle"] = min(qa["cycle"] + 1, MAX_QA_CYCLES)
        qa["rework_stage"] = route(findings)
        qa["result"] = "human_takeover" if qa["cycle"] >= MAX_QA_CYCLES else "fail"
        if qa["rework_stage"]:
            reset_from(manifest, qa["rework_stage"])
        manifest["stages"]["qa-tester"]["state"] = "pending"
    else:
        qa["result"] = "pass"
        qa["rework_stage"] = None
        preview = manifest["gates"]["preview"]
        if preview["state"] != "accepted" or preview["accepted_sha256"] != sha256_tree(run, PREVIEW_DIRS):
            preview["state"] = "pending"
            preview["ts"] = now()
    manifest["qa"] = qa
    save_manifest(run, manifest)
    return manifest, findings


def reset_from(manifest: dict[str, Any], stage_name: str, through: str = "qa-tester") -> None:
    """Set ``stage_name`` and every later stage up to ``through`` back to pending."""
    start, stop = STAGE_ORDER.index(stage_name), STAGE_ORDER.index(through)
    for name in STAGE_ORDER[start : stop + 1]:
        st = manifest["stages"][name]
        st.update({"state": "pending", "finished_at": None, "stop_reason": None})


# ── gate ──────────────────────────────────────────────────────────────


def gate(run: Path, which: str, action: str, text: str | None = None, routed_to: str | None = None) -> dict[str, Any]:
    manifest = load_manifest(run)
    if which not in ("outline", "preview"):
        raise ContractError("gate must be 'outline' or 'preview'")
    if action not in ("accept", "feedback"):
        raise ContractError("action must be 'accept' or 'feedback'")
    g = manifest["gates"][which]
    stages = manifest["stages"]

    if which == "outline":
        if stages["content-architect"]["state"] != "done":
            raise ContractError("outline gate: content-architect has not finished")
        outline = run / "01-blueprint" / "outline.yaml"
        if not outline.is_file():
            raise ContractError(f"outline gate: {outline} is missing")
        if action == "accept":
            g.update({"state": "accepted", "ts": now(), "accepted_sha256": sha256_file(outline)})
        else:
            _add_feedback(g, text)
            reset_from(manifest, "content-architect")
    else:
        qa = manifest.get("qa") or {}
        if action == "accept":
            if qa.get("result") != "pass":
                raise ContractError("preview gate: QA has not passed")
            g.update({"state": "accepted", "ts": now(), "accepted_sha256": sha256_tree(run, PREVIEW_DIRS)})
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
    outline = run / "01-blueprint" / "outline.yaml"
    if outline_gate["state"] == "accepted" and (
        not outline.is_file() or sha256_file(outline) != outline_gate["accepted_sha256"]
    ):
        outline_gate.update({"state": "pending", "ts": now()})
        flipped.append("outline")
    preview_gate = manifest["gates"]["preview"]
    if preview_gate["state"] == "accepted" and sha256_tree(run, PREVIEW_DIRS) != preview_gate["accepted_sha256"]:
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

    lines = [
        f"run {manifest['slug']}  {run}  HEAD {prov['git_head']} dirty:{'yes' if prov['git_dirty'] else 'no'}  enclave:{int(prov['enclave'])}",
        "stages  " + " | ".join(stage_cell(i, s) for i, s in enumerate(STAGES, 1)),
        f"gates   {gate_cell('outline')} | {gate_cell('preview')}",
        f"objectives {len(manifest.get('objectives', []))} | PO bound {1 if course.get('po') else 0} / candidates {len(course.get('po_candidates', []))} | "
        f"CE covered {covered}/{len(ces)} | QA {qa.get('result', 'not_run')} cycle {qa.get('cycle', 0)}/{MAX_QA_CYCLES}"
        + (f" rework→{qa['rework_stage']}" if qa.get("rework_stage") else "")
        + f" | human actions {len(open_actions)} | PROMOTE.md {'yes' if promote.is_file() else '-'}",
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
        pending = [s.name for s in STAGES if stages[s.name]["state"] != "done"]
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
    s.add_argument("--repo-root", type=Path, default=REPO_ROOT)

    s = sub.add_parser("gate", help="record a human decision at the outline or preview gate")
    s.add_argument("run", type=Path)
    s.add_argument("which", nargs="?", choices=["outline", "preview"])
    s.add_argument("action", nargs="?", choices=["accept", "feedback"])
    s.add_argument("--text")
    s.add_argument("--route", choices=AGENT_STAGES)
    s.add_argument("--verify", action="store_true")

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
            m, findings = check_run(args.run, args.repo_root)
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
            m = gate(args.run, args.which, args.action, args.text, args.route)
            print(f"gate {args.which}: {m['gates'][args.which]['state']}")
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
