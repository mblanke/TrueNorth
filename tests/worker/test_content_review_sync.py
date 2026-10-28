"""The content clock's periodic sync (worker/content_review_sync.py; ADR 0014 decisions
22-24, issue #286): review rows for all content, the dependency edges the content already
declares, due dates, and staleness carried down the edges. A real SQLite database built
from the generated table mirror (tables.py).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from worker import content_review_sync as sync_mod
from worker import tables

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
TENANT, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
TABLES = ("golden_images", "templates", "scenarios", "exercise_templates", "courses", "course_modules", "module_content",
          "scheduled_events", "content_review_items", "content_review_edges", "content_review_themes",
          "content_review_cycles", "content_feed_entries", "content_feed_flags", "content_feed_sources",
          "content_feed_settings")


@pytest.fixture
def engine(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'content.db'}")
    for name in TABLES:
        getattr(tables, name).create(eng)
    return eng


def _filler(col):
    t = col.type
    if isinstance(t, sa.Boolean):
        return False
    if isinstance(t, sa.Integer):
        return 0
    if isinstance(t, sa.JSON):
        return []
    if isinstance(t, sa.DateTime):
        return NOW
    if isinstance(t, sa.Enum):
        return t.enums[0]
    if type(t).__name__ == "GUID":
        return str(uuid.uuid4())
    return ""


def ins(engine, table: str, **values) -> str:
    """Insert a row, filling every NOT NULL column the test does not care about."""
    tbl = getattr(tables, table)
    row = {c.name: _filler(c) for c in tbl.columns if not c.nullable}
    row.update(values)
    with engine.begin() as conn:
        conn.execute(tbl.insert().values(**row))
    return str(row.get("id"))


def run(engine, now=NOW):
    with Session(engine) as db:
        out = sync_mod.sync(db, now=now)
        db.commit()
    return out


def review_rows(engine) -> dict[tuple[str, str], dict]:
    def plain(row) -> dict:
        return {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in row._asdict().items()}

    with engine.connect() as conn:
        return {(r.item_type, r.item_id): plain(r) for r in conn.execute(sa.select(tables.content_review_items))}


def deps(engine) -> set[tuple[tuple[str, str], tuple[str, str]]]:
    rows = {r["id"]: (r["item_type"], r["item_id"]) for r in review_rows(engine).values()}
    with engine.connect() as conn:
        return {(rows[str(e.item_id)], rows[str(e.depends_on_id)]) for e in
                conn.execute(sa.select(tables.content_review_edges))}


def golden(engine, cid, *, aliases=(), tenant=None, hypervisor="vsphere"):
    return ins(engine, "golden_images", id=str(uuid.uuid4()), catalogue_id=cid, hypervisor=hypervisor,
               os_aliases=json.dumps([cid, *aliases]), tenant_id=tenant, deleted_at=None, role="")


def template(engine, name, nodes, *, tenant=TENANT, tags=None):
    doc = {"name": name, "nodes": nodes, **({"tags": tags} if tags else {})}
    return ins(engine, "templates", id=str(uuid.uuid4()), name=name, yaml=json.dumps(doc), tenant_id=tenant,
               deleted_at=None)


def scenario(engine, name, range_template, *, tenant=TENANT):
    return ins(engine, "scenarios", id=str(uuid.uuid4()), name=name, tenant_id=tenant, deleted_at=None,
               yaml=json.dumps({"name": name, "range_template": range_template}))


def exercise_template(engine, name, *, scenario_id=None, template_id=None, tenant=TENANT, tags=None):
    return ins(engine, "exercise_templates", id=str(uuid.uuid4()), name=name, tenant_id=tenant, deleted_at=None,
               scenario_id=scenario_id, range_template_id=template_id, tags=tags or [])


def course(engine, name, *, tenant=TENANT, tags="[]"):
    return ins(engine, "courses", id=str(uuid.uuid4()), name=name, tenant_id=tenant, tags=tags)


def module(engine, course_id, title, *, content_ref=""):
    return ins(engine, "course_modules", id=str(uuid.uuid4()), course_id=course_id, title=title,
               content_type="scenario", content_ref=content_ref)


@pytest.fixture
def chain(engine):
    """golden ubuntu-2404 <- template (a node says the old 'ubuntu-22.04') <- scenario <- module <- course."""
    g = golden(engine, "ubuntu-2404", aliases=("ubuntu-24.04",))
    win = golden(engine, "win11")
    t = template(engine, "Medium Enterprise", [{"id": "ws1", "os": "ubuntu-22.04"}, {"id": "ws2", "os": "win11"},
                                                 {"id": "sw", "type": "switch"}])
    s = scenario(engine, "SOC Analyst", "Medium Enterprise")
    c = course(engine, "C201 Network Defence")
    m = module(engine, c, "Phishing triage")
    ins(engine, "module_content", id=str(uuid.uuid4()), module_id=m, scenario_id=s, content_kind="teach")
    return {"golden": g, "win": win, "template": t, "scenario": s, "course": c, "module": m}


# ── items ────────────────────────────────────────────────────────────────────
class TestItems:
    def test_every_piece_of_content_gets_one_review_row(self, engine, chain):
        out = run(engine)
        rows = review_rows(engine)
        assert out["created"] == 6
        assert set(rows) == {("golden_template", chain["golden"]), ("golden_template", chain["win"]),
                             ("range_template", chain["template"]), ("scenario", chain["scenario"]),
                             ("module", chain["module"]), ("course", chain["course"])}
        assert rows[("course", chain["course"])]["title"] == "C201 Network Defence"
        assert rows[("course", chain["course"])]["tenant_id"] == TENANT
        assert rows[("module", chain["module"])]["tenant_id"] == TENANT  # its course's tenant
        assert rows[("golden_template", chain["golden"])]["tenant_id"] is None  # shared
        assert run(engine)["created"] == 0  # idempotent

    def test_never_reviewed_content_is_due_one_cycle_after_it_was_first_tracked(self, engine, chain):
        run(engine)
        due = review_rows(engine)[("course", chain["course"])]["due_at"]
        assert due.replace(tzinfo=UTC) == NOW + timedelta(days=90)

    def test_a_renamed_item_gets_its_new_title_and_a_deleted_one_is_marked_missing(self, engine, chain):
        run(engine)
        with engine.begin() as conn:
            conn.execute(tables.courses.update().values(name="C201 v2"))
            conn.execute(tables.scenarios.update().values(deleted_at=NOW))
        out = run(engine)
        rows = review_rows(engine)
        assert rows[("course", chain["course"])]["title"] == "C201 v2"
        assert rows[("scenario", chain["scenario"])]["missing"] is True
        assert out["missing"] == 1

    def test_a_new_course_is_owned_by_its_latest_bookings_instructor(self, engine, chain):
        early, late = str(uuid.uuid4()), str(uuid.uuid4())
        for who, start in ((early, NOW - timedelta(days=30)), (late, NOW - timedelta(days=2))):
            ins(engine, "scheduled_events", id=str(uuid.uuid4()), course_id=chain["course"], instructor_id=who,
                tenant_id=TENANT, start_time=start, end_time=start + timedelta(hours=8), state="scheduled")
        run(engine)
        rows = review_rows(engine)
        assert rows[("course", chain["course"])]["owner_id"] == late
        assert rows[("module", chain["module"])]["owner_id"] == late

    def test_tags_naming_a_theme_tag_a_new_item(self, engine):
        blue = ins(engine, "content_review_themes", id=str(uuid.uuid4()), tenant_id=None, name="Blue team",
                   colour="#3B5BDB")
        c = course(engine, "Hunt", tags=json.dumps(["blue-team", "soc"]))
        run(engine)
        assert review_rows(engine)[("course", c)]["theme_ids"] == [blue]


# ── edges ────────────────────────────────────────────────────────────────────
class TestEdges:
    def test_the_chain_is_derived_from_the_content(self, engine, chain):
        run(engine)
        got = deps(engine)
        assert got == {
            (("range_template", chain["template"]), ("golden_template", chain["golden"])),
            (("range_template", chain["template"]), ("golden_template", chain["win"])),
            (("scenario", chain["scenario"]), ("range_template", chain["template"])),
            (("module", chain["module"]), ("scenario", chain["scenario"])),
            (("course", chain["course"]), ("module", chain["module"])),
        }

    def test_a_machine_with_no_os_is_built_from_the_default_image(self, engine):
        g = golden(engine, "ubuntu-2404")
        t = template(engine, "Bare", [{"id": "a"}])
        run(engine)
        assert deps(engine) == {(("range_template", t), ("golden_template", g))}

    def test_legacy_content_ref_and_bookings_are_edges_and_cancelled_bookings_are_not(self, engine, chain):
        c2 = course(engine, "C101")
        m2 = module(engine, c2, "Legacy", content_ref=json.dumps({"scenario_id": chain["scenario"]}))
        t2 = template(engine, "Small Enterprise", [])
        t3 = template(engine, "Cancelled", [])
        ins(engine, "scheduled_events", id=str(uuid.uuid4()), course_id=c2, template_id=t2, tenant_id=TENANT,
            start_time=NOW, end_time=NOW, state="scheduled")
        ins(engine, "scheduled_events", id=str(uuid.uuid4()), course_id=c2, template_id=t3, tenant_id=TENANT,
            start_time=NOW, end_time=NOW, state="cancelled")
        run(engine)
        got = deps(engine)
        assert (("module", m2), ("scenario", chain["scenario"])) in got
        assert (("course", c2), ("range_template", t2)) in got
        assert (("course", c2), ("range_template", t3)) not in got

    def test_edges_never_reach_another_tenants_content(self, engine):
        theirs_golden = golden(engine, "kali", tenant=OTHER)
        their_template = template(engine, "Theirs", [], tenant=OTHER)
        mine = template(engine, "Mine", [{"id": "k", "os": "kali"}])
        s = scenario(engine, "Mine", "Theirs")
        run(engine)
        got = deps(engine)
        assert (("range_template", mine), ("golden_template", theirs_golden)) not in got
        assert (("scenario", s), ("range_template", their_template)) not in got

    def test_another_tenants_golden_under_one_spelling_does_not_hide_the_shared_one(self, engine):
        # OTHER's golden template answers to the current spelling; the shared one only to the old.
        golden(engine, "ubuntu-2404", tenant=OTHER)
        shared = golden(engine, "ubuntu-old", aliases=("ubuntu-22.04",), hypervisor="vsphere")
        mine = template(engine, "Mine", [{"id": "a", "os": "ubuntu-22.04"}])
        run(engine)
        assert (("range_template", mine), ("golden_template", shared)) in deps(engine)

    def test_a_shared_course_takes_no_tenants_instructor_as_owner(self, engine):
        c = course(engine, "Shared", tenant=None)
        ins(engine, "scheduled_events", id=str(uuid.uuid4()), course_id=c, instructor_id=str(uuid.uuid4()),
            tenant_id=TENANT, start_time=NOW, end_time=NOW, state="scheduled")
        run(engine)
        assert review_rows(engine)[("course", c)]["owner_id"] is None

    def test_a_scenario_prefers_its_own_tenants_template_over_a_shared_one(self, engine):
        shared = template(engine, "Lab", [], tenant=None)
        own = template(engine, "Lab", [])
        s = scenario(engine, "S", "Lab")
        other = scenario(engine, "S2", "Lab", tenant=OTHER)
        run(engine)
        got = deps(engine)
        assert (("scenario", s), ("range_template", own)) in got
        assert (("scenario", s), ("range_template", shared)) not in got
        assert (("scenario", other), ("range_template", shared)) in got

    def test_unchanged_edges_are_left_alone(self, engine, chain):
        """Only the difference is written: an edge that is still there keeps its row."""
        run(engine)
        with engine.connect() as conn:
            before = {(str(e.item_id), str(e.depends_on_id)): str(e.id)
                      for e in conn.execute(sa.select(tables.content_review_edges))}
        run(engine)
        with engine.connect() as conn:
            after = {(str(e.item_id), str(e.depends_on_id)): str(e.id)
                     for e in conn.execute(sa.select(tables.content_review_edges))}
        assert after == before

    def test_edges_are_replaced_when_the_content_changes(self, engine, chain):
        run(engine)
        with engine.begin() as conn:
            conn.execute(tables.templates.update().values(yaml=json.dumps({"nodes": [{"id": "w", "os": "win11"}]})))
        run(engine)
        got = deps(engine)
        assert (("range_template", chain["template"]), ("golden_template", chain["golden"])) not in got
        assert (("range_template", chain["template"]), ("golden_template", chain["win"])) in got


# ── the clock and propagation ────────────────────────────────────────────────
def _flag(engine, key, text="New Ubuntu 24.04 point release"):
    rows = review_rows(engine)
    rid = rows[key]["id"]
    reason = {"origin": rid, "kind": "release", "text": text, "source": "feed:test", "at": NOW.isoformat()}
    with engine.begin() as conn:
        conn.execute(tables.content_review_items.update().where(tables.content_review_items.c.id == rid)
                     .values(stale_reasons=[reason], stale=True))


class TestExerciseTemplates:
    """ADR 0017: an exercise template is on the content clock and depends on what it names."""

    def test_it_gets_a_row_and_depends_on_its_scenario_and_range_template(self, engine, chain):
        et = exercise_template(engine, "IR drill", scenario_id=chain["scenario"], template_id=chain["template"])
        out = run(engine)
        assert out["created"] == 7
        rows = review_rows(engine)
        assert rows[("exercise_template", et)]["title"] == "IR drill"
        assert rows[("exercise_template", et)]["tenant_id"] == TENANT
        got = deps(engine)
        assert (("exercise_template", et), ("scenario", chain["scenario"])) in got
        assert (("exercise_template", et), ("range_template", chain["template"])) in got

    def test_a_stale_scenario_makes_the_template_stale(self, engine, chain):
        et = exercise_template(engine, "IR drill", scenario_id=chain["scenario"], template_id=chain["template"])
        run(engine)
        _flag(engine, ("scenario", chain["scenario"]))
        run(engine)
        rows = review_rows(engine)
        assert rows[("scenario", chain["scenario"])]["stale"] is True
        assert rows[("exercise_template", et)]["stale"] is True
        assert rows[("exercise_template", et)]["stale_reasons"][0]["text"].startswith("Its scenario")

    def test_it_never_depends_on_another_tenants_content(self, engine):
        theirs_t = template(engine, "Theirs", [], tenant=OTHER)
        theirs_s = scenario(engine, "Theirs", "Theirs", tenant=OTHER)
        et = exercise_template(engine, "Mine", scenario_id=theirs_s, template_id=theirs_t)
        run(engine)
        got = deps(engine)
        assert (("exercise_template", et), ("scenario", theirs_s)) not in got
        assert (("exercise_template", et), ("range_template", theirs_t)) not in got

    def test_a_deleted_template_is_marked_missing_and_tags_name_a_theme(self, engine):
        ins(engine, "content_review_themes", id=str(uuid.uuid4()), tenant_id=None, name="DFIR", colour="#112233")
        et = exercise_template(engine, "IR drill", tags=["DFIR"])
        run(engine)
        assert review_rows(engine)[("exercise_template", et)]["theme_ids"]
        with engine.begin() as conn:
            conn.execute(tables.exercise_templates.update().values(deleted_at=NOW))
        run(engine)
        assert review_rows(engine)[("exercise_template", et)]["missing"] is True


class TestClock:
    def test_a_stale_golden_template_makes_the_course_stale(self, engine, chain):
        run(engine)
        _flag(engine, ("golden_template", chain["golden"]))
        run(engine)
        rows = review_rows(engine)
        course_row = rows[("course", chain["course"])]
        assert course_row["stale"] is True
        assert course_row["stale_reasons"][0]["text"] == \
            "Its golden template ubuntu-2404 is stale (through module Phishing triage)"
        assert rows[("range_template", chain["template"])]["stale_reasons"][0]["text"] == \
            "Its golden template ubuntu-2404 is stale"
        assert rows[("golden_template", chain["win"])]["stale"] is False

    def test_a_retired_item_makes_nothing_stale_downstream(self, engine, chain):
        run(engine)
        _flag(engine, ("range_template", chain["template"]))
        run(engine)
        assert review_rows(engine)[("course", chain["course"])]["stale"] is True
        with engine.begin() as conn:  # retired (the API also clears downstream at once)
            conn.execute(tables.content_review_items.update()
                         .where(tables.content_review_items.c.item_type == "range_template").values(retired=True))
        run(engine)
        rows = review_rows(engine)
        assert rows[("course", chain["course"])]["stale"] is False
        assert rows[("scenario", chain["scenario"])]["stale"] is False

    def test_an_overdue_review_is_flagged_and_does_not_spread(self, engine, chain):
        run(engine)
        later = NOW + timedelta(days=95)
        with engine.begin() as conn:  # only the golden template was reviewed, 100 days before `later`
            conn.execute(tables.content_review_items.update()
                         .where(tables.content_review_items.c.item_type != "golden_template")
                         .values(last_reviewed_at=later - timedelta(days=10)))
        run(engine, now=later)
        rows = review_rows(engine)
        g = rows[("golden_template", chain["golden"])]
        assert g["stale"] is True and g["stale_reasons"][0]["text"] == "Review overdue by 5 days"
        assert rows[("range_template", chain["template"])]["stale"] is False

    def test_the_cycle_per_content_type_moves_the_due_date(self, engine, chain):
        ins(engine, "content_review_cycles", id=str(uuid.uuid4()), tenant_id=TENANT, item_type="course", days=120)
        run(engine)
        rows = review_rows(engine)
        assert rows[("course", chain["course"])]["due_at"].replace(tzinfo=UTC) == NOW + timedelta(days=120)
        assert rows[("module", chain["module"])]["due_at"].replace(tzinfo=UTC) == NOW + timedelta(days=90)

    def test_a_cycle_in_the_edges_is_safe(self, engine, chain):
        run(engine)
        rows = review_rows(engine)
        with engine.begin() as conn:  # a hand-made edge closes the loop: golden depends on the course
            conn.execute(tables.content_review_edges.insert().values(
                id=str(uuid.uuid4()), item_id=rows[("golden_template", chain["golden"])]["id"],
                depends_on_id=rows[("course", chain["course"])]["id"], derived=False))
        _flag(engine, ("golden_template", chain["golden"]))
        run(engine)
        rows = review_rows(engine)
        assert len(rows[("golden_template", chain["golden"])]["stale_reasons"]) == 1
        assert len(rows[("course", chain["course"])]["stale_reasons"]) == 1

    def test_a_review_made_during_the_sync_is_not_overwritten(self, engine, chain, monkeypatch):
        run(engine)
        _flag(engine, ("golden_template", chain["golden"]))
        tpl_id = review_rows(engine)[("range_template", chain["template"])]["id"]
        real = sync_mod.st.Graph.repropagate
        db = Session(engine)

        def compute_while_someone_reviews(graph, **kw):
            # The API's Mark reviewed bumps the revision after the sync read the row (SQLite has
            # one writer, so it is written through the sync's own connection here).
            db.execute(tables.content_review_items.update().where(tables.content_review_items.c.id == tpl_id)
                       .values(revision=tables.content_review_items.c.revision + 7))
            return real(graph, **kw)

        monkeypatch.setattr(sync_mod.st.Graph, "repropagate", compute_while_someone_reviews)
        with db:
            out = sync_mod.sync(db, now=NOW)
            db.commit()
        assert out["skipped"] >= 1
        assert review_rows(engine)[("range_template", chain["template"])]["stale"] is False  # not written


# ── the task and its lock ────────────────────────────────────────────────────
class TestTask:
    @pytest.fixture
    def wired(self, engine, fake_redis, monkeypatch):
        @contextmanager
        def session():
            with Session(engine) as db:
                yield db
                db.commit()

        monkeypatch.setattr(sync_mod, "db_session", session)
        monkeypatch.setattr(sync_mod, "redis_client", lambda: fake_redis)
        return fake_redis

    def test_the_task_runs_the_sync_under_the_lock(self, engine, chain, wired):
        out = sync_mod.sync_content_review.run()
        assert out["status"] == "ok" and out["created"] == 6
        assert sync_mod.LOCK_KEY not in wired.data  # released

    def test_a_second_sync_while_one_runs_does_nothing(self, engine, chain, wired):
        wired.set(sync_mod.LOCK_KEY, "someone-else", nx=True, px=60_000)
        out = sync_mod.sync_content_review.run()
        assert out["status"] == "skipped"
        assert review_rows(engine) == {}

    def test_a_lost_lock_rolls_the_run_back(self, engine, chain, wired, monkeypatch):
        """Another sync may be running once the lock is lost: write nothing, not half a run."""
        real_graph, real_lock, locks = sync_mod.st.Graph.repropagate, sync_mod.sync_lock, []

        def keep_the_lock(client=None):
            locks.append(real_lock(client))
            return locks[-1]

        def compute_and_lose_the_lock(graph, **kw):
            # By now the new rows and edges are written (uncommitted).
            wired.data.clear()  # the key expired: the next renewal finds it gone
            locks[0].renew()
            return real_graph(graph, **kw)

        monkeypatch.setattr(sync_mod, "sync_lock", keep_the_lock)
        monkeypatch.setattr(sync_mod.st.Graph, "repropagate", compute_and_lose_the_lock)
        out = sync_mod.sync_content_review.run()
        assert out["status"] == "lock_lost"
        assert review_rows(engine) == {}

    def test_sync_raises_when_the_lock_is_lost(self, engine, chain):
        import threading

        lost = threading.Event()
        lost.set()
        with Session(engine) as db, pytest.raises(sync_mod.LockLostError):
            sync_mod.sync(db, now=NOW, lost=lost)

    def test_no_redis_no_sync(self, engine, chain, wired):
        wired.down = True
        assert sync_mod.sync_content_review.run()["status"] == "skipped"

    def test_the_beat_runs_it_hourly(self):
        from worker.celery_app import app

        entry = app.conf.beat_schedule["content-review-sync"]
        assert entry["task"] == sync_mod.TASK and entry["options"]["expires"] == 3600
