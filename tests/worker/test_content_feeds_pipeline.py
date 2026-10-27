"""The content feeds' pipeline in the worker (ADR 0014 decision 25, issue #287): the daily
pull stores entries and each source's status; the content clock's sync turns them into
stale flags on the golden templates they name, carried down to what uses them, each once.

A real SQLite database built from the generated table mirror (tables.py); the feeds are
recorded payloads behind a fake ``FeedHttp`` (no network).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from worker import celery_app, content_review_sync
from worker import tables as t
from worker.content_feeds import catalogue, match, pull
from worker.content_review_runtime import staleness as st
from worker.feed_backends import FeedProduct, FeedUnreachableError

FIX = Path(__file__).parent / "content_feeds_fixtures"
TRACKED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # when the content was first tracked
NOW = datetime(2026, 10, 27, 4, 10, tzinfo=UTC)
TENANT = str(uuid.uuid4())
TABLES = ("golden_images", "templates", "scenarios", "courses", "course_modules", "module_content",
          "scheduled_events", "content_review_items", "content_review_edges", "content_review_themes",
          "content_review_cycles", "content_feed_entries", "content_feed_flags", "content_feed_sources")
PRODUCTS = catalogue.load(FIX / "products.yaml")
NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def fixture(name: str):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer


def nvd(params):
    """Windows Server 2022's recorded CVEs; nothing for the other products."""
    if params["virtualMatchString"] == "cpe:2.3:o:microsoft:windows_server_2022":
        return fixture("nvd_windows_server_2022.json")
    return {"totalResults": 0, "vulnerabilities": []}


@pytest.fixture(autouse=True)
def _no_rate_limit_waits(monkeypatch):
    from worker.feed_backends import nvd as nvd_mod

    monkeypatch.setattr(nvd_mod, "_pause", lambda seconds: None)
    monkeypatch.setenv("TN_FEEDS_MODE", "online")


def feeds(**over):
    return {"https://endoflife.date/api/ubuntu.json": fixture("endoflife_ubuntu.json"),
            "https://endoflife.date/api/windows-server.json": fixture("endoflife_windows_server.json"),
            "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json":
                fixture("cisa_kev.json"),
            NVD: nvd, **over}


@pytest.fixture
def engine(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'feeds.db'}")
    for name in TABLES:
        getattr(t, name).create(eng)
    return eng


@pytest.fixture
def session(engine):
    @contextmanager
    def factory():
        with Session(engine) as db:
            yield db
            db.commit()

    return factory


def _filler(col):
    typ = col.type
    if isinstance(typ, sa.Boolean):
        return False
    if isinstance(typ, sa.Integer):
        return 0
    if isinstance(typ, sa.JSON):
        return []
    if isinstance(typ, sa.DateTime):
        return TRACKED
    if type(typ).__name__ == "GUID":
        return str(uuid.uuid4())
    return ""


def ins(engine, table: str, **values) -> str:
    tbl = getattr(t, table)
    row = {c.name: _filler(c) for c in tbl.columns if not c.nullable}
    row.update(values)
    with engine.begin() as conn:
        conn.execute(tbl.insert().values(**row))
    return str(row.get("id"))


def golden(engine, cid):
    return ins(engine, "golden_images", id=str(uuid.uuid4()), catalogue_id=cid, hypervisor="vsphere",
               os_aliases=json.dumps([cid]), tenant_id=None, deleted_at=None, role="")


def sync(engine, now=NOW):
    with Session(engine) as db:
        out = content_review_sync.sync(db, now=now, feed_products=PRODUCTS)
        db.commit()
    return out


def rows(engine) -> dict[tuple[str, str], dict]:
    with engine.connect() as conn:
        return {(r.item_type, str(r.item_id)): r._asdict() for r in conn.execute(sa.select(t.content_review_items))}


def reasons(engine, key) -> list[dict]:
    return [r for r in rows(engine)[key]["stale_reasons"]]


def do_pull(session, http, sources=("endoflife", "cisa_kev", "nvd"), after=None):
    queued = []
    out = pull.pull(now=NOW, http=http, sources=sources, products=PRODUCTS, session=session,
                    after=after or (lambda: queued.append(1)))
    return out, queued


