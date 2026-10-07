"""tools/arc2/assign_owner.py: give unowned Course Studio runs a tenant.

Runs started from Claude Code, or created before the Studio recorded ownership, have no
``tenant_id`` and the API shows them to nobody. The tool records one. It never moves a
run from one tenant to another, never edits the engine's manifest, and running it twice
changes nothing the second time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arc2 import assign_owner

TENANT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def runs(tmp_path) -> Path:
    root = tmp_path / "arc2"
    for slug in ("arc2-cli-only", "arc2-old-studio", "arc2-owned", "arc2-theirs"):
        (root / slug).mkdir(parents=True)
        (root / slug / "manifest.json").write_text(json.dumps({"slug": slug, "course": {"title": f"Title {slug}"}}))
    studio = root / "_studio"
    studio.mkdir()
    (studio / "arc2-old-studio.json").write_text(json.dumps({"name": "Old", "created_by": "a@example.test"}))
    (studio / "arc2-owned.json").write_text(json.dumps({"name": "Owned", "tenant_id": TENANT}))
    (studio / "arc2-theirs.json").write_text(json.dumps({"name": "Theirs", "tenant_id": OTHER}))
    (root / "not-a-run").mkdir()
    return root


def meta(runs: Path, slug: str) -> dict:
    return json.loads((runs / "_studio" / f"{slug}.json").read_text())


def test_all_unowned_assigns_only_the_runs_without_an_owner(runs, capsys):
    assert assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--all-unowned"]) == 0
    assert meta(runs, "arc2-cli-only")["tenant_id"] == TENANT
    assert meta(runs, "arc2-cli-only")["name"] == "Title arc2-cli-only"
    assert meta(runs, "arc2-old-studio") | {"tenant_id": TENANT} == meta(runs, "arc2-old-studio")
    assert meta(runs, "arc2-old-studio")["created_by"] == "a@example.test"
    assert meta(runs, "arc2-theirs")["tenant_id"] == OTHER
    out = capsys.readouterr().out
    assert "unowned before: 2" in out and "assigned: 2" in out and "unowned after: 0" in out


def test_it_is_idempotent(runs, capsys):
    assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--all-unowned"])
    first = {p.name: p.read_text() for p in (runs / "_studio").glob("*.json")}
    capsys.readouterr()
    assert assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--all-unowned"]) == 0
    assert {p.name: p.read_text() for p in (runs / "_studio").glob("*.json")} == first
    assert "assigned: 0" in capsys.readouterr().out


def test_dry_run_writes_nothing(runs, capsys):
    before = {p.name: p.read_text() for p in (runs / "_studio").glob("*.json")}
    assert assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--all-unowned", "--dry-run"]) == 0
    assert {p.name: p.read_text() for p in (runs / "_studio").glob("*.json")} == before
    assert "would assign: 2" in capsys.readouterr().out


def test_a_named_run_owned_by_another_tenant_is_refused_not_moved(runs, capsys):
    assert assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--slug", "arc2-theirs"]) == 1
    assert meta(runs, "arc2-theirs")["tenant_id"] == OTHER
    assert "arc2-theirs: owned by another tenant" in capsys.readouterr().out


def test_a_named_slug_must_exist_and_be_a_run(runs, capsys):
    assert assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--slug", "arc2-missing"]) == 1
    assert assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--slug", "../etc"]) == 1
    assert not (runs / "_studio" / "arc2-missing.json").exists()


def test_the_tenant_must_be_a_uuid(runs):
    with pytest.raises(SystemExit):
        assign_owner.main(["--runs", str(runs), "--tenant", "dev", "--all-unowned"])


def test_the_manifest_is_never_touched(runs):
    before = (runs / "arc2-cli-only" / "manifest.json").read_text()
    assign_owner.main(["--runs", str(runs), "--tenant", TENANT, "--all-unowned"])
    assert (runs / "arc2-cli-only" / "manifest.json").read_text() == before
