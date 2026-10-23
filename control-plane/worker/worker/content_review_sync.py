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

   An item depends only on content of its own tenant or the shared catalogue. The derived
   edges are replaced each run.
3. **Clock.** Each item's due date (last review, else when it was first tracked, plus its
   cycle) and its own overdue reason, then every inherited reason rebuilt over the new
   edges (content_staleness.py, a copy of the API's ``app/content_review/staleness.py``).

The review rows are locked for the run (FOR UPDATE, in id order: the order the API's Mark
reviewed locks them in), so a review waits for the sync or the sync for it. Belt and braces,
a row is written only if nobody changed it since it was read (``revision``): a review that got
in first wins, and the next run catches up. SQLAlchemy Core on
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

from . import content_staleness as st
from .celery_app import app
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
        with db_session() as db:
            return sync(db, lost=lock.lost)


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
        if str(e.state) in ("cancelled", "EventState.cancelled"):
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


def sync(db, *, now: datetime | None = None, lost: threading.Event | None = None) -> dict:
    """One run. ``lost``: the lock was lost; nothing more is written."""
    now = _utc(now) or datetime.now(UTC)
    content = read_content(db)
    cycle = _cycles(db)
    themes = _themes_by_name(db)
    # Locked (FOR UPDATE) in id order for the run, the order the API locks them in
    # (app/content_review/service.load_graph): a review waits for the sync, or the sync for it.
    existing = {(r.item_type, r.item_id): r for r in
                db.execute(sa.select(items).order_by(items.c.id).with_for_update()).all()}
    summary = {"status": "ok", "created": 0, "updated": 0, "missing": 0, "edges": 0, "skipped": 0}

    # 1. items
    for key, info in content.found.items():
        if key in existing:
            continue
        due = st.due_at(baseline=now, last_reviewed=None, cycle_days=cycle(info["tenant"], key[0]))
        db.execute(items.insert().values(
            id=str(uuid.uuid4()), tenant_id=info["tenant"], item_type=key[0], item_id=key[1], title=info["title"],
            owner_id=info["owner"], reviewer_id=None,
            theme_ids=_auto_themes(themes, info["tenant"], info["tags"]), cycle_days=None,
            last_reviewed_at=None, last_reviewed_by=None, due_at=due, stale=False, stale_reasons=[],
            retired=False, retired_at=None, retired_by=None, missing=False, revision=0,
            created_at=now, updated_at=now))
        summary["created"] += 1
    for key, row in sorted(existing.items(), key=lambda kv: str(kv[1].id)):
        if lost is not None and lost.is_set():
            return {**summary, "status": "lock_lost"}
        info = content.found.get(key)
        if info is None:
            if row.missing or key[0] == "lab":  # labs have no record of their own yet
                continue
            values, counter = {"missing": True}, "missing"
        elif row.title != info["title"] or _s(row.tenant_id) != info["tenant"] or row.missing:
            values, counter = {"title": info["title"], "tenant_id": info["tenant"], "missing": False}, "updated"
        else:
            continue
        done = db.execute(items.update().where(items.c.id == row.id, items.c.revision == row.revision).values(
            **values, revision=row.revision + 1, updated_at=now))
        summary[counter if done.rowcount else "skipped"] += 1

    # 2. edges, replaced
    rows = {(r.item_type, r.item_id): r for r in db.execute(sa.select(items).where(items.c.missing.is_(False))).all()}
    ids = {k: str(r.id) for k, r in rows.items()}
    wanted = {(ids[a], ids[b]) for a, deps in content.depends.items() for b in deps if a in ids and b in ids}
    db.execute(edges.delete().where(edges.c.derived.is_(True)))
    manual = {(str(e.item_id), str(e.depends_on_id)) for e in
              db.execute(sa.select(edges.c.item_id, edges.c.depends_on_id)).all()}
    for item_id, dep_id in sorted(wanted - manual):
        db.execute(edges.insert().values(id=str(uuid.uuid4()), item_id=item_id, depends_on_id=dep_id, derived=True))
    summary["edges"] = len(wanted)

    # 3. the clock: due dates, overdue reasons, then inherited reasons over the new edges
    by_id = {str(r.id): r for r in rows.values()}
    depends: dict[str, set[str]] = {}
    for a, b in wanted | manual:
        if a in by_id and b in by_id:
            depends.setdefault(a, set()).add(b)
    names = {k: (st.ITEM_TYPES.get(r.item_type, r.item_type), r.title) for k, r in by_id.items()}
    graph = st.Graph({k: _reasons(r.stale_reasons) for k, r in by_id.items()}, st.used_by(depends), names)
    due: dict[str, datetime] = {}
    changed: set[str] = set()
    for k, r in by_id.items():
        days = r.cycle_days or cycle(_s(r.tenant_id), r.item_type)
        due[k] = st.due_at(baseline=_utc(r.created_at) or now, last_reviewed=_utc(r.last_reviewed_at),
                           cycle_days=days)
        if graph.set_overdue(k, st.overdue_text(due[k], now), at=now):
            changed.add(k)
    changed |= graph.repropagate(at=now)
    for k, r in sorted(by_id.items()):
        if lost is not None and lost.is_set():
            return {**summary, "status": "lock_lost"}
        reasons = graph.reasons.get(k, [])
        if k not in changed and _utc(r.due_at) == due[k] and bool(r.stale) == bool(reasons):
            continue
        done = db.execute(items.update().where(items.c.id == r.id, items.c.revision == r.revision).values(
            due_at=due[k], stale_reasons=reasons, stale=bool(reasons), revision=r.revision + 1, updated_at=now))
        summary["updated" if done.rowcount else "skipped"] += 1
    logger.info("[content-review] %s", summary)
    return summary