@pytest.fixture
def estate(engine):
    """Three golden templates (tracked since 1 October) and a range template on the Exchange one."""
    ids = {"noble": golden(engine, "ubuntu-lts"), "focal": golden(engine, "ubuntu-old"),
           "exchange": golden(engine, "srv2022-exchange2019")}
    doc = {"name": "Large Enterprise", "nodes": [{"id": "mail", "os": "srv2022-exchange2019"}]}
    ids["template"] = ins(engine, "templates", id=str(uuid.uuid4()), name="Large Enterprise", yaml=json.dumps(doc),
                          tenant_id=TENANT, deleted_at=None)
    sync(engine, now=TRACKED)  # first tracked: review rows, edges, nothing stale
    assert not any(r["stale"] for r in rows(engine).values())
    return ids


# ── the pull ─────────────────────────────────────────────────────────────────
class TestPull:
    def test_it_stores_every_sources_entries_and_status_then_queues_the_sync(self, engine, session):
        out, queued = do_pull(session, FakeHttp(feeds()))
        assert out["status"] == "ok" and queued == [1]
        with engine.connect() as conn:
            keys = {r.entry_key: r for r in conn.execute(sa.select(t.content_feed_entries))}
            status = {r.source: r for r in conn.execute(sa.select(t.content_feed_sources))}
        assert {"eol:ubuntu:20.04", "release:ubuntu:24.04:24.04.6", "kev:CVE-2026-21001",
                "nvd:CVE-2026-30100"} <= set(keys)
        assert keys["kev:CVE-2026-21001"].origin == "online"
        assert set(status) == {"endoflife", "cisa_kev", "nvd"}
        assert all(s.last_success_at is not None and s.last_error == "" for s in status.values())
        assert status["nvd"].cursor == NOW.isoformat()

    def test_a_second_pull_adds_nothing_new_and_queues_nothing(self, session):
        do_pull(session, FakeHttp(feeds()))
        out, queued = do_pull(session, FakeHttp(feeds()))
        assert queued == [] and all(s.get("new") == 0 for s in out["sources"].values())

    def test_a_failed_source_is_recorded_and_the_others_still_run(self, engine, session):
        broken = feeds(**{"https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json":
                          FeedUnreachableError("www.cisa.gov could not be reached (ConnectTimeout)")})
        out, _ = do_pull(session, FakeHttp(broken))
        assert out["status"] == "partial"
        with engine.connect() as conn:
            status = {r.source: r for r in conn.execute(sa.select(t.content_feed_sources))}
        assert status["cisa_kev"].last_error == "www.cisa.gov could not be reached (ConnectTimeout)"
        assert status["cisa_kev"].last_success_at is None
        assert status["nvd"].last_success_at is not None

    def test_the_next_nvd_pull_starts_where_the_last_ended(self, session):
        do_pull(session, FakeHttp(feeds()), sources=("nvd",))
        http = FakeHttp(feeds())
        pull.pull(now=NOW + timedelta(days=1), http=http, sources=("nvd",), products=PRODUCTS, session=session,
                  after=lambda: None)
        assert http.calls[0][1]["lastModStartDate"] == NOW.strftime("%Y-%m-%dT%H:%M:%S.000")

    def test_only_public_product_names_leave_the_site(self, session, estate):
        http = FakeHttp(feeds())
        do_pull(session, http)
        sent = json.dumps([[c[0], c[1]] for c in http.calls])
        assert "Large Enterprise" not in sent and TENANT not in sent

    def test_the_task_does_nothing_unless_feeds_are_online(self, monkeypatch):
        called = []
        monkeypatch.setattr(pull, "pull", lambda **kw: called.append(1) or {"status": "ok"})
        for mode in ("", "off", "bundle", "nonsense"):
            monkeypatch.setenv("TN_FEEDS_MODE", mode)
            assert pull.pull_content_feeds.run()["status"] == "skipped"
        assert called == []
        monkeypatch.setenv("TN_FEEDS_MODE", "online")
        assert pull.pull_content_feeds.run() == {"status": "ok"}

    def test_the_pull_uses_the_site_proxy(self, monkeypatch, session):
        made = []

        class Http(FakeHttp):
            def __init__(self, *, proxy=None):
                made.append(proxy)
                super().__init__(feeds())

            def close(self):
                pass

        monkeypatch.setattr(pull, "FeedHttp", Http)
        monkeypatch.setenv("TN_FEEDS_HTTP_PROXY", "http://tn-mgmt01.site:3128")
        pull.pull(now=NOW, sources=("endoflife",), products=PRODUCTS, session=session, after=lambda: None)
        assert made == ["http://tn-mgmt01.site:3128"]


