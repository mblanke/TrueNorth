"""Build ARC² release tarballs for API tests with the real tooling (tools/arc2/release.py), so
the API's verifier is tested against the format the tool actually writes.

The course is C304 from the repo catalogue (its draft is iot-security-foundations.yaml). By
default every module is a theory activity; ``range_ordinals`` makes those modules range
activities with a lab brief and adds a one-VM lab profile.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import yaml
from arc2 import release as arc_release

ROOT = pathlib.Path(__file__).resolve().parents[2]
COURSE_FILE = ROOT / "content/courses/iot-security-foundations.yaml"
CATALOGUE = ROOT / "content/catalogue/cyber_operator_programme.csv"
CROSSWALK = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"

LAB_PROFILE = {
    "schema_version": "arc2/lab-profile/0.1",
    "id": "iot-gateway",
    "version": 1,
    "objective": "Harden one IoT gateway host",
    "module_ids": [],
    "nodes": [
        {"name": "gateway", "catalogue_id": "ubuntu-lts", "vcpu": 2, "ram_mb": 2048, "disk_gb": 20, "networks": ["lab"]}
    ],
    "networks": [{"name": "lab", "cidr": "10.20.0.0/24"}],
    "health_checks": [{"node": "gateway", "kind": "ssh", "port": 22, "timeout_s": 300}],
    "access": [{"node": "gateway", "kind": "console"}],
    "evidence_checks": [{"id": "sshd-config", "node": "gateway", "description": "sshd_config hardened"}],
    "limits": {"max_vms": 1, "vcpu_total": 2, "ram_mb_total": 2048, "disk_gb_total": 20},
    "reset": {"mode": "snapshot"},
    "lifetime": {"idle_minutes": 60, "max_minutes": 240},
    "egress": {"policy": "none"},
}


def course_yaml(
    range_ordinals: frozenset[int] = frozenset(), *, title_suffix: str = "", drop_ordinals: frozenset[int] = frozenset()
) -> str:
    doc = yaml.safe_load(COURSE_FILE.read_text(encoding="utf-8"))
    doc["course_code"] = "ARC2-IOT"
    doc["modules"] = [m for m in doc["modules"] if m["ordinal"] not in drop_ordinals]
    for m in doc["modules"]:
        if m["ordinal"] in range_ordinals:
            m["activity"] = "range"
        else:
            m["activity"] = "theory"
            m.pop("lab", None)
        m["title"] = m["title"] + title_suffix
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


GATES = {"outline": "0" * 64, "preview": "1" * 64}


def manifest(
    mods: list[dict[str, Any]], activities: dict[str, str], open_actions: list[dict[str, str]], catalogue_code: str
) -> dict[str, Any]:
    """The parts of a run's manifest the API cross-checks release.json against."""
    for m in mods:
        kind = activities[m["id"]]
        m["activity"] = {"kind": kind} if kind == "range" else {"kind": kind, "no_range_reason": "no live systems"}
    return {
        "schema_version": "arc2/manifest/0.2",
        "course": {"code": "ARC2-IOT", "catalogue_code": catalogue_code},
        "content": {"modules": mods},
        "qa": {"result": "pass"},
        "gates": {w: {"state": "accepted", "accepted_sha256": d} for w, d in GATES.items()},
        "human_actions": [dict(a, stage="orchestrator", status="open", blocks_promotion=True) for a in open_actions],
    }


def write_run(
    run: pathlib.Path,
    *,
    range_ordinals: frozenset[int] = frozenset(),
    title_suffix: str = "",
    lab_profile: dict[str, Any] | None | bool = True,
    open_actions: list[dict[str, str]] | None = None,
    catalogue_code: str = "C304",
    drop_ordinals: frozenset[int] = frozenset(),
    evidence: dict[str, str] | None = None,
) -> dict[str, str]:
    """Lay down the files a released run carries; returns module id → activity."""
    yml = course_yaml(range_ordinals, title_suffix=title_suffix, drop_ordinals=drop_ordinals)
    doc = yaml.safe_load(yml)
    mods = [{"id": f"mod_{m['ordinal']:03d}", "ordinal": m["ordinal"]} for m in doc["modules"]]
    activities = {m["id"]: ("range" if m["ordinal"] in range_ordinals else "theory") for m in mods}
    files = {
        "manifest.json": json.dumps(manifest(mods, activities, open_actions or [], catalogue_code)),
        "01-blueprint/outline.yaml": yaml.safe_dump(
            {"modules": [{"id": k, "activity": v} for k, v in activities.items()]}
        ),
        "02-content/arc2-iot.yaml": yml,
        "04-artifacts/rubric.md": "| ID | Criterion |\n",
        "04-artifacts/instructor/answer_key.md": "Q1: B\n",
    }
    for m in mods:
        files[f"02-content/{m['id']}/content/page-01.html"] = f"<h2>{m['id']}</h2><p>Lesson.</p>"
        files[f"02-content/{m['id']}/course-config.json"] = "{}"
    for name, text in (evidence or {}).items():
        files[f"02-content/mod_001/content/evidence/{name}"] = text
    ranged = sorted(k for k, v in activities.items() if v == "range")
    if ranged and lab_profile:
        profile = dict(LAB_PROFILE if lab_profile is True else lab_profile)
        profile["module_ids"] = ranged
        files["03-range/lab_profile.yaml"] = yaml.safe_dump(profile)
    for rel, text in files.items():
        p = run / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return activities


def build(
    tmp_path: pathlib.Path,
    *,
    catalogue_code: str = "C304",
    range_ordinals: frozenset[int] = frozenset(),
    title_suffix: str = "",
    open_actions: list[dict[str, str]] | None = None,
    lab_profile: dict[str, Any] | None | bool = True,
    slug: str = "arc2-iot",
    drop_ordinals: frozenset[int] = frozenset(),
    evidence: dict[str, str] | None = None,
) -> bytes:
    run = tmp_path / slug
    activities = write_run(
        run,
        range_ordinals=range_ordinals,
        title_suffix=title_suffix,
        lab_profile=lab_profile,
        open_actions=open_actions,
        catalogue_code=catalogue_code,
        drop_ordinals=drop_ordinals,
        evidence=evidence,
    )
    parts = arc_release.collect(run)
    meta = {
        "schema": arc_release.RELEASE_SCHEMA,
        "catalogue_code": catalogue_code,
        "arc2_code": "ARC2-IOT",
        "title": "IoT Security Foundations",
        "slug": slug,
        "run_id": "0123456789ab",
        "manifest_schema": "arc2/manifest/0.2",
        "course_yaml": "02-content/arc2-iot.yaml",
        "activities": activities,
        "lab_profile": "03-range/lab_profile.yaml" if (run / "03-range/lab_profile.yaml").exists() else None,
        "gates": dict(GATES),
        "open_human_actions": open_actions or [],
        "parts": {n: {"digest": arc_release.digest_files(f), "files": f} for n, f in parts.items()},
    }
    meta["release_digest"] = arc_release.release_digest(meta)
    return arc_release._tarball(run, meta)
