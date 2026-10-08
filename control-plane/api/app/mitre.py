"""MITRE ATT&CK identifiers as TrueNorth accepts them from authors and feeds.

Two levels of strictness:

* ``unknown_attack_ids`` checks form only: a technique (``T1059``), a sub-technique
  (``T1059.001``) or a tactic (``TA0002``). Anything else (``T59``, ``t1059``,
  ``attack.t1059``, free text) is refused. Threat-intel feeds use this; a third party's
  feed may lag an ATT&CK release and its indicators are still worth having.
* ``attack_id_problems`` also looks the id up in the vendored ATT&CK Enterprise catalogue
  (attack_catalogue.py): an id MITRE never issued or has revoked is refused. Detection
  rules, which TrueNorth authors own, use this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .attack_catalogue import ATTACK_ID, load

_SPLIT = re.compile(r"[;,|\s]+")


def is_attack_id(value: str) -> bool:
    return bool(ATTACK_ID.match(value))


def unknown_attack_ids(ids: Iterable[str]) -> list[str]:
    """The ids that are not ATT&CK technique, sub-technique or tactic ids, in order."""
    return [i for i in ids if not isinstance(i, str) or not is_attack_id(i.strip())]


def attack_id_problems(ids: Iterable[str]) -> dict[str, str]:
    """``{id: reason}`` for every id the ATT&CK catalogue refuses, in order.

    Raises ``attack_catalogue.CatalogueUnavailableError`` when the catalogue cannot be read;
    the caller decides how to fail (a rule is never stored unchecked).
    """
    catalogue = load()
    problems: dict[str, str] = {}
    for i in ids:
        if (reason := catalogue.problem(i)) is not None:
            problems[str(i)] = reason
    return problems


def split_attack_ids(raw: str | None) -> list[str]:
    """``"T1059.001; T1071"`` -> ``["T1059.001", "T1071"]`` (separators: ; , | whitespace)."""
    return [part for part in _SPLIT.split(raw or "") if part]
