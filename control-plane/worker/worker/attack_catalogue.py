"""The MITRE ATT&CK Enterprise catalogue TrueNorth validates technique ids against.

The data is ``content/mitre/enterprise-attack-techniques.json``, generated from MITRE's
STIX bundle by ``scripts/update_attack_catalogue.py`` (terms of use and attribution in
``content/mitre/README.md``). Neither image is built with content/ in its context, so the
file is found through ``TN_ATTACK_CATALOGUE``, then ``/app/content/mitre`` (the compose
mount), then the repository checkout this module sits in.

What "known" means depends on who is asking:

* An author (detection rules): the id is an ATT&CK Enterprise technique, sub-technique or
  tactic and MITRE has not revoked it. ``problem()`` says why an id is refused.
* Telemetry: ``current()`` maps an id to the technique it stands for today (a revoked id
  becomes the id MITRE replaced it with), or None when the catalogue does not know it.

This file exists twice, byte for byte: control-plane/api/app/attack_catalogue.py and
control-plane/worker/worker/attack_catalogue.py (the two images share no code, and the
API may not import the worker). tests/contracts/test_attack_catalogue.py fails if they
drift; edit one, copy it over the other.
"""

from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ATTACK_ID = re.compile(r"^(?:T\d{4}(?:\.\d{3})?|TA\d{4})$")

_CATALOGUE_REL = Path("content") / "mitre" / "enterprise-attack-techniques.json"
# In the repo this file is <repo>/control-plane/{api/app,worker/worker}/; in an image it
# is /app/{app,worker}/, which has only two parents, so never index parents blindly.
_HERE = Path(__file__).resolve()
_REPO_COPY = (_HERE.parents[3] / _CATALOGUE_REL) if len(_HERE.parents) > 3 else _CATALOGUE_REL
_CONTAINER_COPY = Path("/app") / _CATALOGUE_REL


class CatalogueUnavailableError(RuntimeError):
    """The catalogue file is missing or is not the generated JSON."""


@dataclass(frozen=True)
class Technique:
    id: str
    name: str
    tactics: tuple[str, ...]
    deprecated: bool = False
    revoked: bool = False
    revoked_by: str | None = None


@dataclass(frozen=True)
class Catalogue:
    techniques: dict[str, Technique]
    tactics: dict[str, str]  # TA id -> name
    attack_modified: str | None = None

    def problem(self, attack_id: object) -> str | None:
        """Why an author may not use ``attack_id``; None when it is a live ATT&CK id."""
        if not isinstance(attack_id, str) or not ATTACK_ID.match(attack_id):
            return "is not of the form T1234, T1234.001 or TA0001"
        if attack_id.startswith("TA"):
            return None if attack_id in self.tactics else "is not an ATT&CK Enterprise tactic"
        technique = self.techniques.get(attack_id)
        if technique is None:
            return "is not an ATT&CK Enterprise technique"
        if technique.revoked:
            return "was revoked by MITRE" + (f"; use {technique.revoked_by}" if technique.revoked_by else "")
        return None

    def current(self, technique_id: str) -> str | None:
        """The live technique id ``technique_id`` stands for, or None if it is unknown."""
        seen: set[str] = set()
        technique = self.techniques.get(technique_id)
        while technique is not None and technique.revoked and technique.id not in seen:
            seen.add(technique.id)
            technique = self.techniques.get(technique.revoked_by or "")
        return technique.id if technique is not None and not technique.revoked else None


def catalogue_path() -> Path:
    env = os.environ.get("TN_ATTACK_CATALOGUE", "").strip()
    if env:
        return Path(env)
    return _CONTAINER_COPY if _CONTAINER_COPY.is_file() else _REPO_COPY


@functools.lru_cache(maxsize=1)
def load() -> Catalogue:
    """The catalogue, read once. Raises ``CatalogueUnavailableError``."""
    path = catalogue_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        techniques = {
            tid: Technique(
                id=tid,
                name=str(entry.get("name", "")),
                tactics=tuple(entry.get("tactics") or ()),
                deprecated=bool(entry.get("deprecated")),
                revoked=bool(entry.get("revoked")),
                revoked_by=entry.get("revoked_by"),
            )
            for tid, entry in raw["techniques"].items()
        }
        tactics = {ta: str(entry.get("name", "")) for ta, entry in raw["tactics"].items()}
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        raise CatalogueUnavailableError(f"ATT&CK catalogue unreadable at {path}: {exc}") from exc
    if not techniques or not tactics:
        raise CatalogueUnavailableError(f"ATT&CK catalogue at {path} is empty")
    return Catalogue(techniques=techniques, tactics=tactics, attack_modified=raw.get("attack_modified"))
