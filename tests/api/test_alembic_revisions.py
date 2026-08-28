"""Structural guards on the migration tree.

The repo once carried two alembic trees — the live one under
``control-plane/api/alembic`` and an abandoned scaffold at the repo root — which
between them defined the *same revision id* (``a1b2c3d4e5f6``) twice. The root
tree was unreachable (``alembic.ini`` points at the live one), so the collision
was inert, but it was a trap: anyone running ``alembic`` from the wrong
directory, or restoring the orphan tree, would get a silently ambiguous history.

These tests make the trap loud rather than latent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_VERSIONS = REPO_ROOT / "control-plane" / "api" / "alembic" / "versions"

REV_RE = re.compile(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', re.M)
DOWN_RE = re.compile(r'^down_revision(?::\s*[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)', re.M)


def _revisions(directory: Path) -> dict[str, str | None]:
    """Map revision id -> down_revision for every migration in a directory."""
    out: dict[str, str | None] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("__"):
            continue
        text = path.read_text(encoding="utf-8")
        rev = REV_RE.search(text)
        if not rev:
            continue
        down = DOWN_RE.search(text)
        out[rev.group(1)] = down.group(1) if down and down.group(1) else None
    return out


def test_live_tree_exists():
    assert LIVE_VERSIONS.is_dir(), f"missing migration directory: {LIVE_VERSIONS}"
    assert _revisions(LIVE_VERSIONS), "no migrations found in the live tree"


def test_every_down_revision_resolves():
    revs = _revisions(LIVE_VERSIONS)
    for rev, down in revs.items():
        if down is not None:
            assert down in revs, f"revision {rev} points at unknown down_revision {down}"


def test_exactly_one_head():
    """Two heads mean an ambiguous `upgrade head` — usually a bad merge."""
    revs = _revisions(LIVE_VERSIONS)
    referenced = {down for down in revs.values() if down is not None}
    heads = sorted(set(revs) - referenced)
    assert len(heads) == 1, f"expected a single head, found {heads}"


def test_no_duplicate_revision_ids_in_the_live_tree():
    """A duplicate id inside one directory silently shadows a migration."""
    seen: dict[str, str] = {}
    for path in sorted(LIVE_VERSIONS.glob("*.py")):
        if path.name.startswith("__"):
            continue
        match = REV_RE.search(path.read_text(encoding="utf-8"))
        if not match:
            continue
        rev = match.group(1)
        assert rev not in seen, f"revision {rev} defined by both {seen[rev]} and {path.name}"
        seen[rev] = path.name


def test_no_second_migration_tree_has_reappeared():
    """The abandoned root tree is gone; keep it gone.

    If a second tree is ever reintroduced it must not reuse ids from the live
    one, or `alembic` resolves history differently depending on where it runs.
    """
    orphan = REPO_ROOT / "alembic" / "versions"
    if not orphan.is_dir():
        return

    live = set(_revisions(LIVE_VERSIONS))
    stray = set(_revisions(orphan))
    overlap = live & stray
    assert not overlap, (
        f"revision ids {sorted(overlap)} are defined in BOTH migration trees "
        f"({LIVE_VERSIONS} and {orphan}). Delete the orphan tree or renumber it."
    )
