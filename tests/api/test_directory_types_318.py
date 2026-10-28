"""OU and security-group types: the API's enums, on create and on edit (#318).

The Users page offered OU types "department" and "team" and the group type "role", which
the API refuses. The page now offers the enums (country, branch, division, unit, section,
custom; access, distribution, training, role_based). An edit is held to the same values a
create is: before, PATCH stored any string.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import acting_as
from app.models import UserRole


def _slug() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("bad", ["department", "team"])
def test_ou_types_the_page_used_to_offer_are_refused(client, bad):
    with acting_as(UserRole.admin):
        assert client.post("/directory/ous", json={"name": "X", "slug": _slug(), "ou_type": bad}).status_code == 422


@pytest.mark.parametrize("ou_type", ["country", "branch", "division", "unit", "section", "custom"])
def test_every_ou_type_the_page_offers_is_accepted_and_editable(client, ou_type):
    with acting_as(UserRole.admin):
        r = client.post("/directory/ous", json={"name": "X", "slug": _slug(), "ou_type": ou_type})
        assert r.status_code == 201, r.text
        ou_id = r.json()["id"]
        assert client.patch(f"/directory/ous/{ou_id}", json={"ou_type": "custom"}).status_code == 200
        assert client.patch(f"/directory/ous/{ou_id}", json={"ou_type": "department"}).status_code == 422


@pytest.mark.parametrize("group_type", ["access", "distribution", "training", "role_based"])
def test_every_group_type_the_page_offers_is_accepted_and_editable(client, group_type):
    with acting_as(UserRole.admin):
        r = client.post("/directory/groups", json={"name": "G", "slug": _slug(), "group_type": group_type})
        assert r.status_code == 201, r.text
        gid = r.json()["id"]
        assert client.patch(f"/directory/groups/{gid}", json={"group_type": "access"}).status_code == 200
        assert client.patch(f"/directory/groups/{gid}", json={"group_type": "role"}).status_code == 422


def test_the_group_type_role_is_refused(client):
    with acting_as(UserRole.admin):
        r = client.post("/directory/groups", json={"name": "G", "slug": _slug(), "group_type": "role"})
        assert r.status_code == 422
