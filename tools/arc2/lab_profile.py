"""Lab profiles: the small individual range a range activity needs.

The range-engineer writes ``03-range/lab_profile.yaml``; ``arc2.check`` validates it here, and
the platform's lab-session service validates the same file with the same function before it
provisions anything, so a profile that passed QA is the profile that runs.

Rules beyond the JSON schema (``lab_profile.schema.json``):
  lab.schema              the document against the schema
  lab.modules             every range module is covered, and only range modules are named
  lab.references          health checks, access, evidence and node networks name things that exist
  lab.size                node totals fit the declared limits; more than three VMs needs a justification
  lab.catalogue           every image is an enabled catalogue id (ARC cannot invent a template)
  lab.egress              an allowlist names at least one destination
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMA_PATH = Path(__file__).with_name("lab_profile.schema.json")
SCHEMA_VERSION = "arc2/lab-profile/0.1"
# The golden-image catalogue the platform imports (app/golden_images.parse_catalogue), then
# the older content-pack copy for repos that only carry that one.
CATALOGUES = (
    Path("content/catalogue/vm_iso_catalogue.csv"),
    Path("truenorth-content-pack/truenorth-content/vm_catalogue.csv"),
)
DEFAULT_MAX_VMS = 3


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def catalogue_ids(repo_root: Path) -> tuple[set[str], set[str]] | None:
    """(enabled ids, disabled ids) from the first catalogue present, or None if none is."""
    for rel in CATALOGUES:
        path = repo_root / rel
        if not path.is_file():
            continue
        enabled: set[str] = set()
        disabled: set[str] = set()
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cid = (row.get("template_id") or "").strip()
                if not cid:
                    continue
                on = (row.get("enabled") or "").strip().lower() in {"yes", "true", "1", "y"}
                (enabled if on else disabled).add(cid)
        return enabled, disabled
    return None


def findings(
    profile: Any,
    *,
    range_modules: set[str],
    catalogue: tuple[set[str], set[str]] | None,
) -> list[tuple[str, str]]:
    """(check, message) pairs; empty means the profile can be provisioned as written.
    ``catalogue=None`` skips the image rule (the caller reports that it could not run)."""
    errors = sorted(jsonschema.Draft7Validator(load_schema()).iter_errors(profile), key=lambda e: list(e.path))
    if errors:
        return [("lab.schema", f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}") for e in errors]
    out: list[tuple[str, str]] = []
    named = set(profile["module_ids"])
    for mid in sorted(range_modules - named):
        out.append(("lab.modules", f"range module {mid} has no lab in this profile"))
    for mid in sorted(named - range_modules):
        out.append(("lab.modules", f"{mid} is not a range module; theory and practical modules get no VM"))

    nodes = {n["name"]: n for n in profile["nodes"]}
    networks = {n["name"] for n in profile["networks"]}
    if len(nodes) != len(profile["nodes"]):
        out.append(("lab.references", "node names are not unique"))
    for n in profile["nodes"]:
        for net in n["networks"]:
            if net not in networks:
                out.append(("lab.references", f"node {n['name']} joins undeclared network {net}"))
    for what in ("health_checks", "access", "evidence_checks"):
        for item in profile[what]:
            if item["node"] not in nodes:
                out.append(("lab.references", f"{what} names unknown node {item['node']}"))
    for hc in profile["health_checks"]:
        if hc["kind"] in ("tcp", "http", "ssh") and "port" not in hc:
            out.append(("lab.references", f"{hc['kind']} health check on {hc['node']} needs a port"))
    unchecked = sorted(set(nodes) - {hc["node"] for hc in profile["health_checks"]})
    if unchecked:
        out.append(("lab.references", f"nodes with no health check cannot be called ready: {', '.join(unchecked)}"))

    limits = profile["limits"]
    count = len(profile["nodes"])
    totals = {
        "vcpu_total": sum(n["vcpu"] for n in profile["nodes"]),
        "ram_mb_total": sum(n["ram_mb"] for n in profile["nodes"]),
        "disk_gb_total": sum(n["disk_gb"] for n in profile["nodes"]),
    }
    if count > limits["max_vms"]:
        out.append(("lab.size", f"{count} nodes exceed max_vms {limits['max_vms']}"))
    for key, total in totals.items():
        if total > limits[key]:
            out.append(("lab.size", f"nodes need {key} {total}, above the limit {limits[key]}"))
    if count > DEFAULT_MAX_VMS and not profile.get("justification"):
        out.append(
            ("lab.size", f"{count} VMs needs an objective-based justification (default is up to {DEFAULT_MAX_VMS})")
        )

    if catalogue is not None:
        enabled, disabled = catalogue
        for n in profile["nodes"]:
            cid = n["catalogue_id"]
            if cid in disabled:
                out.append(("lab.catalogue", f"node {n['name']} uses disabled catalogue image {cid}"))
            elif cid not in enabled:
                out.append(("lab.catalogue", f"node {n['name']} uses {cid}, which is not in the image catalogue"))

    egress = profile["egress"]
    if egress["policy"] == "allowlist" and not egress.get("allow"):
        out.append(("lab.egress", "an allowlist egress policy names no destination"))
    return out
