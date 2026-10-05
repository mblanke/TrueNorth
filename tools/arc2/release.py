"""Build a release candidate from an accepted ARC² run.

    python -m arc2.release build <run> [--out <file.tar.gz>]

A release is what the platform stores, accepts and publishes; the run directory is not. The
tarball has three parts that never mix:

  learner/     what a student may receive: module pages and the cmi5 package
  platform/    what the platform needs to deliver it: manifest, outline, course YAML (it holds
               the quiz keys, so it never goes to a student), module configs, lab profile,
               validators and engine scenario
  instructor/  the instructor pack: rubric, deliverable, variant, xAPI map, instructor/ notes

``release.json`` at the root lists every file with its sha256, a digest per part and a release
digest over the three parts and the rest of release.json (identity, activities, open actions). The API recomputes all of them on upload and refuses a mismatch, so
the digest it records is the content it stores. Only an accepted, packaged run with a
catalogue identity can be released; open human actions travel with it and must be
acknowledged by whoever accepts the release.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

from arc2 import check

RELEASE_SCHEMA = "arc2/release/0.1"
# Ordered: the first matching rule decides a file's part; unmatched files are not released.
PART_RULES: tuple[tuple[str, str], ...] = (
    ("02-content/mod_*/content/*", "learner"),
    ("02-content/mod_*/content/evidence/*", "learner"),  # supplied material for practical work
    ("07-bundle/cmi5/**", "learner"),
    ("manifest.json", "platform"),
    ("01-blueprint/outline.yaml", "platform"),
    ("02-content/*.yaml", "platform"),
    ("02-content/mod_*/course-config.json", "platform"),
    ("03-range/lab_profile.yaml", "platform"),
    ("03-range/timeline.yaml", "platform"),
    ("03-range/range.tf", "platform"),
    ("05-sensor/**", "platform"),
    ("04-artifacts/**", "instructor"),
)
PARTS = ("learner", "platform", "instructor")
# A learner file may never look like instructor material, whatever rule admitted it.
LEARNER_FORBIDDEN = ("instructor", "answer_key", "answer-key", "solution", "rubric")
SKIP_NAMES = frozenset({"fragment.json", ".gitkeep", "PROMOTE.md"})


class ReleaseError(Exception):
    """The run cannot be released as it stands."""


def part_for(rel: str) -> str | None:
    if Path(rel).name in SKIP_NAMES:
        return None
    for pattern, part in PART_RULES:
        if _match(rel, pattern):
            return part
    return None


def _match(rel: str, pattern: str) -> bool:
    """`**` spans directories, `*` stays within one path segment."""
    regex = "".join(
        ".*" if tok == "**" else "[^/]*" if tok == "*" else re.escape(tok)
        for tok in re.split(r"(\*\*|\*)", pattern)
        if tok
    )
    return re.fullmatch(regex, rel) is not None


def learner_leaks(paths: list[str]) -> list[str]:
    return [p for p in paths if any(word in p.lower() for word in LEARNER_FORBIDDEN)]


def digest_files(files: list[dict[str, str]]) -> str:
    lines = "".join(f"{f['path']} {f['sha256']}\n" for f in sorted(files, key=lambda f: f["path"]))
    return hashlib.sha256(lines.encode()).hexdigest()


def meta_digest(meta: dict[str, Any]) -> str:
    """Everything release.json says apart from the file lists and the digest itself: the
    identity, activities and open actions are part of what a release digest names."""
    rest = {k: v for k, v in meta.items() if k not in ("parts", "release_digest")}
    return hashlib.sha256(json.dumps(rest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def release_digest(meta: dict[str, Any]) -> str:
    parts = meta["parts"]
    lines = "".join(f"{name} {parts[name]['digest']}\n" for name in PARTS) + f"meta {meta_digest(meta)}\n"
    return hashlib.sha256(lines.encode()).hexdigest()


def readiness(run: Path, manifest: dict[str, Any]) -> list[str]:
    """Why this run cannot be released; empty when it can."""
    problems = []
    if not (manifest.get("course") or {}).get("catalogue_code"):
        problems.append("course.catalogue_code is not set: a release must name the catalogue course it delivers")
    if (manifest.get("qa") or {}).get("result") != "pass":
        problems.append(f"QA is {(manifest.get('qa') or {}).get('result', 'not_run')}, not pass")
    for which in ("outline", "preview"):
        if manifest["gates"][which]["state"] != "accepted":
            problems.append(f"the {which} gate is {manifest['gates'][which]['state']}, not accepted")
    if manifest["gates"]["outline"]["state"] == "accepted" and (
        check.outline_digest(run, manifest) != manifest["gates"]["outline"]["accepted_sha256"]
        or check.activity_changes(run, manifest, "outline")
    ):
        problems.append("the outline changed after it was accepted")
    if manifest["gates"]["preview"]["state"] == "accepted" and (
        check.preview_digest(run, manifest) != manifest["gates"]["preview"]["accepted_sha256"]
        or check.activity_changes(run, manifest, "preview")
    ):
        problems.append("previewed files changed after they were accepted")
    if manifest["stages"]["package-builder"]["state"] != "done":
        problems.append("package-builder has not run")
    return problems


def collect(run: Path) -> dict[str, list[dict[str, str]]]:
    parts: dict[str, list[dict[str, str]]] = {name: [] for name in PARTS}
    for p in sorted(run.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(run).as_posix()
        part = part_for(rel)
        if part:
            parts[part].append({"path": rel, "sha256": check.sha256_file(p)})
    return parts


def build(run: Path, out: Path | None = None) -> tuple[Path, dict[str, Any]]:
    run = run.resolve()
    manifest = check.load_manifest(run)
    problems = readiness(run, manifest)
    if problems:
        raise ReleaseError("; ".join(problems))
    parts = collect(run)
    leaks = learner_leaks([f["path"] for f in parts["learner"]])
    if leaks:
        raise ReleaseError(f"instructor-looking files in the learner part: {', '.join(leaks)}")
    if not parts["learner"]:
        raise ReleaseError("the learner part is empty")
    course = manifest["course"]
    meta = {
        "schema": RELEASE_SCHEMA,
        "catalogue_code": course["catalogue_code"],
        "arc2_code": course["code"],
        "title": course["title"],
        "slug": manifest["slug"],
        "run_id": manifest["run_id"],
        "manifest_schema": manifest["schema_version"],
        "course_yaml": manifest["content"]["course_yaml"],
        "activities": check.run_activities(run, manifest),
        "lab_profile": (manifest.get("range") or {}).get("lab_profile"),
        "gates": {w: manifest["gates"][w]["accepted_sha256"] for w in ("outline", "preview")},
        "open_human_actions": [
            {"id": a["id"], "category": a["category"], "text": a["text"]}
            for a in manifest["human_actions"]
            if a["status"] == "open" and a["blocks_promotion"]
        ],
        "parts": {name: {"digest": digest_files(files), "files": files} for name, files in parts.items()},
    }
    meta["release_digest"] = release_digest(meta)
    out = out or run.parent / f"{manifest['slug']}-{meta['release_digest'][:12]}.tar.gz"
    out.write_bytes(_tarball(run, meta))
    return out, meta


def _tarball(run: Path, meta: dict[str, Any]) -> bytes:
    """Deterministic: sorted members, zeroed owners and times, so the same run gives the
    same bytes and the release digest is the only identity that matters."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

        add("release.json", json.dumps(meta, indent=2, sort_keys=True).encode())
        for name in PARTS:
            for f in meta["parts"][name]["files"]:
                add(f"{name}/{f['path']}", (run / f["path"]).read_bytes())
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="arc2.release", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("build", help="build a release candidate tarball from an accepted run")
    s.add_argument("run", type=Path)
    s.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    try:
        out, meta = build(args.run, args.out)
    except (ReleaseError, check.ContractError) as exc:
        print(f"arc2.release: {exc}", file=sys.stderr)
        return 1
    counts = ", ".join(f"{n} {len(meta['parts'][n]['files'])}" for n in PARTS)
    print(f"{out}  release {meta['release_digest'][:12]}  {meta['catalogue_code']}  ({counts})")
    for a in meta["open_human_actions"]:
        print(f"  open action [{a['category']}] {a['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
