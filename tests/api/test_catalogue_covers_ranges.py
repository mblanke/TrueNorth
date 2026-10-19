"""Every OS a shipped range names resolves to an enabled golden image (ADR 0012 decisions 1, 8).

The first range build on the work vCenter stopped at a template that did not exist. A range
that names an OS no enabled catalogue row answers for (``vyos-1.4`` while VyOS was a
disabled ``GAP-vyos`` row) can only fail the same way, so every ``os:`` in content/ranges
must resolve, through the same alias parsing the API import uses, to an enabled row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from app import golden_images as api_gi
from worker import render as worker_render

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "content" / "catalogue" / "vm_iso_catalogue.csv"
RANGES = sorted((ROOT / "content" / "ranges").glob("*/template.yaml"))


def _rows() -> dict[str, dict]:
    return {r["catalogue_id"]: r for r in api_gi.parse_catalogue(CATALOGUE.read_text(encoding="utf-8-sig"))}


def _enabled_by_alias() -> dict[str, str]:
    out: dict[str, str] = {}
    for cid, row in _rows().items():
        if row["enabled"]:
            for alias in row["os_aliases"]:
                out.setdefault(alias, cid)
    return out


def _os_values(doc) -> list[str]:
    """Every value of an ``os`` key anywhere in a range template."""
    found: list[str] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key == "os" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_os_values(value))
    elif isinstance(doc, list):
        for item in doc:
            found.extend(_os_values(item))
    return found


@pytest.mark.parametrize("path", RANGES, ids=lambda p: p.parent.name)
def test_every_os_in_a_range_resolves_to_an_enabled_catalogue_image(path):
    enabled = _enabled_by_alias()
    doc = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    named = _os_values(doc)
    assert named, f"{path.parent.name} names no os: (an asset without one silently becomes {worker_render.DEFAULT_OS})"
    missing = sorted({o for o in named
                      if not any(c in enabled for c in api_gi.os_alias_candidates(o))})
    assert not missing, f"{path.parent.name}: no enabled catalogue image for {missing}"
    # And the renderer, the path a build takes, leaves nothing unresolved.
    resolve = lambda alias: enabled.get(alias)  # noqa: E731
    assert worker_render.render_topology(doc, "r-test", resolve)["unresolved"] == []


def test_adr_0012_catalogue_rows():
    rows = _rows()
    assert rows["vyos"]["enabled"] and {"vyos", "vyos-1.4"} <= set(rows["vyos"]["os_aliases"])
    assert "GAP-vyos" not in rows
    assert "win10-ltsc" in rows["win10-22h2"]["os_aliases"]
    for cid in ("srv2025", "debian13", "parrot"):
        assert rows[cid]["enabled"], cid
    assert rows["kali"]["version"] == "2026.1" and rows["rocky"]["version"] == "10.2"
    assert not rows["c2-server"]["enabled"]
    csv_text = CATALOGUE.read_text(encoding="utf-8-sig")
    assert "vyos-2026.10.01-0035-rolling-generic-amd64.iso" in csv_text
    assert "kali-linux-2026.1-installer-everything-amd64.iso" in csv_text


def test_every_catalogue_id_is_a_packer_build_or_derived():
    """A row the Packer tree cannot build is a promise nothing keeps (ADR 0012 decision 2).

    Rows without a build yet are listed here on purpose, so a new one is a decision."""
    order = (ROOT / "infra" / "vsphere" / "packer" / "build.sh").read_text()
    not_built_here = {"win-xp-sp3", "win7-sp1", "blueteam-soc"}  # licensed media / composite stack
    missing = sorted(cid for cid, row in _rows().items()
                     if row["enabled"] and cid not in not_built_here and f" {cid}" not in order)
    assert not missing, f"enabled catalogue rows with no Packer build in build.sh ORDER: {missing}"