class TestSchedule:
    def test_the_daily_pull_is_on_the_beat_and_routed_to_the_worker_with_egress(self):
        entry = celery_app.app.conf.beat_schedule["content-feeds-pull-daily"]
        assert entry["task"] == pull.TASK == "worker.content_feeds.pull_content_feeds"
        assert 0 < entry["options"]["expires"] <= 24 * 3600
        assert celery_app.app.amqp.router.route({}, pull.TASK)["queue"].name == "destroy"
        assert pull.TASK in celery_app.app.tasks


# ── the sync turns entries into flags ────────────────────────────────────────
class TestFlags:
    def test_feed_entries_flag_the_golden_templates_they_name(self, engine, session, estate):
        do_pull(session, FakeHttp(feeds()))
        out = sync(engine)
        focal = {r["text"] for r in reasons(engine, ("golden_template", estate["focal"]))}
        noble = {r["text"] for r in reasons(engine, ("golden_template", estate["noble"]))}
        exchange = reasons(engine, ("golden_template", estate["exchange"]))
        assert focal == {"Ubuntu 20.04 reached EOL 2025-05-31"}
        assert noble == {"Ubuntu 24.04: 24.04.6 released (the template is built from 24.04.5)"}
        texts = {r["text"] for r in exchange}
        assert "CVE-2026-21001 in CISA KEV (Exchange Server 2019): Microsoft Exchange Server Remote Code " \
               "Execution Vulnerability" in texts
        assert any(x.startswith("CVE-2026-20777 in CISA KEV (Windows Server 2022)") for x in texts)
        assert "CVE-2026-30100 in NVD, CVSS 9.8 (Windows Server 2022)" in texts
        # older than the template's tracking, or not yet past: not flagged
        assert not any("CVE-2019-0708" in x for x in texts)
        assert not any("EOL" in x for x in texts)  # Server 2022's end of life is in 2031
        kinds = {r["source"]: r["kind"] for r in exchange}
        assert kinds["kev:CVE-2026-21001"] == st.KIND_VULNERABILITY
        assert {r["kind"] for r in reasons(engine, ("golden_template", estate["focal"]))} == {st.KIND_RELEASE}
        assert out["feed_flags"] == 1 + 1 + len(exchange)

    def test_the_flag_flows_down_to_what_uses_the_template(self, engine, session, estate):
        do_pull(session, FakeHttp(feeds()))
        sync(engine)
        tmpl = reasons(engine, ("range_template", estate["template"]))
        assert tmpl and {r["kind"] for r in tmpl} == {st.KIND_UPSTREAM}
        assert tmpl[0]["text"].startswith("Its golden template srv2022-exchange2019")

    def test_each_flag_is_raised_once_so_a_review_clears_it_for_good(self, engine, session, estate):
        do_pull(session, FakeHttp(feeds()))
        sync(engine)
        key = ("golden_template", estate["focal"])
        with engine.begin() as conn:  # Mark reviewed, as the API does it
            conn.execute(t.content_review_items.update().where(t.content_review_items.c.item_id == estate["focal"])
                         .values(stale_reasons=[], stale=False, last_reviewed_at=NOW,
                                 revision=t.content_review_items.c.revision + 1))
        do_pull(session, FakeHttp(feeds()))
        assert sync(engine, now=NOW + timedelta(hours=1))["feed_flags"] == 0
        assert reasons(engine, key) == []

    def test_a_row_reviewed_while_the_sync_ran_is_flagged_on_the_next_run(self, engine, session, estate,
                                                                           monkeypatch):
        do_pull(session, FakeHttp(feeds()))
        focal = rows(engine)[("golden_template", estate["focal"])]["id"]
        real = content_review_sync.feeds.flags

        def flags_then_review(db, live, now, **kw):
            out = real(db, live, now, **kw)
            db.execute(t.content_review_items.update().where(t.content_review_items.c.id == focal)
                       .values(revision=t.content_review_items.c.revision + 1))
            return out

        monkeypatch.setattr(content_review_sync.feeds, "flags", flags_then_review)
        sync(engine)
        monkeypatch.setattr(content_review_sync.feeds, "flags", real)
        assert reasons(engine, ("golden_template", estate["focal"])) == []
        sync(engine, now=NOW + timedelta(hours=1))
        assert {r["text"] for r in reasons(engine, ("golden_template", estate["focal"]))} == {
            "Ubuntu 20.04 reached EOL 2025-05-31"}

    def test_a_retired_template_is_not_flagged(self, engine, session, estate):
        with engine.begin() as conn:
            conn.execute(t.content_review_items.update().where(t.content_review_items.c.item_id == estate["focal"])
                         .values(retired=True))
        do_pull(session, FakeHttp(feeds()))
        sync(engine)
        assert reasons(engine, ("golden_template", estate["focal"])) == []

    def test_no_entries_no_work(self, engine, estate):
        assert sync(engine)["feed_flags"] == 0

    def test_feeds_turned_off_flag_nothing_more(self, engine, session, estate, monkeypatch):
        do_pull(session, FakeHttp(feeds()))
        monkeypatch.setenv("TN_FEEDS_MODE", "off")
        assert sync(engine)["feed_flags"] == 0
        assert not any(r["stale"] for r in rows(engine).values())

    def test_a_store_failure_is_recorded_and_the_other_sources_still_run(self, engine, session, monkeypatch):
        real = pull.store.upsert_entries

        def boom(db, found, **kw):
            if any(e.source == "cisa_kev" for e in found):
                raise RuntimeError("duplicate key value violates unique constraint")
            return real(db, found, **kw)

        monkeypatch.setattr(pull.store, "upsert_entries", boom)
        out, _ = do_pull(session, FakeHttp(feeds()))
        assert out["sources"]["cisa_kev"]["status"] == "error" and out["sources"]["nvd"]["status"] == "ok"
        with engine.connect() as conn:
            status = {r.source: r for r in conn.execute(sa.select(t.content_feed_sources))}
        assert "could not be stored (RuntimeError)" in status["cisa_kev"].last_error


