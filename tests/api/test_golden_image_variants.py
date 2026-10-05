"""Custom variant images (infra/vsphere/packer/variants/) in the golden-image registry.

`build.sh` registers each built variant through ``POST /golden-images``. These cover:
- create (201) and idempotent upsert (200) keyed on (catalogue_id, hypervisor);
- infra:write gating, with the same roles as the PATCH route;
- a variant never takes over a catalogue image's slot (409);
- the variant resolves by its name and its aliases, through the API, the designer's
  alias map, and the worker's resolver;
- re-importing the catalogue CSV neither deletes nor overwrites a variant.
"""

import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import GoldenImage, UserRole
from worker.render import golden_image_resolver

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
CATALOGUE = Path(__file__).resolve().parents[2] / "content" / "catalogue" / "vm_iso_catalogue.csv"

VARIANT = {
    "catalogue_id": "win11-analyst",
    "os_family": "windows",
    "version": "11 24H2",
    "role": "Windows 11 analyst workstation",
    "hypervisor": "vsphere",
    "template_name": "win11-analyst",
    "os_aliases": ["windows-11-analyst"],
    "build_status": "built",
    "enabled": True,
}


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=DEV_TENANT,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _import_catalogue(client, csv_text: str) -> dict:
    r = client.post(
        "/golden-images/import-catalogue?hypervisor=vsphere",
        files={"file": ("vm_iso_catalogue.csv", csv_text.encode(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_create_then_upsert(client, db_session):
    with acting_as(UserRole.range_ops):
        r = client.post("/golden-images", json=VARIANT)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["catalogue_id"] == "win11-analyst"
        assert body["build_status"] == "built" and body["enabled"] is True
        # Its own name always resolves, so it is in the alias list.
        assert body["os_aliases"] == ["win11-analyst", "windows-11-analyst"]
        assert body["notes"].startswith("origin=variant")

        again = dict(VARIANT, version="11 25H2", os_aliases=["analyst-ws"], build_status="building")
        r2 = client.post("/golden-images", json=again)
        assert r2.status_code == 200, r2.text
        assert r2.json()["id"] == body["id"]
        assert r2.json()["version"] == "11 25H2"
        assert r2.json()["build_status"] == "building"
        assert r2.json()["os_aliases"] == ["analyst-ws", "win11-analyst"]

    rows = db_session.query(GoldenImage).filter_by(catalogue_id="win11-analyst").all()
    assert len(rows) == 1


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.student, UserRole.observer])
def test_post_requires_infra_write(client, db_session, role):
    with acting_as(role):
        r = client.post("/golden-images", json=VARIANT)
    assert r.status_code == 403, r.text
    assert db_session.query(GoldenImage).filter_by(catalogue_id="win11-analyst").count() == 0


@pytest.mark.parametrize("role", [UserRole.admin, UserRole.range_ops])
def test_post_allowed_for_the_patch_roles(client, role):
    with acting_as(role):
        assert client.post("/golden-images", json=VARIANT).status_code == 201


@pytest.mark.parametrize("bad", [
    {"catalogue_id": "Win11 Analyst"},
    {"catalogue_id": "x"},
    {"os_family": "bsd"},
    {"build_status": "done"},
    {"os_aliases": ["has space"]},
])
def test_invalid_bodies_are_422(client, bad):
    assert client.post("/golden-images", json={**VARIANT, **bad}).status_code == 422


def test_variant_cannot_take_a_catalogue_slot(client):
    _import_catalogue(client, CATALOGUE.read_text(encoding="utf-8"))
    r = client.post("/golden-images", json=dict(VARIANT, catalogue_id="win10-22h2"))
    assert r.status_code == 409, r.text


def test_variant_resolves_by_name_and_alias(client, db_session):
    assert client.post("/golden-images", json=VARIANT).status_code == 201

    for alias in ("win11-analyst", "windows-11-analyst"):
        r = client.get("/golden-images/resolve", params={"os": alias, "hypervisor": "vsphere"})
        assert r.status_code == 200, r.text
        assert r.json()["template_name"] == "win11-analyst"

    # The designer's OS picker is built from this map.
    amap = client.get("/golden-images/alias-map").json()["map"]
    assert amap["win11-analyst"] == "win11-analyst"
    assert amap["windows-11-analyst"] == "win11-analyst"

    # And the worker resolves it at provision time.
    resolve = golden_image_resolver(db_session, "vsphere")
    assert resolve("win11-analyst") == "win11-analyst"


def test_disabled_variant_is_not_offered(client):
    assert client.post("/golden-images", json=dict(VARIANT, enabled=False)).status_code == 201
    r = client.get("/golden-images/resolve", params={"os": "win11-analyst"})
    assert r.status_code == 404
    assert "win11-analyst" not in client.get("/golden-images/alias-map").json()["map"]


def test_catalogue_reimport_keeps_the_variant(client, db_session):
    csv_text = CATALOGUE.read_text(encoding="utf-8")
    _import_catalogue(client, csv_text)
    created = client.post("/golden-images", json=VARIANT).json()

    _import_catalogue(client, csv_text)
    # A CSV that (wrongly) lists the variant's id must not overwrite it either.
    header = csv_text.splitlines()[0]
    colliding = header + "\nwin11-analyst,linux,0,clobber,vsphere,x.iso,x,no,no,1,1,low,no,-,-,-,none,clobbered\n"
    stats = _import_catalogue(client, colliding)
    assert stats["skipped_variants"] == 1

    db_session.expire_all()
    img = db_session.query(GoldenImage).filter_by(catalogue_id="win11-analyst", hypervisor="vsphere").one()
    assert str(img.id) == created["id"]
    assert img.os_family == "windows" and img.role == "Windows 11 analyst workstation"
    assert img.enabled is True and img.build_status == "built"
    assert img.notes.startswith("origin=variant")
    r = client.get("/golden-images/resolve", params={"os": "windows-11-analyst"})
    assert r.status_code == 200 and r.json()["template_name"] == "win11-analyst"
