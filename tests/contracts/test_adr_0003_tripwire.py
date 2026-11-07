"""Tripwire tests for docs/adr/0003-mosa-ratchet.md.

Each test pins one DECISION the ADR states, quoting the line it pins in its docstring,
and fails the day the code drifts from it. Everything here is read-only over repository
source: the ratchet is exercised by importing scripts/mosa_check.py against a temporary
baseline file (the real collect() runs, nothing is mocked), every other decision is
checked against the committed text of the gate scripts, the CI workflow, the LICENSE and
the image references that consume third-party containers. No network, no live services.

ADR 0003 decisions that are not enforceable in code are deliberately not tested here and
are reported in the worker return instead:
- "Government data rights ... are set **by contract**, not by this file" (ADR lines
  53-56): a legal fact; only the LICENSE sentence restating it is pinned here.
- "Raise a baseline only with a new ADR explaining why" (ADR line 25): no mechanical
  check ties a manual baseline raise to an ADR; scripts/mosa_check.py only lowers
  automatically.
- "Note Elastic-licensed images in `compose.helk.yml` before redistributing that
  overlay" (ADR line 58): the obligation bites at redistribution time, which the tree
  cannot show.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs" / "adr" / "0003-mosa-ratchet.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DOD = ROOT / "scripts" / "dod.sh"

# Where the repo consumes third-party container images: Dockerfiles anywhere, plus the
# declarative `image:` references under the infrastructure, tooling, installer and CI
# trees. Lab content (content/), tests and docs deliberately quote unpinned images; they
# are simulated targets and prose, not the platform's own dependencies.
IMAGE_SCAN_DIRS = ("infra", "tools", "install", ".github/workflows")

_PROPRIETARY_MARKERS = ("All rights reserved", "proprietary")
_OPEN_SOURCE_MARKERS = (
    "Apache License",
    "GNU General Public",
    "Mozilla Public License",
    "Creative Commons",
    "MIT License",
    "BSD 2-Clause",
    "BSD 3-Clause",
)


def _mosa_check():
    """Load scripts/mosa_check.py as a module; importing it has no side effects."""
    spec = importlib.util.spec_from_file_location("mosa_check", ROOT / "scripts" / "mosa_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _adr_metrics() -> list[str]:
    """The metric names in the ADR's ratchet table, parsed from the ADR itself.

    Rows may pack several names in one cell (`lines_api_models_py`, `lines_api_schemas_py`,
    `lines_worker_tasks_py` share a row), so every backticked name in the first column
    counts.
    """
    metrics = []
    for cell in re.findall(r"^\| ([^|]+) \|", ADR.read_text(encoding="utf-8"), re.M):
        metrics += re.findall(r"`([a-z_]+)`", cell)
    assert len(metrics) >= 7, f"ADR 0003 ratchet table not parsed: {metrics}"
    return metrics


def _counts(mod) -> dict[str, int]:
    metrics = mod.collect()
    return {name: (len(v) if isinstance(v, list) else v) for name, v in metrics.items()}


def _job(workflow_text: str, name: str) -> str:
    """The body of one top-level workflow job, from `  name:` to the next job key."""
    matches = list(re.finditer(r"^  ([\w-]+):\s*$", workflow_text, re.M))
    starts = [m for m in matches if m.group(1) == name]
    assert starts, f"workflow has no job `{name}`"
    start = starts[0].start()
    following = [m.start() for m in matches if m.start() > start]
    return workflow_text[start : following[0]] if following else workflow_text[start:]


# --- Decision: the ratchet (ADR 0003 lines 12-25) -----------------------------------------


def test_the_ratchet_runs_in_the_definition_of_done_gate():
    # "`scripts/mosa_check.py` runs in `scripts/dod.sh` and CI."
    text = DOD.read_text(encoding="utf-8")
    assert "scripts/mosa_check.py" in text, "DoD no longer runs the MOSA ratchet"
    for line in text.splitlines():
        if "mosa_check.py" in line and not line.lstrip().startswith("#"):
            assert "|| fail" in line, f"DoD must fail when the ratchet fails: {line!r}"
    # "Rules now: a missing tool is a FAILURE, never a silent skip. No `|| true` anywhere."
    swallowed = [
        ln
        for ln in text.splitlines()
        if "|| true" in ln and not ln.lstrip().startswith("#")
    ]
    assert not swallowed, f"DoD swallows failures again: {swallowed}"


def test_the_ratchet_runs_in_ci_and_fails_on_an_uncommitted_lowered_baseline():
    # "A count above baseline fails, and a count below it lowers the baseline
    # automatically. CI also fails if the lowered baseline isn't committed."
    mosa = _job(CI.read_text(encoding="utf-8"), "mosa")
    assert "python scripts/mosa_check.py" in mosa, "CI no longer runs the MOSA ratchet"
    assert "git diff --exit-code .dod-mosa-baseline" in mosa, (
        "CI no longer fails when an automatically lowered baseline is not committed"
    )


def test_every_metric_the_adr_names_is_counted_and_recorded_in_the_baseline():
    # "It counts the following and records them in `.dod-mosa-baseline`." (then the table)
    mod = _mosa_check()
    counted = set(_counts(mod))
    recorded = set(json.loads((ROOT / ".dod-mosa-baseline").read_text(encoding="utf-8")))
    missing_count = [name for name in _adr_metrics() if name not in counted]
    missing_record = [name for name in _adr_metrics() if name not in recorded]
    assert not missing_count, f"mosa_check.py stopped counting ADR metrics: {missing_count}"
    assert not missing_record, f".dod-mosa-baseline stopped recording ADR metrics: {missing_record}"


def test_a_count_above_baseline_fails_the_gate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # "A count above baseline fails ..."
    mod = _mosa_check()
    counts = _counts(mod)
    growable = [
        name
        for name in ("lines_api_models_py", "lines_api_schemas_py", "lines_worker_tasks_py")
        if counts[name] > 0
    ]
    assert growable, "no god-file metric carries a count that can rise above a baseline"
    name = growable[0]
    baseline = dict(counts)
    baseline[name] = counts[name] - 1
    baseline_file = tmp_path / ".dod-mosa-baseline"
    baseline_file.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", baseline_file)
    assert mod.main() == 1, "the ratchet passed with a count above baseline"


def test_a_count_below_baseline_lowers_it_automatically(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # "... and a count below it lowers the baseline automatically."
    mod = _mosa_check()
    counts = _counts(mod)
    name = "lines_api_models_py"
    baseline = dict(counts)
    baseline[name] = counts[name] + 3
    baseline_file = tmp_path / ".dod-mosa-baseline"
    baseline_file.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", baseline_file)
    assert mod.main() == 0
    lowered = json.loads(baseline_file.read_text(encoding="utf-8"))
    assert lowered[name] == counts[name], (
        f"debt shrank but the baseline did not follow it down: {lowered[name]} != {counts[name]}"
    )


# --- Decision: supply-chain evidence (ADR 0003 lines 27-47) --------------------------------


def test_supply_chain_job_publishes_sbom_and_audit_reports_as_artifacts():
    # "CI job `supply-chain` produces a CycloneDX SBOM (`sbom.cdx.json`) and pip-audit and
    # npm audit reports as build artifacts."
    job = _job(CI.read_text(encoding="utf-8"), "supply-chain")
    assert "anchore/sbom-action" in job, "supply-chain no longer produces an SBOM"
    assert "format: cyclonedx-json" in job, "the SBOM is no longer CycloneDX"
    assert "output-file: sbom.cdx.json" in job, "the SBOM artifact is no longer sbom.cdx.json"
    assert "if: always()" in job, "the supply-chain reports are not uploaded on failure"
    for report in ("sbom.cdx.json", "pip-audit.json", "npm-audit.json"):
        assert report in job, f"{report} is no longer uploaded as a build artifact"


def test_the_audits_are_blocking_and_always_run():
    # "The audits are **blocking**. Any known advisory fails the build. ... Both always run
    # and the reports always upload, so a failing build carries its evidence."
    job = _job(CI.read_text(encoding="utf-8"), "supply-chain")
    assert "continue-on-error" not in job, "the supply-chain audits were made non-blocking"
    assert job.count("if: success() || failure()") >= 2, (
        "the npm audit steps no longer run after a pip-audit failure, so a failing build "
        "would not carry its evidence"
    )
    assert "exit \"$rc\"" in job or "exit \"$code\"" in job or "exit 1" in job, (
        "the npm audit step no longer fails the job on an advisory"
    )


def test_pip_audit_covers_every_python_service_and_npm_audit_the_web_production_deps():
    # "pip-audit covers every Python service's pins (api, worker, ai-orchestrator,
    # telemetry-pipeline, scenario-engine) and npm audit covers the web app's production
    # dependencies." telemetry-pipeline was removed from the runtime (see the worker
    # return), so the surviving services are pinned here; adding a Python service and
    # leaving it unaudited fails this test via its requirements.txt below.
    job = _job(CI.read_text(encoding="utf-8"), "supply-chain")
    for requirements in (
        "control-plane/api/requirements.txt",
        "control-plane/worker/requirements.txt",
        "ai-orchestrator/requirements.txt",
        "scenario-engine/requirements.txt",
    ):
        assert requirements in job, f"pip-audit no longer covers {requirements}"
    assert "--omit=dev" in job, "npm audit no longer covers only production dependencies"
    for service in ("control-plane/api", "control-plane/worker", "ai-orchestrator", "scenario-engine"):
        reqs = (ROOT / service / "requirements.txt").exists()
        assert reqs, f"pip-audit coverage list is stale: {service}/requirements.txt vanished"


def test_no_advisory_is_ignored_without_an_adr_naming_it():
    # "Do not add an ignore without an ADR that names the advisory, why it does not apply,
    # and when it will be revisited."
    adr_text = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "docs" / "adr").glob("*.md"))
    )
    offenders = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"ignore[-_]vulns?\b", line, re.I):
                continue
            context = "\n".join(lines[max(0, i - 1) : i + 2])
            for advisory in re.findall(r"\b(?:CVE|GHSA|PYSEC)-[0-9A-Za-z-]+\b", context):
                if advisory not in adr_text:
                    offenders.append(
                        f"{workflow.relative_to(ROOT)}:{i + 1} ignores {advisory} "
                        "with no ADR naming it"
                    )
    assert not offenders


def test_third_party_container_images_are_pinned_and_never_latest():
    # "Third-party container images are pinned to a tag. Never use `:latest`."
    dockerfiles = [
        p
        for p in ROOT.rglob("Dockerfile*")
        if p.is_file()
        and not {"node_modules", "dist", ".venv"} & set(p.parts)
    ]
    yamls = [
        p
        for d in IMAGE_SCAN_DIRS
        for p in (ROOT / d).rglob("*")
        if p.is_file() and p.suffix in (".yml", ".yaml") and "node_modules" not in p.parts
    ]
    assert dockerfiles and yamls, "image pinning scan found nothing to check"
    violations = []
    for path in dockerfiles + yamls:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"^\s*FROM\s+(.+)$", line)
            if m:
                ref_m = re.match(r"^(?:--platform=\S+\s+)?(\S+)", m.group(1).strip(), re.I)
                ref, kind = (ref_m.group(1) if ref_m else ""), "FROM"
            else:
                m = re.match(r"^\s*image:\s*(.+?)\s*(?:#.*)?$", line)
                if not m:
                    continue
                ref, kind = m.group(1).strip().strip("\"'"), "image:"
            # Templated refs are pinned by their variable's default or the release
            # pipeline; scratch is not an image; a digest is a stronger pin than a tag.
            if not ref or ref.startswith("${") or ref.startswith("{{") or ref == "scratch":
                continue
            if "@sha256:" in ref:
                continue
            tail = ref.rsplit("/", 1)[-1]
            where = f"{path.relative_to(ROOT)}:{lineno} ({kind} {ref})"
            if ":" not in tail:
                violations.append(f"{where} has no tag (implicit :latest)")
            elif tail.split(":", 1)[1].lower() == "latest":
                violations.append(f"{where} uses :latest")
    assert not violations, "unpinned third-party container images:\n" + "\n".join(violations)


# --- Decision: licensing and data rights (ADR 0003 lines 49-58) ----------------------------


def test_the_license_is_proprietary_all_rights_reserved():
    # "The repository is **proprietary, all rights reserved** (`LICENSE`). This can be
    # relaxed later by the copyright holder."
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    for marker in _PROPRIETARY_MARKERS:
        assert marker in text, f"LICENSE no longer states {marker!r}"
    opensource = [marker for marker in _OPEN_SOURCE_MARKERS if marker in text]
    assert not opensource, f"LICENSE now carries an open-source licence text: {opensource}"


def test_the_license_leaves_government_data_rights_to_the_contract():
    # "Government data rights (e.g. DFARS 252.227-7013/-7014 in the US, or the applicable
    # Canadian contract terms for CAF/DND) are set **by contract**, not by this file.
    # Deliverables must carry the restrictive markings the contract allows; unmarked
    # deliverables can default to unlimited rights."
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "applicable contract" in text, (
        "LICENSE no longer leaves government data rights to the contract"
    )
