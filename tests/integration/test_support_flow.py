"""Integration test: the Support flow on the live stack.

A runbook goes into the wiki, a ticket cites it and is assigned to a member of staff,
and the assignment produces an in-app notification for that assignee only.

What the live stack can and cannot show. scripts/itest.sh runs the API with
AUTH_DISABLED=true, so every request is the seeded dev admin. Notifications are
private (``/notifications`` serves only the caller's own) and never go to the person
who made the change, so from this one identity the test can prove:

- the wiki page and ticket are created, persisted and linked (through Alembic's schema,
  pgbouncer and the real Postgres, not SQLite);
- the assignee is a real staff user of the tenant the ticket is visible to;
- the actor is *not* notified of their own assignment, and no notification row of
  theirs links to this ticket;
- the notifications endpoints answer on the migrated ``notifications`` table.

It cannot read the assignee's inbox: there is no impersonation, and adding one to make
a test pass would widen the auth surface. That half of the flow (assignee's inbox has
"TN-n: assigned to you" linking /support/<id>, mark-read works) is asserted in-process
by tests/api/test_support_notifications.py::TestWikiToTicketToAssignee.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture
def runbook(api_client, run_id, teardown_delete):
    """A wiki space with one published page; the space is archived afterwards."""
    slug = f"itest-support-{run_id}"
    space = api_client.post("/wiki/spaces", json={"name": f"Support runbooks {run_id}", "slug": slug})
    assert space.status_code == 201, f"POST /wiki/spaces => {space.status_code} {space.text}"
    page = api_client.post(
        f"/wiki/spaces/{slug}/pages",
        json={"title": "Rebooting dc01", "body": "1. Console in.\n2. Reboot from vCenter.", "is_published": True},
    )
    assert page.status_code == 201, f"POST /wiki/spaces/{slug}/pages => {page.status_code} {page.text}"
    yield page.json()
    teardown_delete(f"/wiki/spaces/{slug}")  # archives the space


@pytest.fixture
def assignee(api_client, run_id, teardown_delete):
    """A second member of staff in the dev tenant, to assign the ticket to."""
    resp = api_client.post(
        "/users",
        json={
            "email": f"itest-ops-{run_id}@truenorth.local",
            "display_name": f"Sgt Ops {run_id}",
            "role": "range_ops",
        },
    )
    assert resp.status_code == 201, f"POST /users => {resp.status_code} {resp.text}"
    yield resp.json()
    # After the test's own ticket delete; a refusal (the soft-deleted ticket still
    # references the user) is logged by teardown_delete, not raised.
    teardown_delete(f"/users/{resp.json()['id']}")


class TestSupportFlow:
    def test_wiki_page_then_ticket_citing_it_assigned_to_staff(self, api_client, runbook, assignee, teardown_delete):
        page_id = runbook["id"]
        assert runbook["revision_number"] == 1

        # The page reads back.
        got = api_client.get(f"/wiki/pages/{page_id}")
        assert got.status_code == 200, got.text
        assert got.json()["title"] == "Rebooting dc01"

        # The new staff member is offered as an assignee.
        offered = api_client.get("/tickets/assignees")
        assert offered.status_code == 200, offered.text
        assert assignee["id"] in {a["id"] for a in offered.json()}

        before = api_client.get("/notifications/unread-count")
        assert before.status_code == 200, before.text

        link = f"/wiki/pages/{page_id}"
        created = api_client.post(
            "/tickets",
            json={
                "subject": "dc01 won't boot after the runbook",
                "description": f"Followed [Rebooting dc01]({link}); still at the BIOS screen.",
                "type": "incident",
                "priority": "high",
                "assignee_id": assignee["id"],
            },
        )
        assert created.status_code == 201, f"POST /tickets => {created.status_code} {created.text}"
        ticket = created.json()
        try:
            assert ticket["assignee_id"] == assignee["id"]
            assert ticket["assignee_name"] == assignee["display_name"]
            assert ticket["key"] == f"TN-{ticket['number']}"
            assert ticket["status"] == "open"

            # Persisted, and the wiki link survives the round trip.
            again = api_client.get(f"/tickets/{ticket['id']}")
            assert again.status_code == 200, again.text
            assert link in again.json()["description"]
            assert again.json()["assignee_id"] == assignee["id"]

            # The actor is never told about their own change: no new unread, and
            # nothing of theirs links to this ticket.
            after = api_client.get("/notifications/unread-count")
            assert after.status_code == 200, after.text
            assert after.json()["unread"] == before.json()["unread"]
            mine = api_client.get("/notifications", params={"limit": 100})
            assert mine.status_code == 200, mine.text
            assert all(n.get("link") != f"/support/{ticket['id']}" for n in mine.json())
        finally:
            teardown_delete(f"/tickets/{ticket['id']}")

    def test_notifications_are_private(self, api_client):
        """Someone else's (here: a random) notification id is 404, never another user's row."""
        resp = api_client.post(f"/notifications/{uuid.uuid4()}/read")
        assert resp.status_code == 404, resp.text
