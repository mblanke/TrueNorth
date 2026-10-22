#!/usr/bin/env python3
"""Stage the offline Open-VM-Tools package set for the pfSense template build.

The pfSense Packer build (appliances.pkr.hcl) finishes the template inside the installer
(files/pfsense/tn-pfsense-finish.sh) and pkg-adds Open-VM-Tools from its second CD, with no
network. The set is pinned in ``infra/vsphere/packer/files/pfsense/pkgs.lock`` (sha256 and
install order) and the .pkg files are staged, unversioned, into
``infra/vsphere/packer/stage/pfsense/``, which the build puts on the CD.

    python3 scripts/lab/stage_pfsense_pkgs.py stage --depot http://10.30.31.30:8081/repository
    python3 scripts/lab/stage_pfsense_pkgs.py stage --from-dir /srv/media/pfsense-2.7.2
    python3 scripts/lab/stage_pfsense_pkgs.py check            # build-phase.sh runs this
    python3 scripts/lab/stage_pfsense_pkgs.py resolve --out DIR  # once, with internet

``stage`` takes each locked file from the depot's raw ``installers`` repository
(``<depot>/installers/pfsense/2.7.2/<file>``, as tn-build-read: the password is
``TN_DEPOT_BUILD_READ_PASSWORD`` from the environment, else the root-only
``/srv/truenorth/config/depot-build.env``) or a local directory, checks its sha256 and
writes it to the stage directory. Nothing is fetched from the internet.

``resolve`` is the pinning step, run once by a person on a host with internet access: it
reads the pfSense 2.7.2 repository catalogue (packagesite), resolves open-vm-tools-nox11
and its dependencies, downloads each file, checks it against the catalogue's sha256 and
rewrites pkgs.lock. Review that diff and commit it; then upload DIR/*.pkg to the depot
(or carry them to TN-MGMT01 for ``stage --from-dir``). Trust there is HTTPS to the
pfSense repository plus the catalogue's sums; after that the lock is what counts.

Exit 0 on success, 1 with a message saying what to do otherwise. Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "infra/vsphere/packer/files/pfsense/pkgs.lock"
STAGE = REPO / "infra/vsphere/packer/stage/pfsense"
PFSENSE_VERSION = "2.7.2"
DEPOT_SUBDIR = f"installers/pfsense/{PFSENSE_VERSION}"
# /usr/local/etc/pkg/repos/pfSense.conf on a 2.7.2 install names this repository.
UPSTREAM = "https://pkg.pfsense.org/pfSense_v2_7_2_amd64-pfSense_v2_7_2"
ROOTS = ("open-vm-tools-nox11",)
# The depot's installers repository is not anonymous (#214): read it as tn-build-read.
DEPOT_READ_USER = "tn-build-read"
DEPOT_READ_KEY = "TN_DEPOT_BUILD_READ_PASSWORD"
DEPOT_ENV = Path("/srv/truenorth/config/depot-build.env")
LOCK_HEADER = """\
# TrueNorth Range - the offline Open-VM-Tools package set for the pfSense CE 2.7.2 template.
# One "<sha256>  <file>" line per package, in install order (dependencies first).
# tn-pfsense-finish.sh pkg-adds exactly these, after checking each sha256; the build fails
# (no template) if a file is missing or does not match.
# Written by: python3 scripts/lab/stage_pfsense_pkgs.py resolve (review, then commit).
"""


class StageError(Exception):
    """Something the operator has to fix; the message says what."""


def read_lock(path: Path = LOCK) -> list[tuple[str, str]]:
    """(sha256, file) pairs in install order. Raises StageError when nothing is pinned."""
    pins = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64 or not all(c in "0123456789abcdef" for c in parts[0]):
            raise StageError(f"{path}:{n}: expected '<sha256>  <file>', got {line!r}")
        if "/" in parts[1] or not parts[1].endswith(".pkg"):
            raise StageError(f"{path}:{n}: {parts[1]!r} is not a bare .pkg file name")
        pins.append((parts[0], parts[1]))
    if not pins:
        raise StageError(
            f"{path.relative_to(REPO) if path.is_relative_to(REPO) else path} pins no packages yet. "
            "On a host with internet access run: python3 scripts/lab/stage_pfsense_pkgs.py resolve "
            "--out <dir>, review and commit pkgs.lock, put <dir>/*.pkg on the depot "
            f"({DEPOT_SUBDIR}/), then stage again")
    return pins


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(stage: Path = STAGE, lock: Path = LOCK) -> list[str]:
    """Problems with the stage directory (empty list: ready to build)."""
    problems = []
    for want, name in read_lock(lock):
        f = stage / name
        if not f.is_file():
            problems.append(f"missing: {f}")
        elif sha256(f) != want:
            problems.append(f"sha256 mismatch: {f}")
    return problems


def depot_password(env_file: Path = DEPOT_ENV) -> str:
    """TN_DEPOT_BUILD_READ_PASSWORD: the environment, else depot-build.env; "" if neither."""
    value = os.environ.get(DEPOT_READ_KEY, "")
    if not value and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{DEPOT_READ_KEY}="):
                value = line.split("=", 1)[1].strip()
                break
    return value


def _download(url: str, dest: Path, auth: tuple[str, str] | None = None) -> None:
    req = urllib.request.Request(url)  # noqa: S310 - http(s) only, checked by the callers
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f)


def stage(*, depot: str | None, from_dir: Path | None, out: Path = STAGE, lock: Path = LOCK,
          depot_env: Path = DEPOT_ENV) -> list[str]:
    """Copy or fetch every locked file into ``out``, sha256-checked. Returns the file names."""
    if (depot is None) == (from_dir is None):
        raise StageError("give exactly one of --depot URL or --from-dir DIR")
    if depot is not None and not depot.startswith(("http://", "https://")):
        raise StageError(f"--depot must be an http(s) URL, got {depot!r}")
    pins = read_lock(lock)
    password = depot_password(depot_env) if depot is not None else ""
    auth = (DEPOT_READ_USER, password) if password else None
    out.mkdir(parents=True, exist_ok=True)
    names = []
    for want, name in pins:
        dest = out / name
        if dest.is_file() and sha256(dest) == want:
            names.append(name)
            continue
        tmp = out / f".{name}.part"
        try:
            if from_dir is not None:
                src = from_dir / name
                if not src.is_file():
                    raise StageError(f"{src} not found (pkgs.lock lists it)")
                shutil.copyfile(src, tmp)
            else:
                url = f"{depot.rstrip('/')}/{DEPOT_SUBDIR}/{name}"
                try:
                    _download(url, tmp, auth)
                except OSError as exc:
                    raise StageError(f"cannot fetch {url}: {exc}. Is it on the depot ({DEPOT_SUBDIR}/)?") from exc
            got = sha256(tmp)
            if got != want:
                raise StageError(f"{name}: sha256 {got} does not match pkgs.lock ({want}); not staged")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        names.append(name)
    for stale in out.glob("*.pkg"):
        if stale.name not in names:
            stale.unlink()
    return names


# ── resolve (the pinning step; needs the internet) ─────────────────────
def parse_catalogue(text: str) -> dict[str, dict]:
    """packagesite.yaml (one JSON object per line) -> {name: entry}."""
    cat = {}
    for line in text.splitlines():
        line = line.strip()
        if line:
            entry = json.loads(line)
            cat[entry["name"]] = entry
    return cat


def closure(cat: dict[str, dict], roots: tuple[str, ...] = ROOTS) -> list[dict]:
    """The roots and every dependency, dependencies first (a stable topological order)."""
    order: list[dict] = []
    state: dict[str, str] = {}

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "active":
            raise StageError(f"dependency cycle: {' -> '.join((*chain, name))}")
        if name not in cat:
            raise StageError(f"{name} (needed by {chain[-1] if chain else 'the template'}) is not in the catalogue")
        state[name] = "active"
        for dep in sorted(cat[name].get("deps") or {}):
            visit(dep, (*chain, name))
        state[name] = "done"
        order.append(cat[name])

    for root in roots:
        visit(root, ())
    return order


def write_lock(entries: list[tuple[str, str]], lock: Path = LOCK) -> None:
    lines = [LOCK_HEADER] + [f"{s}  {n}\n" for s, n in entries]
    lock.write_text("".join(lines), encoding="utf-8")


def _packagesite(repo: str, work: Path) -> str:
    last: Exception | None = None
    for name in ("packagesite.pkg", "packagesite.txz"):
        archive = work / name
        try:
            _download(f"{repo}/{name}", archive)
        except OSError as exc:
            last = exc
            continue
        r = subprocess.run(["tar", "-xOf", str(archive), "packagesite.yaml"], capture_output=True, check=False)
        if r.returncode == 0:
            return r.stdout.decode("utf-8")
        last = StageError(f"tar cannot read {name}: {r.stderr.decode(errors='replace').strip()}")
    raise StageError(f"no packagesite in {repo}: {last}")


def resolve(*, out: Path, repo: str = UPSTREAM, lock: Path = LOCK) -> list[tuple[str, str]]:
    repo = repo.rstrip("/")
    if not repo.startswith("https://"):
        raise StageError("--repo must be https")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        cat = parse_catalogue(_packagesite(repo, Path(work)))
    pins = []
    for entry in closure(cat):
        name = Path(entry["path"]).name
        dest = out / name
        _download(f"{repo}/{entry['path']}", dest)
        if sha256(dest) != entry["sum"]:
            dest.unlink()
            raise StageError(f"{name}: download does not match the catalogue's sha256")
        pins.append((entry["sum"], name))
        print(f"  {entry['sum']}  {name}")
    write_lock(pins, lock)
    return pins


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage", help="stage the locked .pkg files from the depot or a directory")
    s.add_argument("--depot", help="depot base URL, e.g. http://10.30.31.30:8081/repository")
    s.add_argument("--from-dir", type=Path, help="a directory holding the locked .pkg files")
    s.add_argument("--out", type=Path, default=STAGE)
    s.add_argument("--depot-env", type=Path, default=DEPOT_ENV, help="file holding TN_DEPOT_BUILD_READ_PASSWORD")
    c = sub.add_parser("check", help="exit 1 unless the stage directory matches pkgs.lock")
    c.add_argument("--out", type=Path, default=STAGE)
    r = sub.add_parser("resolve", help="pin pkgs.lock from the pfSense repository (internet)")
    r.add_argument("--out", type=Path, required=True, help="where to download the .pkg files")
    r.add_argument("--repo", default=UPSTREAM)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "stage":
            names = stage(depot=a.depot, from_dir=a.from_dir, out=a.out, depot_env=a.depot_env)
            print(f"staged {len(names)} package(s) in {a.out}")
        elif a.cmd == "check":
            problems = check(a.out)
            if problems:
                raise StageError("pfSense packages not staged: " + "; ".join(problems)
                                 + ". Run: python3 scripts/lab/stage_pfsense_pkgs.py stage --depot <depot_url>")
            print(f"pfSense packages staged in {a.out}: OK")
        else:
            pins = resolve(out=a.out, repo=a.repo)
            print(f"pinned {len(pins)} package(s) in {LOCK}; review and commit it")
    except StageError as exc:
        print(f"stage_pfsense_pkgs: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
