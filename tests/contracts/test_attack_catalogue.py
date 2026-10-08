"""The vendored ATT&CK Enterprise catalogue (content/mitre) and the module that reads it.

The API and worker images share no code, so attack_catalogue.py is two identical files;
the first test fails the moment they drift. The rest pin what "known" means, check the
repository's own detection content against the catalogue, and run the generator on a
small synthetic STIX bundle so its rules are fixed without downloading MITRE's.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from app import attack_catalogue as api_catalogue

pytest.importorskip("celery")
from worker import attack_catalogue as worker_catalogue  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
API_COPY = ROOT / "control-plane/api/app/attack_catalogue.py"
WORKER_COPY = ROOT / "control-plane/worker/worker/attack_catalogue.py"
CATALOGUE = ROOT / "content/mitre/enterprise-attack-techniques.json"
README = ROOT / "content/mitre/README.md"
DETECTIONS = sorted((ROOT / "content/detections").glob("*.y*ml"))


def _generator():
    spec = importlib.util.spec_from_file_location("update_attack_catalogue", ROOT / "scripts/update_attack_catalogue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_and_worker_copies_are_identical():
    assert API_COPY.read_bytes() == WORKER_COPY.read_bytes(), (
        "control-plane/api/app/attack_catalogue.py and control-plane/worker/worker/attack_catalogue.py "
        "differ; edit one and copy it over the other"
    )


@pytest.fixture(params=[api_catalogue, worker_catalogue], ids=["api", "worker"])
def catalogue(request):
    return request.param.load()


# -- the vendored file ---------------------------------------------------------------------
def test_the_catalogue_carries_mitre_attribution_and_its_readme_the_terms_of_use():
    raw = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    assert "The MITRE Corporation" in raw["attribution"] and "terms-of-use" in raw["attribution"]
    assert raw["source"].endswith("enterprise-attack/enterprise-attack.json")
    readme = README.read_text(encoding="utf-8")
    assert "attack.mitre.org/resources/legal-and-branding/terms-of-use" in readme
    assert "scripts/update_attack_catalogue.py" in readme


def test_the_catalogue_is_the_generators_own_rendering():
    """Hand edits are caught: re-rendering the parsed file must reproduce it byte for byte."""
    text = CATALOGUE.read_text(encoding="utf-8")
    assert _generator().render(json.loads(text)) == text


def test_the_catalogue_is_a_whole_matrix(catalogue):
    assert len(catalogue.techniques) > 500 and len(catalogue.tactics) >= 14
    assert catalogue.tactics["TA0002"] == "Execution"
    assert catalogue.techniques["T1059.001"].name == "PowerShell"
    assert "TA0002" in catalogue.techniques["T1059.001"].tactics
    for technique in catalogue.techniques.values():
        assert set(technique.tactics) <= set(catalogue.tactics), technique.id
        if technique.revoked_by:
            assert technique.revoked and technique.revoked_by in catalogue.techniques, technique.id


# -- what "known" means --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("attack_id", "problem"),
    [
        ("T1059", None),
        ("T1059.001", None),
        ("TA0002", None),
        ("T1064", None),  # deprecated, not revoked: still a valid reference
        ("T9999", "is not an ATT&CK Enterprise technique"),
        ("T1059.999", "is not an ATT&CK Enterprise technique"),
        ("TA9999", "is not an ATT&CK Enterprise tactic"),
        ("T1086", "was revoked by MITRE; use T1059.001"),
        ("t1059", "is not of the form T1234, T1234.001 or TA0001"),
        ("T1059.1", "is not of the form T1234, T1234.001 or TA0001"),
        ("", "is not of the form T1234, T1234.001 or TA0001"),
        (1059, "is not of the form T1234, T1234.001 or TA0001"),
    ],
)
def test_problem(catalogue, attack_id, problem):
    assert catalogue.problem(attack_id) == problem


@pytest.mark.parametrize(
    ("technique_id", "current"),
    [("T1059.001", "T1059.001"), ("T1086", "T1059.001"), ("T1064", "T1064"), ("T9999", None), ("TA0002", None)],
)
def test_current(catalogue, technique_id, current):
    assert catalogue.current(technique_id) == current


def test_a_revocation_cycle_does_not_loop():
    t = api_catalogue.Technique
    cat = api_catalogue.Catalogue(
        techniques={
            "T0001": t("T0001", "a", (), revoked=True, revoked_by="T0002"),
            "T0002": t("T0002", "b", (), revoked=True, revoked_by="T0001"),
        },
        tactics={},
    )
    assert cat.current("T0001") is None


def test_the_env_override_and_an_unreadable_file(tmp_path, monkeypatch):
    bad = tmp_path / "catalogue.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("TN_ATTACK_CATALOGUE", str(bad))
    api_catalogue.load.cache_clear()
    try:
        assert api_catalogue.catalogue_path() == bad
        with pytest.raises(api_catalogue.CatalogueUnavailableError, match="unreadable"):
            api_catalogue.load()
        bad.write_text(json.dumps({"tactics": {}, "techniques": {}}), encoding="utf-8")
        api_catalogue.load.cache_clear()
        with pytest.raises(api_catalogue.CatalogueUnavailableError, match="empty"):
            api_catalogue.load()
    finally:
        monkeypatch.delenv("TN_ATTACK_CATALOGUE")
        api_catalogue.load.cache_clear()
    assert api_catalogue.catalogue_path() == CATALOGUE


# -- the repository's own content ----------------------------------------------------------
def test_there_is_detection_content_to_check():
    assert DETECTIONS


@pytest.mark.parametrize("path", DETECTIONS, ids=lambda p: p.name)
def test_detection_content_names_only_live_attack_ids(path):
    """content/detections may not cite an id MITRE never issued, revoked or deprecated."""
    catalogue = api_catalogue.load()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Files use either key; Sigma's own form is a tag such as attack.t1059.001.
    ids = list(doc.get("mitre_attack") or []) + list(doc.get("mitre_technique") or [])
    ids += [t.split(".", 1)[1].upper() for t in doc.get("tags") or [] if str(t).lower().startswith("attack.t")]
    assert ids, f"{path.name} names no ATT&CK ids (mitre_attack, mitre_technique or attack.* tags)"
    for attack_id in ids:
        assert catalogue.problem(attack_id) is None, f"{path.name}: {attack_id!r} {catalogue.problem(attack_id)}"
        if not attack_id.startswith("TA"):
            assert not catalogue.techniques[attack_id].deprecated, f"{path.name}: {attack_id} is deprecated"


# -- the generator -------------------------------------------------------------------------
def _ref(ext_id):
    return [{"source_name": "mitre-attack", "external_id": ext_id}]


def _phase(name):
    return [{"kill_chain_name": "mitre-attack", "phase_name": name}]


BUNDLE = {
    "type": "bundle",
    "objects": [
        {
            "type": "marking-definition",
            "id": "marking-definition--1",
            "modified": "2020-01-01T00:00:00Z",
            "definition": {"statement": "Copyright The MITRE Corporation."},
        },
        {"type": "x-mitre-tactic", "id": "x-mitre-tactic--1", "name": "Execution", "x_mitre_shortname": "execution",
         "external_references": _ref("TA0002"), "modified": "2026-01-02T00:00:00Z"},
        {"type": "attack-pattern", "id": "attack-pattern--new", "name": "PowerShell",
         "external_references": _ref("T1059.001"), "kill_chain_phases": _phase("execution")},
        {"type": "attack-pattern", "id": "attack-pattern--old", "name": "PowerShell", "revoked": True,
         "external_references": _ref("T1086"), "kill_chain_phases": _phase("execution")},
        {"type": "attack-pattern", "id": "attack-pattern--dep", "name": "Scripting", "x_mitre_deprecated": True,
         "external_references": _ref("T1064"), "kill_chain_phases": _phase("execution") + _phase("no-such-tactic")},
        # Same id twice: the revoked copy must not shadow the live one.
        {"type": "attack-pattern", "id": "attack-pattern--dup-old", "name": "Old", "revoked": True,
         "external_references": _ref("T1003")},
        {"type": "attack-pattern", "id": "attack-pattern--dup-new", "name": "OS Credential Dumping",
         "external_references": _ref("T1003"), "modified": "2026-03-04T00:00:00Z"},
        {"type": "relationship", "id": "relationship--1", "relationship_type": "revoked-by",
         "source_ref": "attack-pattern--old", "target_ref": "attack-pattern--new"},
        {"type": "malware", "id": "malware--1", "name": "x", "external_references": _ref("S0001")},
    ],
}  # fmt: skip


def test_the_generator_reduces_a_stix_bundle_to_the_catalogue():
    gen = _generator()
    built = gen.build(BUNDLE, gen.SOURCE_URL)
    assert built["attack_modified"] == "2026-03-04"
    assert built["copyright"] == "Copyright The MITRE Corporation."
    assert built["tactics"] == {"TA0002": {"name": "Execution", "shortname": "execution"}}
    assert built["techniques"] == {
        "T1003": {"name": "OS Credential Dumping", "tactics": []},
        "T1059.001": {"name": "PowerShell", "tactics": ["TA0002"]},
        "T1064": {"name": "Scripting", "tactics": ["TA0002"], "deprecated": True},
        "T1086": {"name": "PowerShell", "tactics": ["TA0002"], "revoked": True, "revoked_by": "T1059.001"},
    }
    assert json.loads(gen.render(built)) == built


def test_the_generator_check_mode_flags_a_stale_file(tmp_path):
    gen = _generator()
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps(BUNDLE), encoding="utf-8")
    out = tmp_path / "out.json"
    assert gen.main(["--source", str(source), "--output", str(out)]) == 0
    assert gen.main(["--source", str(source), "--output", str(out), "--check"]) == 0
    out.write_text(out.read_text(encoding="utf-8").replace("Scripting", "Scripted"), encoding="utf-8")
    assert gen.main(["--source", str(source), "--output", str(out), "--check"]) == 1
