"""Tenant isolation — the leak nothing else would catch.

`tenant_id` appears throughout the models, but a query that simply forgets to filter on
it returns the wrong tenant's rows and every other test still passes: the data looks
well-formed, the status code is 200, and the shape is right. Only a test that
deliberately seeds a SECOND tenant can tell the difference.

These tests seed a foreign tenant's rows directly through the session, then assert the
API — which in dev mode authenticates as a fixed tenant — cannot see or reach them.
"""

import uuid

import pytest

# The identity `get_current_user` returns when AUTH_DISABLED is set.
DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _range(db, tenant_id: str, name: str):
    from app.models import Range

    r = Range(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        name=name,
        template_id=uuid.uuid4(),
    )
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def two_tenants(db_session):
    """One range for us, one for a tenant we must never see."""
    mine = _range(db_session, DEV_TENANT, "mine-visible")
    theirs = _range(db_session, OTHER_TENANT, "theirs-must-not-leak")
    db_session.commit()
    return mine, theirs


class TestRangeTenantIsolation:
    def test_list_excludes_other_tenants(self, client, two_tenants):
        mine, theirs = two_tenants
        body = client.get("/ranges").json()
        names = {r["name"] for r in body}
        assert "mine-visible" in names, "own tenant's range should be listed"
        assert "theirs-must-not-leak" not in names, (
            "LEAK: another tenant's range appeared in /ranges"
        )
        returned = {r["id"] for r in body}
        assert str(theirs.id) not in returned

    def test_direct_fetch_of_a_foreign_range_is_not_readable(self, client, two_tenants):
        """Guessing an id must not be enough. 404 or 403 — never 200."""
        _, theirs = two_tenants
        resp = client.get(f"/ranges/{theirs.id}")
        assert resp.status_code != 200, (
            f"LEAK: fetched another tenant's range directly "
            f"(status {resp.status_code}, body {resp.text[:200]})"
        )
        assert resp.status_code in (403, 404)

    def test_stats_do_not_count_other_tenants(self, client, two_tenants):
        """Aggregates leak too — a count is still disclosure."""
        resp = client.get("/ranges/stats")
        if resp.status_code != 200:
            pytest.skip(f"/ranges/stats unavailable ({resp.status_code})")
        total = resp.json().get("total_ranges")
        assert total == 1, (
            f"LEAK: stats counted {total} ranges; only 1 belongs to this tenant"
        )
