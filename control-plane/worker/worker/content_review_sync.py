"""The content clock's periodic sync (ADR 0014 decisions 22-24, issue #286).

Each run, under a Redis lock (redis_lock.py) so two never overlap:

1. **Items.** Every course, course module, scenario, range template and golden template that
   exists gets a review row (``content_review_items``); a renamed one gets its new title, a
   deleted one is marked ``missing`` (kept for its history, hidden). A new course takes the
   instructor of its latest booking as its owner, and its modules the course's; a new item
   whose own tags name a theme gets that theme. Labs have no record of their own yet (they
   live inside course release bundles), so none are made.
2. **Edges.** What each item depends on, derived from what the content already says:

   * range template -> golden templates: each machine's OS (``os``, else ``os_template``,
     else ``type``, else the default, exactly as render.py builds it) matched to a golden
     template's catalogue id or one of its OS aliases;
   * scenario -> range template: its ``range_template`` (a template's id or name);
   * module -> scenario: ``module_content.scenario_id`` and the module's legacy
     ``content_ref`` ``scenario_id``;
   * course -> its modules, and course -> range template where a booking
     (``scheduled_events``, not cancelled) links the two.

   An item depends only on content of its own tenant or the shared catalogue. Only the
   difference from the derived edges already stored is written, in bulk.
3. **Clock.** Each item's due date (last review, else when it was first tracked, plus its
   cycle) and its own overdue reason, then every inherited reason rebuilt over the new
   edges (content_review_runtime/staleness.py, generated from the API's ``app/content_review/staleness.py``).
4. **Feeds.** Before that rebuild, new release and vulnerability feed entries flag the
   golden templates they name (ADR 0014 decision 25; ``content_feeds/``), each once.

Everything is read unlocked and computed first. Only then are the review rows that change
locked (FOR NO KEY UPDATE, which an edge insert's foreign key check does not block; in id order,
the order the API locks them in), and each is written only
if its ``revision`` is still the one read: a review that committed meanwhile wins, and the
next run catches up. A lost Redis lock raises LockLostError, so the run rolls back whole.
SQLAlchemy Core on
the generated table mirror only (tables.py): no raw SQL in the worker (ADR 0003).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
import yaml

from .celery_app import app
from .content_feeds import apply as feeds
from .content_review_runtime import staleness as st
from .redis_lock import RedisLock
from .render import _DESIGNER_NON_VM_TYPES, _extract_nodes, _node_os, os_alias_candidates
from .tables import (
    content_review_cycles,
    content_review_edges,
    content_review_items,
    content_review_themes,
    course_modules,
    courses,
    golden_images,
    module_content,
    scenarios,
    scheduled_events,
    templates,
)
from .task_plumbing import db_session

logger = logging.getLogger("truenorth.worker.content_review_sync")

TASK = "worker.content_review_sync.sync_content_review"
LOCK_KEY = "truenorth:content-review-sync:lock"
LOCK_TTL_MS = int(float(os.getenv("CONTENT_REVIEW_SYNC_LOCK_SECONDS", "600")) * 1000)

items = content_review_items
edges = content_review_edges

Key = tuple[str, str]  # (item_type, content id)


def redis_client():
    """The worker's Redis (its broker, REDIS_URL), short socket timeouts."""
    import redis

    from .tasks import REDIS_URL  # read at call time: tasks imports this module's siblings at load

    return redis.Redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)


def sync_lock(client=None) -> RedisLock:
    return RedisLock(client if client is not None else redis_client(), LOCK_KEY, LOCK_TTL_MS)


@app.task(bind=True, name=TASK)
def sync_content_review(self):
    with sync_lock() as lock:
        if not lock.held:
            logger.info("[content-review] another sync holds the lock (or Redis is down); this one does nothing")
            return {"status": "skipped", "reason": "a sync is already running, or Redis is unavailable"}
        try:
            with db_session() as db:  # LockLostError leaves it uncommitted: rolled back
                return sync(db, lost=lock.lost)
        except LockLostError as exc:
            logger.error("[content-review] %s", exc)
            return {"status": "lock_lost"}


# ── reading the content ──────────────────────────────────────────────────────
def _s(value) -> str | None:
    return None if value is None else str(value)


