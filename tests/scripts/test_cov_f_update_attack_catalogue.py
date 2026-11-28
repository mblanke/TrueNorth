# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/update_attack_catalogue.py: the ATT&CK reduction over a synthetic STIX bundle —
tactics (deprecated and revoked ones dropped, shortnames mapped to TA ids), techniques with
their kill-chain tactics, deprecation/revocation and revoked-by resolution, the shared-id
rule where a live object beats a revoked one, the one-item-per-line rendering, and the
--source/--output/--check CLI against a local bundle file. load_bundle() is only exercised
with a local path: the default source is the MITRE CTI URL and this clone has no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_f_update_attack_catalogue", ROOT / "scripts" / "update_attack_catalogue.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

COPYRIGHT = "This work is licensed under a MITRE-style licence for tests."


def attack_pattern(ext_id, stix_id, *, name, phases=(), deprecated=False, revoked=False, modified=""):
    refs = [{"source_name": "mitre-attack", "external_id": ext_id}]
    return {
        "type": "attack-pattern",
        "id": stix_id,
        "name": name,
        "modified": modified,
        "external_references": refs,
        "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": p} for p in phases],
        "x_mitre_deprecated": deprecated,
        "revoked": revoked,
    }


def bundle():
    objects = [
        {"type": "marking-definition", "id": "mark--1", "definition": {"statement": COPYRIGHT}},
        # Tactic A is live, tactic B is deprecated, tactic C is revoked: only A and B count.
        {
            "type": "x-mitre-tactic",
            "id": "tactic-a",
            "name": "Recon",
            "x_mitre_shortname": "recon",
            "external_references": [{"source_name": "mitre-attack", "external_id": "TA0001"}],
        },
        {
            "type": "x-mitre-tactic",
            "id": "tactic-b",
            "name": "Old",
            "x_mitre_shortname": "old",
            "x_mitre_deprecated": True,
            "external_references": [{"source_name": "mitre-attack", "external_id": "TA0002"}],
        },
        {
            "type": "x-mitre-tactic",
            "id": "tactic-c",
            "name": "Gone",
            "x_mitre_shortname": "gone",
            "revoked": True,
            "external_references": [{"source_name": "mitre-attack", "external_id": "TA0003"}],
        },
        # T1555 lives in two tactics; T1003 is deprecated; T9999 has an unrecognised phase.
        attack_pattern(
            "T1555",
            "ap-1",
            name="Credentials from Password Stores",
            phases=("recon", "old"),
            modified="2025-03-05T00:00:00.000Z",
        ),
        attack_pattern(
            "T1003",
            "ap-2",
            name="OS Credential Dumping",
            phases=("recon",),
            deprecated=True,
            modified="2025-04-01T00:00:00.000Z",
        ),
        attack_pattern("T9999", "ap-3", name="Unknown Phase", phases=("gone",)),
        attack_pattern("G0001", "ap-4", name="A tool, not a technique", phases=("recon",)),
        {"type": "attack-pattern", "id": "ap-5", "name": "No ATT&CK reference", "modified": "2025-12-31T09:00:00.000Z"},
        # Two objects share T0001: an old revoked one and a live replacement.
        attack_pattern(
            "T0001", "ap-6", name="Old Way", phases=("recon",), revoked=True, modified="2024-01-01T00:00:00.000Z"
        ),
        attack_pattern("T0001", "ap-7", name="New Way", phases=("recon",), modified="2025-06-01T00:00:00.000Z"),
        # The revoked-by relationship is resolved through the STIX ids of its endpoints.
        {
            "type": "relationship",
            "id": "rel-1",
            "relationship_type": "revoked-by",
            "source_ref": "ap-6",
            "target_ref": "ap-7",
        },
        # An unrelated relationship type and dangling refs must be ignored.
        {
            "type": "relationship",
            "id": "rel-2",
            "relationship_type": "uses",
            "source_ref": "ap-1",
            "target_ref": "ap-2",
        },
        {
            "type": "relationship",
            "id": "rel-3",
            "relationship_type": "revoked-by",
            "source_ref": "ap-1",
            "target_ref": "missing",
        },
        # The newest modified date in the bundle pins the release date.
        {"type": "note", "id": "note-1", "modified": "2025-11-15T10:00:00.000Z"},
    ]
    return {"type": "bundle", "objects": objects}


