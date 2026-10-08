"""Read and verify an ARC² release tarball (``tools/arc2/release.py`` writes it).

Nothing in the upload is trusted: every file's sha256, each part's digest and the release
digest (which also covers the rest of release.json) are recomputed here; the three parts
are checked against their own allow-lists (independently of the tool that built them);
release.json is checked against the run's own manifest (QA passed, both gates accepted on
the digests it names, the same open actions, the same module activities, both ways); the
course file is parsed with the importer's parser; and a lab profile is validated with the
same rules ARC² applied. A tarball that fails any of this is refused with the reason,
before anything is stored. The gzip is inflated against a hard ceiling before tar sees it,
so a small upload cannot expand into gigabytes.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import zlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from .. import safe_yaml
from ..course_content_ingest import parse_course_content
from . import lab_profile

RELEASE_SCHEMA = "arc2/release/0.1"
PARTS = ("learner", "platform", "instructor")
MAX_BYTES = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBERS = 5000
MODULE_ID = re.compile(r"^mod_[0-9]{3}$")
ACTIVITY_KINDS = ("theory", "practical", "range")
PATH_RE = re.compile(r"^(?!.*(^|/)\.\.(/|$))[A-Za-z0-9_][A-Za-z0-9_./ -]*$")
ALLOWED: dict[str, tuple[re.Pattern[str], ...]] = {
    "learner": (
        re.compile(r"^02-content/mod_[0-9]{3}/content/.+$"),  # pages, evidence and templates, any depth
        re.compile(r"^07-bundle/cmi5/.+$"),
    ),
    "platform": (
        re.compile(r"^manifest\.json$"),
        re.compile(r"^01-blueprint/outline\.yaml$"),
        re.compile(r"^02-content/[^/]+\.yaml$"),
        re.compile(r"^02-content/mod_[0-9]{3}/course-config\.json$"),
        re.compile(r"^03-range/(lab_profile\.yaml|timeline\.yaml|range\.tf)$"),
        re.compile(r"^05-sensor/.+$"),
    ),
    "instructor": (re.compile(r"^04-artifacts/.+$"),),
}
LEARNER_FORBIDDEN = ("instructor", "answer_key", "answer-key", "solution", "rubric")


class BundleError(ValueError):
    """The upload is not a valid release; the message says why."""


@dataclass
class Bundle:
    meta: dict[str, Any]
    files: dict[str, dict[str, bytes]] = field(default_factory=dict)  # part -> path -> bytes
    sha256: str = ""
    course: dict[str, Any] = field(default_factory=dict)  # parse_course_content output
    activities: dict[str, str] = field(default_factory=dict)
    lab_profile: dict[str, Any] | None = None

    @property
    def open_actions(self) -> list[dict[str, str]]:
        return list(self.meta.get("open_human_actions") or [])


def digest_files(files: dict[str, bytes]) -> str:
    lines = "".join(f"{p} {hashlib.sha256(files[p]).hexdigest()}\n" for p in sorted(files))
    return hashlib.sha256(lines.encode()).hexdigest()


def meta_digest(meta: dict[str, Any]) -> str:
    rest = {k: v for k, v in meta.items() if k not in ("parts", "release_digest")}
    return hashlib.sha256(json.dumps(rest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def release_digest(part_digests: dict[str, str], meta: dict[str, Any]) -> str:
    lines = "".join(f"{n} {part_digests[n]}\n" for n in PARTS) + f"meta {meta_digest(meta)}\n"
    return hashlib.sha256(lines.encode()).hexdigest()


def _gunzip(data: bytes) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = inflater.decompress(data, MAX_EXPANDED + 1)
    except zlib.error as exc:
        raise BundleError(f"not a gzipped tar: {exc}") from exc
    if len(raw) > MAX_EXPANDED or inflater.unconsumed_tail:
        raise BundleError(f"release expands beyond {MAX_EXPANDED} bytes")
    if not inflater.eof:
        raise BundleError("not a gzipped tar: truncated")
    return raw


def _members(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_BYTES:
        raise BundleError(f"release is {len(data)} bytes; the limit is {MAX_BYTES}")
    out: dict[str, bytes] = {}
    total = 0
    raw = _gunzip(data)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for i, info in enumerate(tar):
                if i >= MAX_MEMBERS:
                    raise BundleError(f"more than {MAX_MEMBERS} files")
                if not info.isfile():
                    raise BundleError(f"{info.name}: only regular files are allowed")
                if not PATH_RE.match(info.name) or info.name.startswith("/"):
                    raise BundleError(f"{info.name}: unsafe path")
                total += info.size
                if total > MAX_BYTES * 4:
                    raise BundleError("release expands beyond the size limit")
                fh = tar.extractfile(info)
                if fh is None:
                    raise BundleError(f"{info.name}: unreadable")
                if info.name in out:
                    raise BundleError(f"{info.name}: duplicated")
                out[info.name] = fh.read()
    except (tarfile.TarError, OSError, EOFError, gzip.BadGzipFile) as exc:
        raise BundleError(f"not a gzipped tar: {exc}") from exc
    return out


def parse(data: bytes) -> Bundle:
    members = _members(data)
    if "release.json" not in members:
        raise BundleError("release.json is missing")
    try:
        meta = json.loads(members.pop("release.json"))
    except ValueError as exc:
        raise BundleError(f"release.json is not JSON: {exc}") from exc
    if not isinstance(meta, dict) or meta.get("schema") != RELEASE_SCHEMA:
        raise BundleError(f"release.json schema must be {RELEASE_SCHEMA!r}")
    for key in ("catalogue_code", "arc2_code", "title", "slug", "run_id", "course_yaml", "activities", "parts"):
        if not meta.get(key):
            raise BundleError(f"release.json has no {key}")

    bundle = Bundle(meta=meta, sha256=hashlib.sha256(data).hexdigest())
    for name in PARTS:
        bundle.files[name] = {}
    for member, content in members.items():
        part, _, rel = member.partition("/")
        if part not in PARTS or not rel:
            raise BundleError(f"{member}: outside learner/, platform/ and instructor/")
        if not any(rx.match(rel) for rx in ALLOWED[part]):
            raise BundleError(f"{member}: not allowed in the {part} part")
        if part == "learner" and any(w in rel.lower() for w in LEARNER_FORBIDDEN):
            raise BundleError(f"{member}: instructor material in the learner part")
        bundle.files[part][rel] = content

    digests = {}
    for name in PARTS:
        declared = (meta["parts"].get(name) or {}) if isinstance(meta["parts"], dict) else {}
        listed = {f["path"]: f["sha256"] for f in declared.get("files") or []}
        actual = {p: hashlib.sha256(b).hexdigest() for p, b in bundle.files[name].items()}
        if listed != actual:
            missing = sorted(set(listed) - set(actual))
            extra = sorted(set(actual) - set(listed))
            changed = sorted(p for p in set(listed) & set(actual) if listed[p] != actual[p])
            raise BundleError(
                f"{name} files do not match release.json (missing {missing}, extra {extra}, changed {changed})"
            )
        digests[name] = digest_files(bundle.files[name])
        if declared.get("digest") != digests[name]:
            raise BundleError(f"{name} digest does not match its files")
    if not bundle.files["learner"]:
        raise BundleError("the learner part is empty")
    if meta.get("release_digest") != release_digest(digests, meta):
        raise BundleError("release_digest does not match the parts and release.json")
    manifest = _manifest(bundle)
    _check_manifest(bundle, manifest)

    course_rel = meta["course_yaml"]
    if course_rel not in bundle.files["platform"]:
        raise BundleError(f"platform/{course_rel} (the course file) is missing")
    try:
        bundle.course = parse_course_content(bundle.files["platform"][course_rel].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BundleError(f"{course_rel}: {exc}") from exc
    bundle.activities = _check_activities(bundle, manifest)
    bundle.lab_profile = _check_lab_profile(bundle)
    return bundle


def _manifest(bundle: Bundle) -> dict[str, Any]:
    raw = bundle.files["platform"].get("manifest.json")
    if raw is None:
        raise BundleError("platform/manifest.json is missing")
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise BundleError(f"manifest.json: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleError("manifest.json is not an object")
    return manifest


def _check_manifest(bundle: Bundle, manifest: dict[str, Any]) -> None:
    """release.json must say what the run itself recorded: a release cannot claim QA, gates,
    identity or a shorter list of open actions the run does not have."""
    meta = bundle.meta
    course = manifest.get("course") or {}
    if course.get("catalogue_code") != meta["catalogue_code"] or course.get("code") != meta["arc2_code"]:
        raise BundleError("release.json names a different course than the run's manifest")
    if (manifest.get("qa") or {}).get("result") != "pass":
        raise BundleError("the run's QA did not pass")
    gates = manifest.get("gates") or {}
    for which in ("outline", "preview"):
        gate = gates.get(which) or {}
        if gate.get("state") != "accepted" or gate.get("accepted_sha256") != (meta.get("gates") or {}).get(which):
            raise BundleError(f"the run's {which} gate is not accepted on the digest release.json names")
    open_ids = {
        a.get("id")
        for a in manifest.get("human_actions") or []
        if a.get("status") == "open" and a.get("blocks_promotion")
    }
    if open_ids != {a.get("id") for a in meta.get("open_human_actions") or []}:
        raise BundleError("release.json's open actions differ from the run's")


def _check_activities(bundle: Bundle, manifest: dict[str, Any]) -> dict[str, str]:
    """Activities agree three ways: release.json, the manifest's modules and the course
    file, with every module accounted for in each."""
    acts = bundle.meta["activities"]
    if not isinstance(acts, dict) or any(v not in ACTIVITY_KINDS for v in acts.values()):
        raise BundleError("release.json activities must map module ids to theory, practical or range")
    legacy = manifest.get("schema_version") == "arc2/manifest/0.1"
    modules = (manifest.get("content") or {}).get("modules") or []
    declared: dict[str, str] = {}
    ordinals: dict[str, int] = {}
    for m in modules:
        mid = str(m.get("id", ""))
        if not MODULE_ID.match(mid):
            raise BundleError(f"module id {mid!r} is not mod_NNN")
        kind = (m.get("activity") or {}).get("kind") or ("range" if legacy else None)
        if kind not in ACTIVITY_KINDS:
            raise BundleError(f"{mid} declares no activity in the run's manifest")
        declared[mid] = kind
        ordinals[mid] = int(m.get("ordinal", -1))
    if declared != acts:
        raise BundleError("release.json activities differ from the run's modules")
    by_ordinal = {m["ordinal"]: m for m in bundle.course["modules"]}
    if set(by_ordinal) != set(ordinals.values()):
        raise BundleError("the course file's modules differ from the run's modules")
    for mid, kind in acts.items():
        if by_ordinal[ordinals[mid]]["activity"] != kind:
            raise BundleError(
                f"{mid} is {kind} in the run but {by_ordinal[ordinals[mid]]['activity']} in the course file"
            )
    return dict(acts)


def _check_lab_profile(bundle: Bundle) -> dict[str, Any] | None:
    ranged = {mid for mid, kind in bundle.activities.items() if kind == "range"}
    rel = bundle.meta.get("lab_profile")
    if not ranged:
        if rel:
            raise BundleError("a release with no range activity carries a lab profile")
        return None
    if not rel or rel not in bundle.files["platform"]:
        raise BundleError(f"range activities {sorted(ranged)} need a lab profile in the platform part")
    try:
        doc = safe_yaml.load(bundle.files["platform"][rel])
    except yaml.YAMLError as exc:
        raise BundleError(f"{rel}: {exc}") from exc
    # The image catalogue is checked against the tenant's golden images when a lab is
    # provisioned; here the profile must be structurally provisionable.
    problems = lab_profile.findings(doc, range_modules=ranged, catalogue=None)
    if problems:
        raise BundleError(f"{rel}: " + "; ".join(f"{c}: {m}" for c, m in problems))
    return doc


def part_tarball(bundle: Bundle, part: str) -> bytes:
    """One part as its own deterministic tar.gz (what a download of that part returns)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for rel in sorted(bundle.files[part]):
            data = bundle.files[part][rel]
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