def _doc(text) -> dict:
    try:
        doc = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def _tags(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    return [str(t) for t in value if t] if isinstance(value, list) else []


def template_os_names(doc: dict) -> list[str]:
    """The OS of every machine the template builds, as render.py decides it."""
    out = []
    for node in _extract_nodes(doc):
        if not isinstance(node, dict) or str(node.get("type", "")).lower() in _DESIGNER_NON_VM_TYPES:
            continue
        out.append(_node_os(node))
    return list(dict.fromkeys(out))


class Content:
    """What exists, read once: ``found[key] = {tenant, title, tags, owner}``; ``depends[key]``."""

    def __init__(self):
        self.found: dict[Key, dict] = {}
        self.depends: dict[Key, set[Key]] = {}

    def add(self, key: Key, tenant, title: str, *, tags=(), owner=None) -> None:
        self.found[key] = {"tenant": _s(tenant), "title": (title or key[1])[:255], "tags": list(tags), "owner": owner}

    def dep(self, item: Key, on: Key) -> bool:
        """Only content of the item's tenant or the shared catalogue, and never itself.
        Returns whether the edge was taken."""
        a, b = self.found.get(item), self.found.get(on)
        if a is None or b is None or item == on:
            return False
        if b["tenant"] is None or b["tenant"] == a["tenant"]:
            self.depends.setdefault(item, set()).add(on)
            return True
        return False


def read_content(db) -> Content:
    c = Content()
    # golden templates, indexed by every name a template may use for them
    golden_by_name: dict[str, list[tuple[str, str | None]]] = {}
    for g in db.execute(sa.select(golden_images.c.id, golden_images.c.catalogue_id, golden_images.c.role,
                                  golden_images.c.os_aliases, golden_images.c.tenant_id)
                        .where(golden_images.c.deleted_at.is_(None))).all():
        title = g.catalogue_id + (f" ({g.role})" if g.role else "")
        c.add(("golden_template", str(g.id)), g.tenant_id, title)
        names = {g.catalogue_id, *_tags(g.os_aliases)}
        for n in names:
            golden_by_name.setdefault(n, []).append((str(g.id), _s(g.tenant_id)))
    # range templates -> golden templates
    template_rows = db.execute(sa.select(templates.c.id, templates.c.name, templates.c.yaml, templates.c.tenant_id)
                               .where(templates.c.deleted_at.is_(None))).all()
    by_template_name: dict[str, list[tuple[str, str | None]]] = {}
    for t in template_rows:
        doc = _doc(t.yaml)
        key = ("range_template", str(t.id))
        c.add(key, t.tenant_id, t.name, tags=_tags(doc.get("tags")))
        by_template_name.setdefault(t.name, []).append((str(t.id), _s(t.tenant_id)))
        for os_name in template_os_names(doc):
            for alias in os_alias_candidates(os_name):
                # The first spelling with a golden template this template may use (another
                # tenant's under that spelling does not stop the search).
                taken = [c.dep(key, ("golden_template", gid)) for gid, _tenant in golden_by_name.get(alias, [])]
                if any(taken):
                    break
    # scenarios -> range template (by id, else by name: the scenario's tenant first, then shared)
    template_ids = {str(t.id) for t in template_rows}
    scenario_rows = db.execute(sa.select(scenarios.c.id, scenarios.c.name, scenarios.c.yaml, scenarios.c.tenant_id)
                               .where(scenarios.c.deleted_at.is_(None))).all()
    for s in scenario_rows:
        key = ("scenario", str(s.id))
        doc = _doc(s.yaml)
        c.add(key, s.tenant_id, s.name, tags=_tags(doc.get("tags")))
        ref = str(doc.get("range_template") or "").strip()
        if not ref:
            continue
        if ref in template_ids:
            c.dep(key, ("range_template", ref))
            continue
        named = by_template_name.get(ref, [])
        pick = [tid for tid, tenant in named if tenant == _s(s.tenant_id)] or [tid for tid, tenant in named
                                                                               if tenant is None]
        if pick:
            c.dep(key, ("range_template", sorted(pick)[0]))
    # courses, their latest booking's instructor, and the templates their bookings use
    owners: dict[str, str] = {}
    booked: list[tuple[str, str]] = []
    for e in db.execute(sa.select(scheduled_events.c.course_id, scheduled_events.c.template_id,
                                  scheduled_events.c.instructor_id, scheduled_events.c.state)
                        .where(scheduled_events.c.course_id.is_not(None))
                        .order_by(scheduled_events.c.start_time)).all():
        if str(e.state).removeprefix("EventState.") in ("cancelled", "rejected"):
            continue
        if e.instructor_id is not None:
            owners[str(e.course_id)] = str(e.instructor_id)  # the latest wins
        if e.template_id is not None:
            booked.append((str(e.course_id), str(e.template_id)))
    for co in db.execute(sa.select(courses.c.id, courses.c.name, courses.c.tenant_id, courses.c.tags)).all():
        # A shared course is booked by many tenants: no one tenant's instructor owns it.
        owner = owners.get(str(co.id)) if co.tenant_id is not None else None
        c.add(("course", str(co.id)), co.tenant_id, co.name, tags=_tags(co.tags), owner=owner)
    for course_id, template_id in booked:
        c.dep(("course", course_id), ("range_template", template_id))
    # modules: in their course's tenant; course -> module -> scenario
    module_rows = db.execute(sa.select(course_modules.c.id, course_modules.c.title, course_modules.c.course_id,
                                       course_modules.c.content_ref)).all()
    for m in module_rows:
        course = c.found.get(("course", str(m.course_id)))
        if course is None:
            continue
        key = ("module", str(m.id))
        c.add(key, course["tenant"], m.title, owner=course["owner"])
        c.dep(("course", str(m.course_id)), key)
        try:
            ref = json.loads(m.content_ref or "{}")
        except ValueError:
            ref = {}
        if isinstance(ref, dict) and ref.get("scenario_id"):
            c.dep(key, ("scenario", str(ref["scenario_id"])))
    for mc in db.execute(sa.select(module_content.c.module_id, module_content.c.scenario_id)
                         .where(module_content.c.scenario_id.is_not(None))).all():
        c.dep(("module", str(mc.module_id)), ("scenario", str(mc.scenario_id)))
    return c


# ── the sync ─────────────────────────────────────────────────────────────────
def _utc(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _reasons(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _cycles(db):
    rows = {(_s(r.tenant_id), r.item_type): r.days for r in
            db.execute(sa.select(content_review_cycles.c.tenant_id, content_review_cycles.c.item_type,
                                 content_review_cycles.c.days)).all()}

    def get(tenant: str | None, item_type: str) -> int:
        if tenant is not None and (tenant, item_type) in rows:
            return rows[(tenant, item_type)]
        return rows.get((None, item_type), st.DEFAULT_CYCLE_DAYS)

    return get


def _themes_by_name(db) -> dict[tuple[str | None, str], str]:
    return {(_s(t.tenant_id), _norm(t.name)): str(t.id) for t in
            db.execute(sa.select(content_review_themes.c.id, content_review_themes.c.tenant_id,
                                 content_review_themes.c.name)).all()}


def _auto_themes(themes: dict, tenant: str | None, tags: list[str]) -> list[str]:
    out = []
    for tag in tags:
        tid = themes.get((tenant, _norm(tag))) or themes.get((None, _norm(tag)))
        if tid and tid not in out:
            out.append(tid)
    return out


class LockLostError(RuntimeError):
    """The sync's Redis lock was lost: another sync may be running. Raised so the run's
    transaction rolls back rather than committing half a run."""


def _check(lost: threading.Event | None) -> None:
    if lost is not None and lost.is_set():
        raise LockLostError("the content review sync lost its lock; nothing of this run is kept")


def sync(db, *, now: datetime | None = None, lost: threading.Event | None = None, feed_products=None) -> dict:
    """One run, in the caller's transaction. Raises LockLostError (nothing written is kept) once
    ``lost`` is set.

    Reads are unlocked; new rows and the edge difference are written in bulk; then the review
    rows that change are locked (FOR NO KEY UPDATE, in id order, as the API locks them) and
    each is written only if its ``revision`` is still the one read: a review that committed
    meanwhile wins, and the next run catches up.

    Release and vulnerability feed entries (ADR 0014 decision 25) flag the golden templates
    they name (``content_feeds/apply.py``) before the inherited reasons are rebuilt; each
    flag is recorded only once its row is written. ``feed_products``: the product map
    (tests); by default ``content/catalogue/feed_products.yaml``."""
    now = _utc(now) or datetime.now(UTC)
    content = read_content(db)
    cycle = _cycles(db)
    themes = _themes_by_name(db)
    existing = {(r.item_type, r.item_id): r._asdict() for r in db.execute(sa.select(items)).all()}
    summary = {"status": "ok", "created": 0, "updated": 0, "missing": 0, "edges": 0, "skipped": 0, "feed_flags": 0}
    _check(lost)

    # 1. items: new ones inserted in bulk; changes to existing ones collected for step 3
    new_rows = []
    for key, info in content.found.items():
        if key in existing:
            continue
        due = st.due_at(baseline=now, last_reviewed=None, cycle_days=cycle(info["tenant"], key[0]))
        new_rows.append(dict(
            id=str(uuid.uuid4()), tenant_id=info["tenant"], item_type=key[0], item_id=key[1], title=info["title"],
            owner_id=info["owner"], reviewer_id=None,
            theme_ids=_auto_themes(themes, info["tenant"], info["tags"]), cycle_days=None,
            last_reviewed_at=None, last_reviewed_by=None, due_at=due, stale=False, stale_reasons=[],
            retired=False, retired_at=None, retired_by=None, missing=False, revision=0,
            created_at=now, updated_at=now))
    if new_rows:
        db.execute(items.insert(), new_rows)
    summary["created"] = len(new_rows)
    pending: dict[str, dict] = {}  # row id -> column changes
    live: dict[str, dict] = {}  # row id -> row as it will be, for the rows that are not missing
    for key, row in existing.items():
        rid, info = str(row["id"]), content.found.get(key)
        if info is None:
            if key[0] == "lab" and not row["missing"]:  # labs have no record of their own yet: kept
                live[rid] = row
            elif not row["missing"]:
                pending[rid] = {"missing": True}
                summary["missing"] += 1
            continue
        if row["title"] != info["title"] or _s(row["tenant_id"]) != info["tenant"] or row["missing"]:
            pending[rid] = {"title": info["title"], "tenant_id": info["tenant"], "missing": False}
        live[rid] = {**row, **pending.get(rid, {})}
    for row in new_rows:
        live[row["id"]] = row
    key_to_id = {(r["item_type"], r["item_id"]): rid for rid, r in live.items()}

    # 2. edges: only the difference, in bulk
    wanted = {(key_to_id[a], key_to_id[b]) for a, deps in content.depends.items() for b in deps
              if a in key_to_id and b in key_to_id}
    derived, manual = {}, set()
    for e in db.execute(sa.select(edges.c.id, edges.c.item_id, edges.c.depends_on_id, edges.c.derived)).all():
        pair = (str(e.item_id), str(e.depends_on_id))
        if e.derived:
            derived[pair] = str(e.id)
        else:
            manual.add(pair)
    gone = [eid for pair, eid in derived.items() if pair not in wanted]
    added = sorted(wanted - set(derived) - manual)
    _check(lost)
    if gone:
        db.execute(edges.delete().where(edges.c.id.in_(gone)))
    if added:
        db.execute(edges.insert(), [{"id": str(uuid.uuid4()), "item_id": a, "depends_on_id": b, "derived": True}
                                    for a, b in added])
    summary["edges"] = len(wanted)

    # 3. the clock: due dates, overdue reasons, then inherited reasons over the new edges
    depends: dict[str, set[str]] = {}
    for a, b in wanted | manual:
        if a in live and b in live:
            depends.setdefault(a, set()).add(b)
    names = {k: (st.ITEM_TYPES.get(r["item_type"], r["item_type"]), r["title"]) for k, r in live.items()}
    graph = st.Graph({k: _reasons(r["stale_reasons"]) for k, r in live.items()}, st.used_by(depends), names,
                     retired={k for k, r in live.items() if r["retired"]})
    for k, r in live.items():
        days = r["cycle_days"] or cycle(_s(r["tenant_id"]), r["item_type"])
        due = st.due_at(baseline=_utc(r["created_at"]) or now, last_reviewed=_utc(r["last_reviewed_at"]),
                        cycle_days=days)
        graph.set_overdue(k, st.overdue_text(due, now), at=now)
        if _utc(r["due_at"]) != due:
            pending.setdefault(k, {})["due_at"] = due
    feed_flags = feeds.flags(db, live, now, products=feed_products)
    for f in feed_flags:
        graph.flag_stale(f.item, f.text, f.entry_key, kind=f.kind, at=now)
    graph.repropagate(at=now)
    for k, r in live.items():
        reasons = graph.reasons.get(k, [])
        if _norm_reasons(reasons) != _norm_reasons(_reasons(r["stale_reasons"])) or bool(r["stale"]) != bool(reasons):
            pending.setdefault(k, {}).update(stale_reasons=reasons, stale=bool(reasons))
    _check(lost)

    # 4. write what changed: lock those rows in id order, keep a row someone changed meanwhile
    read_revision = {str(r["id"]): r["revision"] for r in existing.values()}
    write = {k: v for k, v in pending.items() if k in read_revision}  # new rows already carry theirs
    for row in new_rows:
        if row["id"] in pending:
            db.execute(items.update().where(items.c.id == row["id"]).values(**pending[row["id"]], updated_at=now))
    skipped: set[str] = set()
    if write:
        locked = db.execute(sa.select(items.c.id, items.c.revision).where(items.c.id.in_(sorted(write)))
                            .order_by(items.c.id).with_for_update(key_share=True)).all()
        for got in locked:
            rid = str(got.id)
            if got.revision != read_revision[rid]:
                summary["skipped"] += 1
                skipped.add(rid)
                continue
            db.execute(items.update().where(items.c.id == got.id).values(
                **write[rid], revision=got.revision + 1, updated_at=now))
            if "missing" not in write[rid] or len(write[rid]) > 1:
                summary["updated"] += 1
        skipped |= set(write) - {str(got.id) for got in locked}
    # A feed flag counts as raised once its row carries it; a skipped row is flagged next run.
    summary["feed_flags"] = feeds.record(db, [f for f in feed_flags if f.item not in skipped], now)
    _check(lost)
    logger.info("[content-review] %s", summary)
    return summary


def _norm_reasons(reasons: list) -> list:
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in reasons)
