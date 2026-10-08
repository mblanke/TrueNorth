"""YAML from users is parsed without aliases and under a size cap (security sweep M4).

``engine_bridge.validate_yaml`` (POST /scenarios/validate, /templates/validate; a Student
holds scenario:read) used ``yaml.safe_load``, which expands aliases: a "billion laughs"
document of a few hundred bytes becomes gigabytes when the result is walked by the schema
validator and serialised back as ``normalized``.
"""

from __future__ import annotations

import uuid

import pytest
import yaml
from _shared import act_as, real_tenant, real_user
from app import safe_yaml
from app.models import UserRole

LAUGHS = "\n".join(
    ["a: &a [lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    + [f"{chr(98 + i)}: &{chr(98 + i)} [" + ", ".join([f"*{chr(97 + i)}"] * 9) + "]" for i in range(8)]
)


def test_a_billion_laughs_is_refused_not_expanded():
    with pytest.raises(yaml.YAMLError, match="aliases"):
        safe_yaml.load(LAUGHS)


def test_an_oversize_document_is_refused():
    with pytest.raises(safe_yaml.YamlTooLargeError):
        safe_yaml.load("a: " + "x" * 100, max_bytes=50)


def test_ordinary_yaml_still_parses():
    assert safe_yaml.load("name: s\nobjectives: [{id: o}]\n") == {"name": "s", "objectives": [{"id": "o"}]}


@pytest.mark.parametrize("path", ["/scenarios/validate", "/templates/validate"])
def test_the_validate_endpoints_refuse_aliases(client, db_session, path):
    t = real_tenant(db_session, "yaml")
    act_as(real_user(db_session, UserRole.instructor, t.id))
    r = client.post(path, json={"yaml": LAUGHS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False and body["normalized"] is None
    assert "aliases" in body["errors"][0]["message"]


def test_a_student_cannot_use_validate_to_expand_aliases(client, db_session):
    t = real_tenant(db_session, "yaml-s")
    act_as(real_user(db_session, UserRole.student, t.id))
    body = client.post("/scenarios/validate", json={"yaml": LAUGHS}).json()
    assert body["valid"] is False and body["normalized"] is None


def test_arc2_run_files_share_the_loader():
    from app.routers import arc2_studio

    assert not hasattr(arc2_studio, "_NoAliasLoader")  # one implementation, in app/safe_yaml.py


def test_scenario_create_and_update_refuse_an_alias_bomb_with_422(client, db_session):
    """Create and update parsed with yaml.safe_load (PR #112 review: M4 gap)."""
    from app.models import Scenario

    t = real_tenant(db_session, "yaml-sc")
    act_as(real_user(db_session, UserRole.admin, t.id))

    r = client.post("/scenarios", json={"name": "bomb", "version": "1", "yaml": LAUGHS})
    assert r.status_code == 422, r.text
    assert "aliases" in r.json()["detail"]
    assert db_session.query(Scenario).filter(Scenario.name == "bomb").count() == 0

    ok = client.post("/scenarios", json={"name": "fine", "version": "1", "yaml": "name: fine\ntimeline: []\n"})
    assert ok.status_code == 201, ok.text
    sid = ok.json()["id"]
    r = client.put(f"/scenarios/{sid}", json={"yaml": LAUGHS})
    assert r.status_code == 422, r.text
    assert "aliases" in r.json()["detail"]
    assert db_session.get(Scenario, uuid.UUID(sid)).yaml == "name: fine\ntimeline: []\n"
