"""Every YAML parse in the API goes through ``app.safe_yaml`` (security sweep M4, follow-up to PR #112).

PR #112 moved scenario create/update/validate and template validate to ``safe_yaml.load``
(no aliases, 1 MiB cap); about twenty other sites still called ``yaml.safe_load`` on
templates, scenarios, Sigma rules, course bundles and AI output. The guard below keeps a new
one from appearing; the behaviour tests show an alias bomb is refused on write paths other
than scenarios and that stored-YAML readers fall back as they do for unparseable YAML.
"""

from __future__ import annotations

import ast
from pathlib import Path

from _shared import act_as, real_tenant, real_user
from app.models import UserRole

LAUGHS = "\n".join(  # same "billion laughs" as test_safe_yaml.py
    ["a: &a [lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    + [f"{chr(98 + i)}: &{chr(98 + i)} [" + ", ".join([f"*{chr(97 + i)}"] * 9) + "]" for i in range(8)]
)

API_PKG = Path(__file__).resolve().parents[2] / "control-plane" / "api" / "app"

# PyYAML entry points that build Python objects from a document.
_LOADERS = {
    "load",
    "load_all",
    "safe_load",
    "safe_load_all",
    "full_load",
    "full_load_all",
    "unsafe_load",
    "unsafe_load_all",
    "compose",
    "compose_all",
}

# path (relative to app/) -> why it may call PyYAML directly.
ALLOWED = {
    "safe_yaml.py": "the one implementation: yaml.load with NoAliasLoader behind the size cap",
    "course_releases/lab_profile.py": (
        "byte-identical copy of tools/arc2/lab_profile.py (tests/contracts/test_lab_profile_copy.py); "
        "its load() reads a local ARC2 run file for arc2.check and is never called by the API"
    ),
}


def _direct_yaml_loads(tree: ast.AST) -> list[int]:
    """Lines calling a PyYAML loader, through ``import yaml [as x]`` or ``from yaml import ...``."""
    aliases: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "yaml"}
        elif isinstance(node, ast.ImportFrom) and node.module == "yaml":
            bare |= {a.asname or a.name for a in node.names if a.name in _LOADERS}
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        via_module = (
            isinstance(f, ast.Attribute)
            and f.attr in _LOADERS
            and isinstance(f.value, ast.Name)
            and f.value.id in aliases
        )
        if via_module or (isinstance(f, ast.Name) and f.id in bare):
            lines.append(node.lineno)
    return lines


def _scan() -> dict[str, list[int]]:
    found = {}
    for path in sorted(API_PKG.rglob("*.py")):
        lines = _direct_yaml_loads(ast.parse(path.read_text(), filename=str(path)))
        if lines:
            found[path.relative_to(API_PKG).as_posix()] = lines
    return found


def test_no_direct_pyyaml_load_outside_safe_yaml():
    offenders = {p: lines for p, lines in _scan().items() if p not in ALLOWED}
    assert not offenders, (
        f"parse YAML with app.safe_yaml.load (no aliases, size cap), not yaml.safe_load/yaml.load: {offenders}"
    )


def test_the_guard_sees_every_import_form():
    src = (
        "import yaml\nimport yaml as pyyaml\nfrom yaml import safe_load as sl\n"
        "yaml.safe_load('a')\npyyaml.load('a', Loader=None)\nsl('a')\nyaml.safe_dump({})\n"
    )
    assert _direct_yaml_loads(ast.parse(src)) == [4, 5, 6]


def test_the_allowlist_is_not_stale():
    found = _scan()
    assert set(ALLOWED) <= set(found), f"allowlisted files no longer load YAML directly: {set(ALLOWED) - set(found)}"


def test_the_api_never_calls_the_lab_profile_copys_loader():
    for path in API_PKG.rglob("*.py"):
        if path.name == "lab_profile.py":
            continue
        text = path.read_text()
        assert "lab_profile.load(" not in text and "profile_rules.load(" not in text, path


# -- behaviour: write paths other than scenarios -------------------------------------------
SIGMA = "title: t\nlogsource: {product: windows}\ndetection: {sel: {a: b}, condition: sel}\n"


def test_a_detection_rule_alias_bomb_is_422_and_not_stored(client, db_session):
    from app.models import DetectionRule

    t = real_tenant(db_session, "yaml-dr")
    act_as(real_user(db_session, UserRole.admin, t.id))
    r = client.post("/detection-rules", json={"title": "bomb", "detection_yaml": LAUGHS, "level": "high"})
    assert r.status_code == 422, r.text
    assert "aliases" in r.text
    assert db_session.query(DetectionRule).filter(DetectionRule.title == "bomb").count() == 0

    body = client.post("/detection-rules/validate", json={"yaml": LAUGHS}).json()
    assert body["valid"] is False and "aliases" in body["errors"][0]

    ok = client.post("/detection-rules", json={"title": "fine", "detection_yaml": SIGMA, "level": "high"})
    assert ok.status_code == 201, ok.text
    r = client.patch(f"/detection-rules/{ok.json()['id']}", json={"detection_yaml": LAUGHS})
    assert r.status_code == 422, r.text


def test_a_template_alias_bomb_is_422_on_create_and_update(client, db_session):
    import uuid

    from app.models import Template

    t = real_tenant(db_session, "yaml-tp")
    act_as(real_user(db_session, UserRole.admin, t.id))
    r = client.post("/templates", json={"name": "bomb", "version": "1", "yaml": LAUGHS})
    assert r.status_code == 422, r.text
    assert "aliases" in r.json()["detail"]
    assert db_session.query(Template).filter(Template.name == "bomb").count() == 0

    good = "name: fine\nassets: [{role: ws, count: 1}]\n"
    ok = client.post("/templates", json={"name": "fine", "version": "1", "yaml": good})
    assert ok.status_code == 201, ok.text
    tid = ok.json()["id"]
    r = client.put(f"/templates/{tid}", json={"yaml": LAUGHS})
    assert r.status_code == 422, r.text
    db_session.expire_all()
    assert db_session.get(Template, uuid.UUID(tid)).yaml == good


def test_unparseable_template_yaml_is_still_stored_as_before(client, db_session):
    """Only the refusals are new: plain syntax errors keep their old path (stored, validation reports them)."""
    t = real_tenant(db_session, "yaml-tp2")
    act_as(real_user(db_session, UserRole.admin, t.id))
    r = client.post("/templates", json={"name": "draft", "version": "1", "yaml": "a: [unclosed"})
    assert r.status_code == 201, r.text


def test_an_oversize_template_is_422(client, db_session):
    from app import safe_yaml

    t = real_tenant(db_session, "yaml-tp3")
    act_as(real_user(db_session, UserRole.admin, t.id))
    big = "name: big\nnotes: " + "x" * (safe_yaml.MAX_YAML_BYTES + 1) + "\n"
    r = client.post("/templates", json={"name": "big", "version": "1", "yaml": big})
    assert r.status_code == 422, r.text


# -- behaviour: readers of stored YAML fall back as they do for unparseable YAML ------------
def test_stored_yaml_readers_refuse_an_alias_bomb_quietly():
    from app import exercise_completion, range_topology, scenario_objectives
    from app.detections import credit, redaction

    assert scenario_objectives.parse(LAUGHS) == []
    assert exercise_completion.duration(LAUGHS) is None
    assert range_topology.count_template_hosts(LAUGHS) is None
    assert redaction.redact_scenario_yaml(LAUGHS) == ""
    assert credit._scenario(LAUGHS) == {}