def test_build_reduces_the_bundle_to_tactics_and_techniques():
    catalogue = mod.build(bundle(), "http://example.invalid/attack.json")
    assert catalogue["attribution"] == mod.ATTRIBUTION
    assert catalogue["source"] == "http://example.invalid/attack.json"
    assert catalogue["copyright"] == COPYRIGHT
    assert catalogue["attack_modified"] == "2025-12-31"
    assert catalogue["generated_by"] == "scripts/update_attack_catalogue.py"
    assert catalogue["tactics"] == {
        "TA0001": {"name": "Recon", "shortname": "recon"},
        "TA0002": {"name": "Old", "shortname": "old", "deprecated": True},
    }
    assert catalogue["techniques"] == {
        "T0001": {"name": "New Way", "tactics": ["TA0001"]},
        "T1003": {"name": "OS Credential Dumping", "tactics": ["TA0001"], "deprecated": True},
        "T1555": {"name": "Credentials from Password Stores", "tactics": ["TA0001", "TA0002"]},
        "T9999": {"name": "Unknown Phase", "tactics": []},
    }


def test_build_prefers_the_revoked_object_when_no_live_one_exists():
    objects = [attack_pattern("T0002", "ap-old", name="Only Way", revoked=True)]
    catalogue = mod.build({"objects": objects}, "s.json")
    assert catalogue["techniques"]["T0002"] == {"name": "Only Way", "tactics": [], "revoked": True}


def test_build_records_what_a_revoked_technique_was_replaced_by():
    objects = [
        attack_pattern("T0003", "ap-old", name="Old", revoked=True),
        attack_pattern("T0004", "ap-new", name="New"),
        {"type": "relationship", "relationship_type": "revoked-by", "source_ref": "ap-old", "target_ref": "ap-new"},
    ]
    catalogue = mod.build({"objects": objects}, "s.json")
    assert catalogue["techniques"]["T0003"] == {
        "name": "Old",
        "tactics": [],
        "revoked": True,
        "revoked_by": "T0004",
    }


def test_build_with_an_empty_bundle_stays_degradable():
    catalogue = mod.build({}, "s.json")
    assert catalogue["attack_modified"] is None
    assert catalogue["copyright"] is None
    assert catalogue["tactics"] == {} and catalogue["techniques"] == {}


def test_render_writes_one_item_per_line_round_tripping_through_json():
    text = mod.render(mod.build(bundle(), "s.json"))
    lines = text.splitlines()
    assert lines[0] == "{" and lines[-1] == "}"
    # Head keys first, then the tactics section, then techniques (no trailing comma).
    assert lines[1].strip().startswith('"attribution"')
    assert any(line.strip() == '"tactics": {' for line in lines)
    technique_lines = [line for line in lines if line.startswith('    "T') and not line.startswith('    "TA')]
    assert all(line.endswith(",") for line in technique_lines[:-1])
    assert technique_lines[-1].endswith("}")
    # Every line is valid JSON on its own, and the whole text parses back to the catalogue.
    assert json.loads(text) == mod.build(bundle(), "s.json")


def test_load_bundle_reads_a_local_file(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"objects": []}), encoding="utf-8")
    assert mod.load_bundle(str(path)) == {"objects": []}


def test_main_writes_the_catalogue_and_labels_a_local_source_with_the_public_url(tmp_path, capsys):
    src = tmp_path / "bundle.json"
    src.write_text(json.dumps(bundle()), encoding="utf-8")
    out = tmp_path / "mitre" / "catalogue.json"
    assert mod.main(["--source", str(src), "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["source"] == mod.SOURCE_URL  # provenance stays the public bundle
    assert len(data["techniques"]) == 4
    out_text = capsys.readouterr().out
    assert f"wrote {out}: ATT&CK as of 2025-12-31, 4 techniques, 2 tactics" in out_text


def test_main_check_fails_when_the_file_differs_and_passes_when_current(tmp_path, capsys):
    src = tmp_path / "bundle.json"
    src.write_text(json.dumps(bundle()), encoding="utf-8")
    out = tmp_path / "catalogue.json"
    # No output yet: --check fails without writing anything.
    assert mod.main(["--source", str(src), "--output", str(out), "--check"]) == 1
    assert f"{out} is out of date with {src}" in capsys.readouterr().err
    assert not out.exists()

    assert mod.main(["--source", str(src), "--output", str(out)]) == 0
    assert mod.main(["--source", str(src), "--output", str(out), "--check"]) == 0

    # A changed bundle makes the check fail again.
    src.write_text(json.dumps({"objects": []}), encoding="utf-8")
    assert mod.main(["--source", str(src), "--output", str(out), "--check"]) == 1
