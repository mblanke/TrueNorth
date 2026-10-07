"""MITRE ATT&CK identifiers as TrueNorth accepts them from authors and feeds.

There is no ATT&CK catalogue in the repo, so "known" means well-formed: a technique
(``T1059``), a sub-technique (``T1059.001``) or a tactic (``TA0002``). Anything else
(``T59``, ``t1059``, ``attack.t1059``, free text) is refused rather than stored, so a
rule or indicator never carries an id that no ATT&CK lookup will ever resolve.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_ATTACK_ID = re.compile(r"^(?:T\d{4}(?:\.\d{3})?|TA\d{4})$")
_SPLIT = re.compile(r"[;,|\s]+")


def is_attack_id(value: str) -> bool:
    return bool(_ATTACK_ID.match(value))


def unknown_attack_ids(ids: Iterable[str]) -> list[str]:
    """The ids that are not ATT&CK technique, sub-technique or tactic ids, in order."""
    return [i for i in ids if not isinstance(i, str) or not is_attack_id(i.strip())]


def split_attack_ids(raw: str | None) -> list[str]:
    """``"T1059.001; T1071"`` -> ``["T1059.001", "T1071"]`` (separators: ; , | whitespace)."""
    return [part for part in _SPLIT.split(raw or "") if part]
