"""Wiki edits and restores are compare-and-set on the page's revision.

Two people saving the same page at once must leave one consistent page and history.
The second save gets a 409 with the current page; it must not get a 500 or silently
overwrite the first. A restore is an edit too: it names the revision it was decided
against, so it cannot wipe out a newer edit made after the history was opened.

"Concurrent" here is deterministic: the other person's save is committed to the
database after this request has loaded the page and before it writes, which is the
window a check against the loaded page cannot close.
"""

import uuid

import pytest
from app.models_wiki import WikiPage, WikiRevision
from app.routers import wiki
from sqlalchemy import update


def _space_and_page(client, body="v1"):
    assert (
        client.post("/wiki/spaces", json={"name": "Runbooks", "slug": "runbooks", "visibility": "all"}).status_code
        == 201
    )
    resp = client.post("/wiki/spaces/runbooks/pages", json={"title": "Home", "body": body})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def someone_saves_first(db_session, monkeypatch):
    """After the handler loads the page, another editor's save of revision 2 lands."""
    real = wiki._page

    def page_then_concurrent_save(db, page_id, user):
        page, space = real(db, page_id, user)
        # Another transaction's write: it does not refresh the page this request already loaded.
        db.execute(
            update(WikiPage).where(WikiPage.id == page.id).values(body="theirs", revision_number=2),
            execution_options={"synchronize_session": False},
        )
        db.add(
            WikiRevision(
                tenant_id=page.tenant_id,
                page_id=page.id,
                revision_number=2,
                title=page.title,
                body="theirs",
                editor_id=uuid.uuid4(),
                edit_summary="the other editor",
            )
        )
        db.flush()
        monkeypatch.setattr(wiki, "_page", real)  # only the first load races
        return page, space

    monkeypatch.setattr(wiki, "_page", page_then_concurrent_save)


def _history(client, page_id):
    numbers = sorted(r["revision_number"] for r in client.get(f"/wiki/pages/{page_id}/revisions").json())
    return [(n, client.get(f"/wiki/pages/{page_id}/revisions/{n}").json()["body"]) for n in numbers]


def test_a_save_that_loses_the_race_is_409_and_history_stays_consistent(client, someone_saves_first):
    page = _space_and_page(client)
    clash = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "mine"})
    assert clash.status_code == 409, clash.text
    assert clash.json()["current"]["body"] == "theirs"
    assert client.get(f"/wiki/pages/{page['id']}").json()["body"] == "theirs"
    assert _history(client, page["id"]) == [(1, "v1"), (2, "theirs")]


def test_a_restore_that_loses_the_race_is_409(client, someone_saves_first):
    page = _space_and_page(client)
    clash = client.post(f"/wiki/pages/{page['id']}/revisions/1/restore", json={"base_revision": 1})
    assert clash.status_code == 409, clash.text
    assert clash.json()["current"]["body"] == "theirs"
    assert _history(client, page["id"]) == [(1, "v1"), (2, "theirs")]


def test_a_restore_must_name_the_revision_it_was_decided_against(client):
    page = _space_and_page(client)
    client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "v2"})
    assert client.post(f"/wiki/pages/{page['id']}/revisions/1/restore").status_code == 422
    stale = client.post(f"/wiki/pages/{page['id']}/revisions/1/restore", json={"base_revision": 1})
    assert stale.status_code == 409
    assert stale.json()["current"]["body"] == "v2"
    ok = client.post(f"/wiki/pages/{page['id']}/revisions/1/restore", json={"base_revision": 2})
    assert ok.status_code == 200
    assert (ok.json()["body"], ok.json()["revision_number"]) == ("v1", 3)
    assert _history(client, page["id"]) == [(1, "v1"), (2, "v2"), (3, "v1")]


def test_a_metadata_save_that_loses_the_race_is_409(client, someone_saves_first):
    page = _space_and_page(client)
    clash = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "tags": "vsphere"})
    assert clash.status_code == 409
    assert client.get(f"/wiki/pages/{page['id']}").json()["tags"] in ("", None)


def test_on_postgres_the_second_of_two_concurrent_saves_waits_then_loses():
    """Real row locking: B's compare-and-set blocks on A's lock, then matches no row.

    Runs against a scratch database when TEST_POSTGRES_ADMIN_URL is set (as the
    migration-chain test does); skipped otherwise.
    """
    import os
    import threading
    import time

    import sqlalchemy as sa
    from app.db import Base
    from sqlalchemy.orm import sessionmaker

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_wiki_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name))
    try:
        Base.metadata.create_all(engine)
        make = sessionmaker(engine)
        with make() as s:
            s.execute(sa.text("SET session_replication_role = replica"))  # no FK rows needed for the race
            tenant = uuid.uuid4()
            page = WikiPage(
                id=uuid.uuid4(),
                tenant_id=tenant,
                space_id=uuid.uuid4(),
                title="Home",
                slug="home",
                body="v1",
                revision_number=1,
                author_id=uuid.uuid4(),
                last_editor_id=uuid.uuid4(),
            )
            s.add(page)
            s.commit()
            page_id = page.id
        a, b = make(), make()
        page_a, page_b = a.get(WikiPage, page_id), b.get(WikiPage, page_id)
        assert wiki._claim(a, page_a, 1, bump=True)  # A holds the row lock, not yet committed
        outcome = {}

        def b_saves():
            outcome["b"] = wiki._claim(b, page_b, 1, bump=True)
            outcome["at"] = time.monotonic()

        t = threading.Thread(target=b_saves)
        t.start()
        time.sleep(0.5)
        assert "b" not in outcome, "B did not wait for A's row lock"
        committed_at = time.monotonic()
        a.commit()
        t.join(10)
        assert outcome["b"] is False and outcome["at"] >= committed_at
        b.rollback()
        with make() as s:
            assert s.get(WikiPage, page_id).revision_number == 2
        a.close()
        b.close()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
