"""Order TrueNorth release tags: ``vX.Y.Z`` and ``vX.Y.Z-rcN``.

Ansible's ``version(..., version_type='semver')`` follows SemVer 2.0 to the letter: a
prerelease identifier that is not all digits is compared as an ASCII string, so ``rc12``
sorts *before* ``rc9``. On the work install (2026-10-20) that made every upgrade from
v1.2.0-rc9 to v1.2.0-rc12 fail as a "downgrade".

Here a pair of prerelease identifiers made of the same letters followed by digits (``rc9``
and ``rc12``; ``beta2`` and ``beta10``) compares the numbers numerically. Everything else
is SemVer 2.0 precedence: the core ``X.Y.Z`` numerically, a final release after every
prerelease of the same core, numeric identifiers numerically and before alphanumeric ones,
other identifiers as ASCII, a longer identifier list after its own prefix, build metadata
ignored.

Pure Python with no Ansible import, so tests load it directly.
"""

from __future__ import annotations

import re

_VERSION = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_LETTERS_NUMBER = re.compile(r"^(?P<letters>[A-Za-z]+)(?P<number>\d+)$")


def _parse(tag: object) -> tuple[tuple[int, int, int], list[str]]:
    text = str(tag).strip()
    match = _VERSION.match(text)
    if match is None:
        raise ValueError(f"not a release tag (vX.Y.Z or vX.Y.Z-suffix): {text!r}")
    core = (int(match["major"]), int(match["minor"]), int(match["patch"]))
    pre = match["pre"].split(".") if match["pre"] else []
    return core, pre


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


def _cmp_identifier(a: str, b: str) -> int:
    a_num, b_num = a.isdigit(), b.isdigit()
    if a_num and b_num:
        return _cmp(int(a), int(b))
    if a_num != b_num:  # SemVer: a numeric identifier sorts before an alphanumeric one
        return -1 if a_num else 1
    ma, mb = _LETTERS_NUMBER.match(a), _LETTERS_NUMBER.match(b)
    if ma and mb and ma["letters"] == mb["letters"]:
        return _cmp(int(ma["number"]), int(mb["number"]))
    return _cmp(a, b)


def tn_release_cmp(a: object, b: object) -> int:
    """-1, 0 or 1 as release tag ``a`` is older than, the same as, or newer than ``b``."""
    core_a, pre_a = _parse(a)
    core_b, pre_b = _parse(b)
    if core_a != core_b:
        return _cmp(core_a, core_b)
    if not pre_a or not pre_b:  # a final release sorts after all of its prereleases
        return _cmp(not pre_a, not pre_b)
    for ia, ib in zip(pre_a, pre_b, strict=False):  # unequal lengths: decided below
        result = _cmp_identifier(ia, ib)
        if result:
            return result
    return _cmp(len(pre_a), len(pre_b))


class FilterModule:
    """``{{ tn_release_version | tn_release_cmp(previous) < 0 }}`` means "older than"."""

    def filters(self):
        return {"tn_release_cmp": tn_release_cmp}
