"""Deploy-time software catalogue: a node's ``services`` names -> install specs per OS family.

The catalogue is ``content/catalogue/software_catalogue.yaml``. The worker image copies
content/catalogue to ``/app/content/catalogue`` (a named ``content`` build context), so
the file is found through ``TN_SOFTWARE_CATALOGUE``, then ``/app/content/catalogue`` (the
image), then the repository checkout this module sits in. Pure apart from reading that file.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_CATALOGUE_REL = Path("content") / "catalogue" / "software_catalogue.yaml"
# In the repo this file is <repo>/control-plane/worker/worker/; in the image it is
# /app/worker/, which has only two parents — so never index parents at import time.
_HERE = Path(__file__).resolve()
_REPO_COPY = (_HERE.parents[3] / _CATALOGUE_REL) if len(_HERE.parents) > 3 else _CATALOGUE_REL
_CONTAINER_COPY = Path("/app/content/catalogue/software_catalogue.yaml")

# Red Hat family guests use dnf; everything else Linux uses apt.
_DNF_PREFIXES = ("rocky", "rhel", "redhat", "centos", "alma", "fedora", "oracle", "ol8", "ol9")


class CatalogueError(ValueError):
    """The catalogue file is missing or malformed."""


@dataclass(frozen=True)
class InstallSpec:
    """One catalogue entry resolved for one VM."""

    name: str  # the catalogue name (canonical, not the alias the node used)
    manager: str  # choco | apt | dnf
    packages: tuple[str, ...]
    version: str | None = None
    # False: the install may need the internet (a Chocolatey wrapper that downloads the
    # vendor installer), so it can fail in a no-egress range. See is_offline().
    offline: bool = True


@dataclass(frozen=True)
class Catalogue:
    entries: dict  # canonical name -> entry dict
    aliases: dict  # lower-case name or alias -> canonical name
    roles: frozenset


def _key(name: str) -> str:
    return str(name or "").strip().lower()


def catalogue_path() -> Path:
    env = os.environ.get("TN_SOFTWARE_CATALOGUE", "").strip()
    if env:
        return Path(env)
    return _CONTAINER_COPY if _CONTAINER_COPY.is_file() else _REPO_COPY


def parse(doc: dict) -> Catalogue:
    if not isinstance(doc, dict) or not isinstance(doc.get("software"), dict):
        raise CatalogueError("software catalogue has no 'software' mapping")
    entries: dict = {}
    aliases: dict = {}
    for name, entry in doc["software"].items():
        entry = entry or {}
        if not isinstance(entry, dict):
            raise CatalogueError(f"software catalogue entry {name!r} is not a mapping")
        canonical = _key(name)
        entries[canonical] = entry
        for alias in [canonical, *(entry.get("aliases") or [])]:
            aliases.setdefault(_key(alias), canonical)
    roles = frozenset(_key(r) for r in doc.get("roles") or [])
    return Catalogue(entries=entries, aliases=aliases, roles=roles)


@functools.lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Catalogue:
    try:
        with open(path, encoding="utf-8") as fh:
            return parse(yaml.safe_load(fh) or {})
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogueError(f"cannot read software catalogue {path}: {exc}") from exc


def load(path: str | os.PathLike | None = None) -> Catalogue:
    """The catalogue at ``path`` (default: see catalogue_path), re-read when it changes."""
    p = Path(path) if path else catalogue_path()
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        raise CatalogueError(f"software catalogue not found at {p}: {exc}") from exc
    return _load(str(p), mtime)


def is_offline(entry: dict) -> bool:
    """Whether an entry's Windows (Chocolatey) install works with no internet.

    The entry's ``offline`` flag when it has one; otherwise false for an entry with a
    windows block (unknown, so assume it downloads at install time) and true for a
    Linux-only one (apt/dnf go through the depot's caching proxy).
    """
    flag = (entry or {}).get("offline")
    if isinstance(flag, bool):
        return flag
    win = (entry or {}).get("windows")
    return not (isinstance(win, dict) and win.get("choco"))


def linux_manager(os_name: str) -> str:
    """``dnf`` for Red Hat family guests, else ``apt``."""
    return "dnf" if _key(os_name).startswith(_DNF_PREFIXES) else "apt"


def resolve(services, family: str, os_name: str, catalogue: Catalogue) -> tuple[list[InstallSpec], list[str]]:
    """Install specs for a node's ``services`` on ``family`` (windows | linux), and warnings.

    Role names (dns, iis, ...) are skipped silently. Unknown names, and known names with
    no install for this OS family, are skipped with a warning. Each name is installed
    once, in the order given.
    """
    specs: list[InstallSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw in services or []:
        key = _key(raw)
        if not key or key in catalogue.roles:
            continue
        canonical = catalogue.aliases.get(key)
        if canonical is None:
            warnings.append(f"software {raw!r} is not in the software catalogue; skipped")
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        entry = catalogue.entries[canonical]
        block = entry.get(family) or {}
        if family == "windows" and block.get("choco"):
            version = block.get("version")
            specs.append(InstallSpec(canonical, "choco", (str(block["choco"]),), str(version) if version else None,
                                     offline=is_offline(entry)))
            continue
        if family == "linux":
            manager = linux_manager(os_name)
            pkgs = block.get(manager) or []
            if isinstance(pkgs, str):
                pkgs = [pkgs]
            if pkgs:
                specs.append(InstallSpec(canonical, manager, tuple(str(p) for p in pkgs)))
                continue
            warnings.append(f"software {raw!r} has no {manager} install in the catalogue; skipped")
            continue
        warnings.append(f"software {raw!r} has no {family} install in the catalogue; skipped")
    return specs, warnings
