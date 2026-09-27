"""The ubuntu-2204 -> ubuntu-2404 rename must not strand anything already stored.

`ubuntu-lts` is Ubuntu 24.04 (docs/vm-build-sheet.md §8). Ranges, designer diagrams and
golden-image registries created before the rename still say `ubuntu-2204` /
`ubuntu-22.04`; those are deprecated aliases that must keep resolving to the same image.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from app import golden_images as api_gi
from app.models import GoldenImage
from template_engine import renderer as se_renderer
from worker import render as worker_render

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "content" / "catalogue" / "vm_iso_catalogue.csv"
RANGES = sorted((ROOT / "content" / "ranges").glob("*/template.yaml"))
OLD_NAMES = ("ubuntu-2204", "ubuntu-22.04", "ubuntu2204")


def _catalogue_resolver(csv_text: str):
    """The same alias lookup the worker builds from the DB, fed from a parsed catalogue."""
    by_id: dict[str, str] = {}
    by_alias: dict[str, str] = {}
    for row in api_gi.parse_catalogue(csv_text):
        if not row["enabled"]:
            continue
        by_id[row["catalogue_id"]] = row["template_name"]
        for a in row["os_aliases"]:
            by_alias.setdefault(a, row["template_name"])
    return lambda alias: by_id.get(alias) or by_alias.get(alias)


def _old_registry_resolver(alias: str) -> str | None:
    """A registry imported before the rename: it only knows the old spelling."""
    return {"ubuntu-lts": "ubuntu-lts", "ubuntu-2204": "ubuntu-lts"}.get(alias)


def _one_node(os_name: str) -> dict:
    return {"name": "t", "network": {"vlans": [{"id": 100, "name": "lan", "cidr": "10.1.0.0/24"}]},
            "nodes": [{"id": "n1", "os": os_name, "vlan": "lan"}]}


# ── the three copies of the table agree ──────────────────────────────────


def test_alias_tables_are_identical_across_api_worker_and_scenario_engine():
    assert worker_render.DEPRECATED_OS_ALIASES == api_gi.DEPRECATED_OS_ALIASES
    assert se_renderer.DEPRECATED_OS_ALIASES == api_gi.DEPRECATED_OS_ALIASES
    assert worker_render.EQUIVALENT_OS_ALIASES == api_gi.EQUIVALENT_OS_ALIASES


@pytest.mark.parametrize("old,new", [("ubuntu-2204", "ubuntu-2404"), ("ubuntu-22.04", "ubuntu-24.04"),
                                     ("ubuntu2204", "ubuntu-2404"), (" ubuntu-2204 ", "ubuntu-2404"),
                                     ("windows-11", "windows-11")])
def test_canonical_os(old, new):
    assert api_gi.canonical_os(old) == new
    assert worker_render.canonical_os(old) == new
    assert se_renderer.canonical_os(old) == new


# ── content and catalogue ────────────────────────────────────────────────


def test_shipped_ranges_use_the_new_name_only():
    assert RANGES, "no range templates found"
    for path in RANGES:
        text = path.read_text(encoding="utf-8-sig")
        for old in OLD_NAMES:
            assert old not in text, f"{path.parent.name} still uses deprecated {old}"


def test_catalogue_maps_old_and_new_names_to_ubuntu_lts():
    rows = {r["catalogue_id"]: r for r in api_gi.parse_catalogue(CATALOGUE.read_text(encoding="utf-8-sig"))}
    aliases = set(rows["ubuntu-lts"]["os_aliases"])
    assert {"ubuntu-2404", "ubuntu-24.04", *OLD_NAMES} <= aliases
    assert rows["ubuntu-lts"]["version"] == "24.04"


def test_every_shipped_ubuntu_node_resolves_against_the_catalogue():
    resolve = _catalogue_resolver(CATALOGUE.read_text(encoding="utf-8-sig"))
    for path in RANGES:
        tpl = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        out = worker_render.render_topology(tpl, "r-test", resolve)
        ubuntu = [v for v in out["vm_definitions"] if v["os"].startswith("ubuntu")]
        for vm in ubuntu:
            assert vm["template_name"] == "ubuntu-lts", f"{path.parent.name}/{vm['node_id']}"


# ── worker renderer: the path provisioning actually takes ────────────────


@pytest.mark.parametrize("stored", OLD_NAMES)
def test_worker_renders_a_stored_range_with_the_deprecated_alias(stored):
    resolve = _catalogue_resolver(CATALOGUE.read_text(encoding="utf-8-sig"))
    out = worker_render.render_topology(_one_node(stored), "r-test", resolve)
    vm = out["vm_definitions"][0]
    assert vm["template_name"] == "ubuntu-lts"
    assert vm["os"] == api_gi.canonical_os(stored)
    assert out["unresolved"] == []


def test_worker_resolves_the_new_name_against_a_registry_imported_before_the_rename():
    out = worker_render.render_topology(_one_node("ubuntu-2404"), "r-test", _old_registry_resolver)
    assert out["vm_definitions"][0]["template_name"] == "ubuntu-lts"
    assert out["unresolved"] == []


def test_worker_default_os_is_2404_for_asset_style_templates():
    tpl = {"name": "s", "inputs": {"cidr_lan": "10.2.0.0/24"}, "assets": [{"role": "user", "count": 1}]}
    out = worker_render.render_topology(tpl, "r-test", lambda a: None)
    assert out["vm_definitions"][0]["os"] == "ubuntu-2404"


def test_unrelated_unresolved_os_is_still_reported():
    out = worker_render.render_topology(_one_node("beos-5"), "r-test", _old_registry_resolver)
    assert out["unresolved"] == ["beos-5"]


# ── API registry ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["ubuntu-2404", "ubuntu-24.04", *OLD_NAMES])
def test_api_resolves_old_and_new_names_after_catalogue_import(db_session, alias):
    api_gi.import_catalogue(db_session, CATALOGUE.read_text(encoding="utf-8-sig"), hypervisor="vsphere")
    assert api_gi.resolve_template(db_session, alias, "vsphere") == "ubuntu-lts"


def _old_registry(db_session) -> None:
    db_session.add(GoldenImage(catalogue_id="ubuntu-lts", hypervisor="proxmox", template_name="ubuntu-2204-cloud",
                               os_aliases=json.dumps(["ubuntu-2204", "ubuntu-lts"]), enabled=True))
    db_session.flush()


def test_api_resolves_the_new_name_against_a_registry_imported_before_the_rename(db_session):
    _old_registry(db_session)
    assert api_gi.resolve_template(db_session, "ubuntu-2404", "proxmox") == "ubuntu-2204-cloud"
    assert api_gi.resolve_template(db_session, "ubuntu-24.04", "proxmox") == "ubuntu-2204-cloud"


def test_alias_map_offers_new_names_but_not_deprecated_ones_it_did_not_have(db_session):
    _old_registry(db_session)
    amap = api_gi.resolve_map(db_session, "proxmox")
    assert amap["ubuntu-2404"] == amap["ubuntu-24.04"] == "ubuntu-2204-cloud"
    assert "ubuntu-22.04" not in amap  # deprecated: resolvable, not offered
    assert api_gi.resolve_template(db_session, "beos-5", "proxmox") is None
