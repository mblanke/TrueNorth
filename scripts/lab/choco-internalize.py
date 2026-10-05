#!/usr/bin/env python3
"""Internalize Chocolatey packages into the depot, so they install with no internet.

Runs on TN-BUILD01 (internet + the depot), from the TrueNorth checkout, called by the
tn_prefetch role (install/lab/prefetch.yml). Needs Python 3 and PyYAML only.

Many community packages (googlechrome, firefoxesr, vscode, ...) are wrappers whose
chocolateyInstall.ps1 downloads the vendor installer at install time; in a no-egress
range that fails even though the .nupkg is in the depot. For every definition in
content/choco/<name>.yaml this script:

  1. downloads the vendor installer(s) and checks the pinned sha256 (a blank sha256 is
     computed here, reported, and written back into the definition with --write-back);
  2. uploads each installer to the Nexus raw repo: installers/<name>/<version>/<file>;
  3. generates a .nupkg with the SAME id as the community package (callers and the
     software catalogue do not change): a nuspec, tools/chocolateyInstall.ps1 that
     downloads from the depot ($env:TN_DEPOT_URL, else the depot URL baked in at build
     time) with Install-ChocolateyPackage and the checksum, and optionally
     tools/chocolateyUninstall.ps1;
  4. pushes it to chocolatey-hosted, reusing choco_prefetch.py's upload path.

Precedence (why ranges get this package and not the community wrapper):
  * Version: <vendor version>.<revision> (a 4th "fix" segment, Chocolatey's package fix
    notation). A vendor version that already has 4 segments gets its last segment
    multiplied by 100 plus the revision (155.0.8059.26 -> 155.0.8059.2601). Either way
    ours sorts above the community package of the same vendor release, so an unpinned
    `choco install <id>` (and a dependency such as wireshark -> vcredist140) resolves to
    it.
  * The depot's chocolatey-hosted may already hold a community build of the same id
    (earlier prefetch runs uploaded it; the repo is ALLOW_ONCE so it stays). Before
    pushing, this script lists the hosted versions and refuses (the package is reported
    failed) if any of them sorts above ours: bump `version` or `revision`.
  * choco_prefetch.py is told to skip internalized ids (--skip), so no newer community
    wrapper is uploaded to hosted or fetched through the proxy afterwards. Offline, the
    proxy is blocked and the group answers from hosted, which lists hosted first.
  * A newer community version upstream is reported (NEWER-UPSTREAM), not fatal: it can
    never reach an offline range, but it means the definition is due an update.

Credentials: NEXUS_USER / NEXUS_PASSWORD in the environment (the tn-prefetch user, which
may write chocolatey-hosted and installers). Never printed.

  choco-internalize.py --defs content/choco --list            # ids, as JSON (no network)
  choco-internalize.py --defs content/choco --nexus http://10.30.32.10:8081 \
      --workdir /var/tmp/tn-prefetch/internalize --report report.json [--only vscode]
  choco-internalize.py --defs content/choco --pack-only --baked-url http://depot:8081 \
      --workdir out/                                          # nupkgs only, no network
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEFS = ROOT / "content" / "choco"
_PREFETCH = ROOT / "install" / "lab" / "roles" / "tn_prefetch" / "files" / "choco_prefetch.py"

TYPES = ("msi", "exe", "zip")
ARCHES = ("any", "x64")
DEFAULT_EXIT_CODES = [0, 1641, 3010]
DEPOT_PORT = 8081  # Nexus; TN_DEPOT_URL in the platform .env carries no port
RAW_REPO = "installers"
CHUNK = 1 << 20
TIMEOUT = 300

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

TOP_KEYS = {"name", "version", "revision", "title", "summary", "authors", "description", "type",
            "silent_args", "valid_exit_codes", "software_name", "installers", "uninstall", "notes"}
INSTALLER_KEYS = {"url", "sha256", "file", "arch"}
UNINSTALL_KEYS = {"silent_args"}


class DefinitionError(ValueError):
    """A content/choco definition is malformed."""


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #


def validate(doc: object, source: str = "<definition>") -> dict:
    """A normalised copy of one definition, or DefinitionError naming what is wrong."""
    def bad(msg: str) -> DefinitionError:
        return DefinitionError(f"{source}: {msg}")

    if not isinstance(doc, dict):
        raise bad("not a mapping")
    unknown = set(doc) - TOP_KEYS
    if unknown:
        raise bad(f"unknown keys {sorted(unknown)}")
    name = str(doc.get("name") or "")
    if not NAME_RE.match(name):
        raise bad(f"name {name!r} must be a lower-case Chocolatey id")
    version = str(doc.get("version") or "")
    if not VERSION_RE.match(version):
        raise bad(f"version {version!r} must be 1-4 numeric segments (the vendor version)")
    revision = doc.get("revision", 1)
    if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision <= 99:
        raise bad("revision must be an integer 1..99")
    ftype = str(doc.get("type") or "").lower()
    if ftype not in TYPES:
        raise bad(f"type must be one of {TYPES}")
    silent = doc.get("silent_args")
    if ftype != "zip" and not (isinstance(silent, str) and silent.strip()):
        raise bad("silent_args is required for msi/exe")
    codes = doc.get("valid_exit_codes", DEFAULT_EXIT_CODES)
    if not isinstance(codes, list) or not codes or not all(isinstance(c, int) for c in codes):
        raise bad("valid_exit_codes must be a non-empty list of integers")
    software = doc.get("software_name")
    if software is not None and not (isinstance(software, str) and software.strip()):
        raise bad("software_name must be a non-empty string")

    raw_installers = doc.get("installers")
    if not isinstance(raw_installers, list) or not raw_installers:
        raise bad("installers must be a non-empty list")
    installers, files = [], set()
    for i, inst in enumerate(raw_installers):
        where = f"installers[{i}]"
        if not isinstance(inst, dict):
            raise bad(f"{where} is not a mapping")
        unknown = set(inst) - INSTALLER_KEYS
        if unknown:
            raise bad(f"{where}: unknown keys {sorted(unknown)}")
        url = str(inst.get("url") or "")
        if not re.match(r"^https?://", url):
            raise bad(f"{where}: url must be http(s)")
        sha = str(inst.get("sha256") or "").strip().lower()
        if sha and not SHA_RE.match(sha):
            raise bad(f"{where}: sha256 must be 64 hex characters (or blank: computed at build)")
        fname = str(inst.get("file") or "") or Path(urllib.parse.urlparse(url).path).name
        if not FILE_RE.match(fname) or "." not in fname:
            raise bad(f"{where}: give `file:` (a plain file name with an extension) for {url}")
        if fname in files:
            raise bad(f"{where}: duplicate file {fname}")
        files.add(fname)
        arch = str(inst.get("arch") or "any").lower()
        if arch not in ARCHES:
            raise bad(f"{where}: arch must be one of {ARCHES}")
        installers.append({"url": url, "sha256": sha or None, "file": fname, "arch": arch})

    uninstall = doc.get("uninstall")
    if uninstall is not None:
        if ftype == "zip":
            raise bad("uninstall is not used for zip packages (Chocolatey removes the lib folder)")
        if not isinstance(uninstall, dict) or set(uninstall) - UNINSTALL_KEYS:
            raise bad(f"uninstall must be a mapping with only {sorted(UNINSTALL_KEYS)}")
        if not software:
            raise bad("uninstall needs software_name (the Programs and Features display name)")
        if not isinstance(uninstall.get("silent_args"), str) or not uninstall["silent_args"].strip():
            raise bad("uninstall.silent_args is required")
        uninstall = {"silent_args": uninstall["silent_args"].strip()}

    title = str(doc.get("title") or name)
    return {
        "name": name,
        "version": version,
        "revision": revision,
        "title": title,
        "summary": str(doc.get("summary") or f"{title}, internalized for offline TrueNorth ranges"),
        "authors": str(doc.get("authors") or "TrueNorth (internalized)"),
        "description": str(doc.get("description") or "").strip()
        or f"{title} {version}. The installer is served by the TrueNorth depot; nothing is downloaded "
           "from the vendor at install time.",
        "type": ftype,
        "silent_args": (silent or "").strip(),
        "valid_exit_codes": list(codes),
        "software_name": software.strip() if software else None,
        "installers": installers,
        "uninstall": uninstall,
    }


def load_definition(path: Path) -> dict:
    import yaml  # PyYAML: python3-yaml on TN-BUILD01 (a dependency of ansible-core)

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DefinitionError(f"{path}: {exc}") from exc
    defn = validate(doc, str(path))
    if defn["name"] != path.stem:
        raise DefinitionError(f"{path}: name {defn['name']!r} must match the file name")
    return defn


def load_definitions(defs_dir: Path) -> list[dict]:
    paths = sorted(p for p in Path(defs_dir).glob("*.yaml"))
    return [dict(load_definition(p), path=str(p)) for p in paths]


def package_version(version: str, revision: int = 1) -> str:
    """The nupkg version: the vendor version with a TrueNorth fix segment.

    1.140.0 -> 1.140.0.1; 7.6 -> 7.6.0.1; 155.0.8059.26 -> 155.0.8059.2601 (NuGet allows
    four numeric segments, so a 4-segment vendor version folds the revision into the last).
    """
    if not VERSION_RE.match(version) or not 1 <= revision <= 99:
        raise DefinitionError(f"bad version {version!r} / revision {revision!r}")
    parts = [int(p) for p in version.split(".")]
    if len(parts) == 4:
        parts[3] = parts[3] * 100 + revision
    else:
        parts = (parts + [0, 0, 0])[:3] + [revision]
    return ".".join(str(p) for p in parts)


def version_key(version: str) -> tuple:
    """Sortable NuGet version: numeric segments padded to 4; a pre-release sorts below."""
    core, _, pre = str(version).partition("-")
    nums = [int(p) if p.isdigit() else 0 for p in core.split(".")]
    return (tuple((nums + [0, 0, 0, 0])[:4]), 0 if pre else 1)


def raw_path(defn: dict, inst: dict) -> str:
    """Path inside the raw repo: <name>/<vendor version>/<file>."""
    return f"{defn['name']}/{defn['version']}/{inst['file']}"


# --------------------------------------------------------------------------- #
# Package generation (no network, no Windows)
# --------------------------------------------------------------------------- #


def _ps(value: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def render_install_ps1(defn: dict, baked_url: str) -> str:
    """tools/chocolateyInstall.ps1: every installer from the depot, checksum-verified."""
    baked = baked_url.rstrip("/") + f"/repository/{RAW_REPO}"
    codes = ", ".join(str(c) for c in defn["valid_exit_codes"])
    out = [
        f"# Generated by scripts/lab/choco-internalize.py from content/choco/{defn['name']}.yaml.",
        "# Do not edit: change the definition and rebuild. The installer is served by the",
        "# TrueNorth depot; nothing is fetched from the vendor at install time.",
        "$ErrorActionPreference = 'Stop'",
        "$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition",
        "",
        "# Depot: $env:TN_DEPOT_URL (as in the platform .env, e.g. http://10.30.32.10; Nexus",
        f"# port {DEPOT_PORT} unless one is given), else the depot this package was built against.",
        "$depot = \"$env:TN_DEPOT_URL\".Trim().TrimEnd('/')",
        "if ($depot) {",
        f"  if ($depot -notmatch ':\\d+$') {{ $depot = \"${{depot}}:{DEPOT_PORT}\" }}",
        f"  $installers = \"$depot/repository/{RAW_REPO}\"",
        "} else {",
        f"  $installers = {_ps(baked)}",
        "}",
        f"Write-Host \"{defn['name']} {defn['version']}: installers from $installers\"",
    ]
    for inst in defn["installers"]:
        if not inst["sha256"]:
            raise DefinitionError(f"{defn['name']}: {inst['file']} has no sha256 yet (download it first)")
        url = f"\"$installers/{urllib.parse.quote(raw_path(defn, inst))}\""
        # x64-only: url64bit, so Chocolatey refuses it on 32-bit Windows instead of guessing.
        x64 = inst["arch"] == "x64"
        k_url, k_sum, k_type = (("url64bit", "checksum64", "checksumType64") if x64
                                else ("url", "checksum", "checksumType"))
        out += ["", "$packageArgs = @{", "  packageName    = $env:ChocolateyPackageName"]
        if defn["type"] == "zip":
            out += ["  unzipLocation  = $toolsDir"]
        else:
            out += [f"  fileType       = {_ps(defn['type'])}",
                    f"  silentArgs     = {_ps(defn['silent_args'])}",
                    f"  validExitCodes = @({codes})"]
            if defn["software_name"]:
                out += [f"  softwareName   = {_ps(defn['software_name'] + '*')}"]
        out += [f"  {k_url:<15}= {url}",
                f"  {k_sum:<15}= {_ps(inst['sha256'])}",
                f"  {k_type:<15}= 'sha256'",
                "}"]
        out += ["Install-ChocolateyZipPackage @packageArgs" if defn["type"] == "zip"
                else "Install-ChocolateyPackage @packageArgs"]
    return "\n".join(out) + "\n"


def render_uninstall_ps1(defn: dict) -> str | None:
    """tools/chocolateyUninstall.ps1, when the definition asks for one."""
    if not defn.get("uninstall"):
        return None
    codes = ", ".join(str(c) for c in defn["valid_exit_codes"])
    lines = [
        f"# Generated by scripts/lab/choco-internalize.py from content/choco/{defn['name']}.yaml.",
        "$ErrorActionPreference = 'Stop'",
        f"$silent = {_ps(defn['uninstall']['silent_args'])}",
        f"[array]$keys = Get-UninstallRegistryKey -SoftwareName {_ps(defn['software_name'] + '*')}",
        "if ($keys.Count -eq 0) {",
        f"  Write-Warning {_ps(defn['software_name'] + ' is not installed (removed by other means?)')}",
        "  return",
        "}",
        "foreach ($key in $keys) {",
    ]
    if defn["type"] == "msi":
        lines += ["  Uninstall-ChocolateyPackage -PackageName $env:ChocolateyPackageName -FileType 'msi' `",
                  "    -SilentArgs \"$($key.PSChildName) $silent\" -File '' `",
                  f"    -ValidExitCodes @({codes})"]
    else:
        lines += ["  $file = $key.UninstallString -replace '^\"?([^\"]+?\\.exe)\"?.*$', '$1'",
                  "  Uninstall-ChocolateyPackage -PackageName $env:ChocolateyPackageName -FileType 'exe' `",
                  "    -SilentArgs $silent -File $file `",
                  f"    -ValidExitCodes @({codes})"]
    lines += ["}"]
    return "\n".join(lines) + "\n"


def render_nuspec(defn: dict, pkg_version: str) -> str:
    files = ", ".join(i["file"] for i in defn["installers"])
    notes = (f"Vendor version {defn['version']}, TrueNorth revision {defn['revision']}. "
             f"Installers ({files}) from the depot raw repository "
             f"{RAW_REPO}/{defn['name']}/{defn['version']}/.")
    meta = [
        ("id", defn["name"]),
        ("version", pkg_version),
        ("title", defn["title"]),
        ("authors", defn["authors"]),
        ("owners", "TrueNorth"),
        ("requireLicenseAcceptance", "false"),
        ("summary", defn["summary"]),
        ("description", defn["description"]),
        ("releaseNotes", notes),
        ("tags", f"truenorth offline internalized {defn['name']}"),
    ]
    body = "\n".join(f"    <{k}>{escape(v)}</{k}>" for k, v in meta)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://schemas.microsoft.com/packaging/2015/06/nuspec.xsd">\n'
            f"  <metadata>\n{body}\n  </metadata>\n</package>\n")


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
    '<Default Extension="nuspec" ContentType="application/octet" />'
    '<Default Extension="ps1" ContentType="application/octet" />'
    '<Default Extension="psmdcp" ContentType="application/vnd.openxmlformats-package.core-properties+xml" />'
    "</Types>"
)


def build_nupkg(defn: dict, baked_url: str) -> tuple[str, bytes]:
    """(file name, bytes) of the package. Deterministic for the same definition."""
    import io

    ver = package_version(defn["version"], defn["revision"])
    name = defn["name"]
    nuspec = render_nuspec(defn, ver)
    core_id = hashlib.sha256(f"{name}/{ver}".encode()).hexdigest()[:32]
    core = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<coreProperties xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties">'
        f"<dc:creator>{escape(defn['authors'])}</dc:creator><dc:description>{escape(defn['description'])}"
        f"</dc:description><dc:identifier>{name}</dc:identifier><version>{ver}</version>"
        f"<keywords>truenorth offline internalized</keywords>"
        "<lastModifiedBy>choco-internalize.py</lastModifiedBy></coreProperties>"
    )
    rels = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Type="http://schemas.microsoft.com/packaging/2010/07/manifest" Target="/{name}.nuspec" '
        'Id="R0" />'
        '<Relationship Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        f'Target="/package/services/metadata/core-properties/{core_id}.psmdcp" Id="R1" />'
        "</Relationships>"
    )
    parts = [
        ("_rels/.rels", rels.encode()),
        (f"{name}.nuspec", nuspec.encode()),
        ("tools/chocolateyInstall.ps1", render_install_ps1(defn, baked_url).encode("utf-8-sig")),
    ]
    uninstall = render_uninstall_ps1(defn)
    if uninstall:
        parts.append(("tools/chocolateyUninstall.ps1", uninstall.encode("utf-8-sig")))
    parts += [
        (f"package/services/metadata/core-properties/{core_id}.psmdcp", core.encode()),
        ("[Content_Types].xml", _CONTENT_TYPES.encode()),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, data in parts:
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return f"{name}.{ver}.nupkg", buf.getvalue()


def write_back_sha256(path: Path, url: str, sha: str) -> bool:
    """Fill the blank ``sha256:`` of the installer whose ``url:`` is ``url``. True if written."""
    lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(r"^\s*-?\s*url:\s*['\"]?" + re.escape(url), ln)), None)
    if start is None:
        return False
    for i in range(start, len(lines)):
        if i > start and re.match(r"^\s*-\s", lines[i]):
            break
        m = re.match(r"^(\s*-?\s*sha256:)\s*(?:\"\"|''|)?\s*(#.*)?$", lines[i].rstrip("\r\n"))
        if m:
            lines[i] = f"{m.group(1)} {sha}\n"
            Path(path).write_text("".join(lines), encoding="utf-8")
            return True
    return False


# --------------------------------------------------------------------------- #
# Network (TN-BUILD01 only)
# --------------------------------------------------------------------------- #


def _prefetch():
    """choco_prefetch.py from the tn_prefetch role: its http() and upload() are reused."""
    spec = importlib.util.spec_from_file_location("choco_prefetch", _PREFETCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _auth_header(auth) -> dict:
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_installer(url: str, dest: Path, expected: str | None) -> tuple[str, bool]:
    """Download ``url`` to ``dest`` (reused if it already matches). (sha256, downloaded?)."""
    if dest.is_file() and expected and sha256_file(dest) == expected:
        return expected, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "TrueNorth-choco-internalize/1"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(part, "wb") as out:  # noqa: S310
        for chunk in iter(lambda: resp.read(CHUNK), b""):
            h.update(chunk)
            out.write(chunk)
    actual = h.hexdigest()
    if expected and actual != expected:
        part.unlink(missing_ok=True)
        raise ValueError(f"sha256 mismatch for {url}: expected {expected}, got {actual} "
                         "(the vendor changed the file: update version and sha256 in the definition)")
    part.replace(dest)
    return actual, True


def raw_has(nexus: str, auth, path: str, sha: str) -> bool:
    q = urllib.parse.urlencode({"repository": RAW_REPO, "sha256": sha})
    req = urllib.request.Request(f"{nexus}/service/rest/v1/search/assets?{q}", headers=_auth_header(auth))
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        items = json.loads(resp.read()).get("items") or []
    return any(str(it.get("path", "")).lstrip("/") == path for it in items)


def raw_upload(nexus: str, auth, path: str, src: Path) -> None:
    size = src.stat().st_size
    url = f"{nexus}/repository/{RAW_REPO}/{urllib.parse.quote(path)}"
    with open(src, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="PUT", headers={
            **_auth_header(auth), "Content-Length": str(size), "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=TIMEOUT):  # noqa: S310
            pass


def hosted_versions(nexus: str, auth, pkg_id: str) -> list[str]:
    """Every version of ``pkg_id`` in chocolatey-hosted (Nexus search API, paginated)."""
    versions, token = [], None
    while True:
        params = {"repository": "chocolatey-hosted", "name": pkg_id}
        if token:
            params["continuationToken"] = token
        req = urllib.request.Request(f"{nexus}/service/rest/v1/search?{urllib.parse.urlencode(params)}",
                                     headers=_auth_header(auth))
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            body = json.loads(resp.read())
        versions += [str(it["version"]) for it in body.get("items") or []
                     if str(it.get("name", "")).lower() == pkg_id.lower() and it.get("version")]
        token = body.get("continuationToken")
        if not token:
            return versions


def precedence_problem(ours: str, hosted: list[str]) -> str | None:
    """Why pushing ``ours`` would not make it the version an unpinned install resolves to."""
    above = sorted((v for v in hosted if v != ours and version_key(v) > version_key(ours)), key=version_key)
    if above:
        return (f"chocolatey-hosted already has {', '.join(above)}, above {ours}: an unpinned install "
                "would get that instead. Raise version (or revision) in the definition")
    return None


def internalize(defn: dict, nexus: str, auth, workdir: Path, upstream: str | None, write_back: bool,
                prefetch=None) -> dict:
    """Steps 1-4 for one definition. Returns its report record (raises on failure)."""
    prefetch = prefetch or _prefetch()
    ver = package_version(defn["version"], defn["revision"])
    rec = {"id": defn["name"], "vendor_version": defn["version"], "version": ver, "installers": []}
    for inst in defn["installers"]:
        dest = workdir / "installers" / raw_path(defn, inst)
        sha, downloaded = fetch_installer(inst["url"], dest, inst["sha256"])
        entry = {"file": inst["file"], "sha256": sha, "downloaded": downloaded}
        if not inst["sha256"]:
            entry["recorded"] = True  # was blank in the definition
            if write_back and defn.get("path"):
                entry["written_back"] = write_back_sha256(Path(defn["path"]), inst["url"], sha)
        inst["sha256"] = sha
        path = raw_path(defn, inst)
        if raw_has(nexus, auth, path, sha):
            entry["raw"] = "present"
        else:
            raw_upload(nexus, auth, path, dest)
            entry["raw"] = "uploaded"
        rec["installers"].append(entry)

    hosted = hosted_versions(nexus, auth, defn["name"])
    if ver in hosted:
        rec["hosted"] = "present"
    else:
        problem = precedence_problem(ver, hosted)
        if problem:
            raise RuntimeError(problem)
        fname, data = build_nupkg(defn, nexus)
        (workdir / "nupkgs").mkdir(parents=True, exist_ok=True)
        (workdir / "nupkgs" / fname).write_bytes(data)
        prefetch.upload(nexus, auth, fname, data)
        rec["hosted"] = "uploaded"
    if upstream:
        try:
            latest = prefetch.latest_version(upstream, defn["name"])
            if version_key(latest) >= version_key(ver):
                rec["newer_upstream"] = latest
        except Exception:  # noqa: BLE001 - advisory only
            pass
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--defs", default=str(DEFAULT_DEFS), help="directory of <name>.yaml definitions")
    ap.add_argument("--list", action="store_true", help="print the internalized ids as JSON and exit")
    ap.add_argument("--only", action="append", default=[], help="limit to these names (repeatable)")
    ap.add_argument("--nexus", help="depot Nexus, e.g. http://10.30.32.10:8081")
    ap.add_argument("--upstream", default="https://community.chocolatey.org/api/v2/",
                    help="community feed, for the NEWER-UPSTREAM advisory ('' to skip)")
    ap.add_argument("--workdir", default="/var/tmp/tn-prefetch/internalize")
    ap.add_argument("--report", help="JSON report path")
    ap.add_argument("--write-back", action="store_true", help="record computed sha256 into blank definitions")
    ap.add_argument("--pack-only", action="store_true", help="build nupkgs into --workdir; no network")
    ap.add_argument("--baked-url", help="depot URL baked into --pack-only packages (default: --nexus)")
    args = ap.parse_args(argv)

    try:
        defs = load_definitions(Path(args.defs))
    except DefinitionError as exc:
        print(f"FATAL {exc}")
        return 2
    if args.list:
        print(json.dumps([d["name"] for d in defs]))
        return 0
    if args.only:
        wanted = {n.lower() for n in args.only}
        defs = [d for d in defs if d["name"] in wanted]
    workdir = Path(args.workdir)

    if args.pack_only:
        baked = args.baked_url or args.nexus
        if not baked:
            sys.exit("--pack-only needs --baked-url (or --nexus)")
        (workdir / "nupkgs").mkdir(parents=True, exist_ok=True)
        for d in defs:
            fname, data = build_nupkg(d, baked)
            (workdir / "nupkgs" / fname).write_bytes(data)
            print(f"packed    {fname}")
        return 0

    if not args.nexus:
        sys.exit("--nexus is required")
    nexus = args.nexus.rstrip("/")
    upstream = args.upstream if not args.upstream or args.upstream.endswith("/") else args.upstream + "/"
    auth = (os.environ.get("NEXUS_USER", ""), os.environ.get("NEXUS_PASSWORD", ""))
    if not all(auth):
        sys.exit("NEXUS_USER / NEXUS_PASSWORD are not set")
    prefetch = _prefetch()
    try:
        prefetch.http(f"{nexus}/service/rest/v1/status")
    except (urllib.error.URLError, OSError) as exc:
        print(f"FATAL depot unreachable at {nexus}: {exc}")
        return 2

    results = []
    for d in defs:
        try:
            rec = internalize(d, nexus, auth, workdir, upstream or None, args.write_back, prefetch)
        except Exception as exc:  # noqa: BLE001 - one package must not stop the rest
            rec = {"id": d["name"], "vendor_version": d["version"], "hosted": "failed",
                   "version": package_version(d["version"], d["revision"]),
                   "error": f"{type(exc).__name__}: {exc}"[:500]}
        results.append(rec)
        extra = ""
        for inst in rec.get("installers", []):
            if inst.get("recorded"):
                extra += f"  RECORDED sha256 {inst['file']}={inst['sha256']}"
        if rec.get("newer_upstream"):
            extra += f"  NEWER-UPSTREAM {rec['newer_upstream']}"
        if "error" in rec:
            extra += f"  {rec['error']}"
        print(f"{rec['hosted']:9} {rec['id']} {rec['version']}{extra}")

    if args.report:
        Path(args.report).write_text(json.dumps(results, indent=2))
    count = {k: sum(r["hosted"] == k for r in results) for k in ("uploaded", "present", "failed")}
    print(f"TNINTERNALIZE uploaded={count['uploaded']} present={count['present']} failed={count['failed']} "
          f"ids={','.join(r['id'] for r in results) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
