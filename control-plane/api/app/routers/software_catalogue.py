"""Read-only view of the deploy-time software catalogue, for the Range Designer.

GET /software-catalogue   any authenticated user

The catalogue is ``content/catalogue/software_catalogue.yaml``; the provisioning worker
reads it to turn a node's ``services`` names into installs. The API image copies
content/catalogue in (a named ``content`` build context), so the file is found through
``TN_SOFTWARE_CATALOGUE``, then ``/app/content/catalogue`` (the image), then the
repository checkout. This
is a deliberately small loader: it reports names, aliases, OS families and whether the
Windows install is offline-ready, and never
resolves installs, which stay the worker's business.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import safe_yaml
from ..auth import CurrentUser, get_current_user

router = APIRouter(prefix="/software-catalogue", tags=["software-catalogue"])

# In the repository this file is control-plane/api/app/routers/; in the API image it is
# /app/app/routers/, which has only three parents, so never index parents unguarded at
# import time (tests/api/test_api_image_layout_import.py).
_HERE = Path(__file__).resolve()
_CATALOGUE_REL = Path("content") / "catalogue" / "software_catalogue.yaml"
_REPO_COPY = (_HERE.parents[4] / _CATALOGUE_REL) if len(_HERE.parents) > 4 else _CATALOGUE_REL
_CONTAINER_COPY = Path("/app/content/catalogue/software_catalogue.yaml")


class SoftwareEntryOut(BaseModel):
    name: str
    aliases: list[str]
    os_families: list[str]  # subset of ["windows", "linux"]
    # The Windows (Chocolatey) install works with no internet: the package embeds its
    # installer or is internalized (content/choco). Same rule as the worker's is_offline.
    offline: bool


class SoftwareCatalogueOut(BaseModel):
    software: list[SoftwareEntryOut]
    roles: list[str]  # names that describe a node's role; never installed


def catalogue_path() -> Path:
    env = os.environ.get("TN_SOFTWARE_CATALOGUE", "").strip()
    if env:
        return Path(env)
    return _CONTAINER_COPY if _CONTAINER_COPY.is_file() else _REPO_COPY


def _key(name: object) -> str:
    return str(name or "").strip().lower()


def _families(entry: dict) -> list[str]:
    out = []
    win = entry.get("windows")
    if isinstance(win, dict) and win.get("choco"):
        out.append("windows")
    lin = entry.get("linux")
    if isinstance(lin, dict) and (lin.get("apt") or lin.get("dnf")):
        out.append("linux")
    return out


def _offline(entry: dict) -> bool:
    """The entry's ``offline`` flag; unset: false with a windows block, true otherwise."""
    flag = entry.get("offline")
    if isinstance(flag, bool):
        return flag
    return "windows" not in _families(entry)


def summarise(doc: object) -> SoftwareCatalogueOut:
    """Names, aliases and OS families from a parsed catalogue document."""
    if not isinstance(doc, dict) or not isinstance(doc.get("software"), dict):
        raise ValueError("software catalogue has no 'software' mapping")
    software = []
    for name, entry in doc["software"].items():
        entry = entry or {}
        if not isinstance(entry, dict):
            raise ValueError(f"software catalogue entry {name!r} is not a mapping")
        software.append(SoftwareEntryOut(
            name=_key(name),
            aliases=[_key(a) for a in entry.get("aliases") or [] if _key(a)],
            os_families=_families(entry),
            offline=_offline(entry),
        ))
    software.sort(key=lambda e: e.name)
    roles = sorted({_key(r) for r in doc.get("roles") or [] if _key(r)})
    return SoftwareCatalogueOut(software=software, roles=roles)


@functools.lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> SoftwareCatalogueOut:
    with open(path, encoding="utf-8") as fh:
        return summarise(safe_yaml.load(fh.read()) or {})


@router.get("", response_model=SoftwareCatalogueOut)
def get_software_catalogue(_user: CurrentUser = Depends(get_current_user)) -> SoftwareCatalogueOut:
    """Software names a node's ``services`` can use, with aliases and OS families."""
    path = catalogue_path()
    try:
        return _load(str(path), path.stat().st_mtime)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        # 503, not an empty list: an empty catalogue would read as "nothing installs".
        raise HTTPException(503, f"software catalogue unavailable: {exc}") from exc