class TestMatch:
    """The rules, pure."""

    ITEM = match.Tracked(products=(FeedProduct(name="Ubuntu 24.04", eol_product="ubuntu", eol_cycle="24.04",
                                               release="24.04.5", cpe="cpe:2.3:o:canonical:ubuntu_linux:24.04"),),
                         since=TRACKED)

    def entry(self, key, kind, products, at, **data):
        return {"key": key, "kind": kind, "products": products, "at": at, "title": "", "data": data}

    def test_an_end_of_life_still_to_come_is_not_a_flag_yet(self):
        e = self.entry("eol:ubuntu:24.04", "release", ["eol:ubuntu:24.04"], NOW + timedelta(days=1), eol="x")
        assert match.flags_for({"i": self.ITEM}, [e], set(), NOW) == []
        assert match.flags_for({"i": self.ITEM}, [e], set(), NOW + timedelta(days=2))[0].text == \
            "Ubuntu 24.04 reached EOL x"

    def test_a_release_no_newer_than_the_pinned_one_is_not_a_flag(self):
        for latest, flagged in (("24.04.5", False), ("24.04.4", False), ("24.04.10", True)):
            e = self.entry(f"release:ubuntu:24.04:{latest}", "release", ["eol:ubuntu:24.04"], NOW, latest=latest)
            assert bool(match.flags_for({"i": self.ITEM}, [e], set(), NOW)) is flagged, latest

    def test_a_vulnerability_published_the_day_tracking_began_counts(self):
        same_day = TRACKED.replace(hour=0)
        e = self.entry("nvd:CVE-1", "vulnerability", ["cpe:cpe:2.3:o:canonical:ubuntu_linux:24.04"], same_day,
                       cve="CVE-1", cvss=9.1)
        assert [f.text for f in match.flags_for({"i": self.ITEM}, [e], set(), NOW)] == [
            "CVE-1 in NVD, CVSS 9.1 (Ubuntu 24.04)"]
        assert match.flags_for({"i": self.ITEM}, [e], {("i", "nvd:CVE-1")}, NOW) == []

    def test_an_entry_for_another_product_or_an_unknown_kind_is_ignored(self):
        other = self.entry("eol:debian:13", "release", ["eol:debian:13"], NOW - timedelta(days=1), eol="x")
        odd = self.entry("eol:ubuntu:24.04", "telemetry", ["eol:ubuntu:24.04"], NOW - timedelta(days=1))
        assert match.flags_for({"i": self.ITEM}, [other, odd], set(), NOW) == []
