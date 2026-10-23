"""The content clock's rules (ADR 0014 decisions 23 and 24): pure, no database, no clock.

The worker carries a generated copy (``worker/content_review_runtime/staleness.py``) for its
periodic sync, because it may not import the API (ADR 0002 §5). Change this file, then run
``python scripts/export_content_staleness.py``; ``tests/contracts/test_content_staleness_copy.py``
fails until you do.

An item is identified by a key (the review row's id). Its stale reasons are a list of dicts:

* ``origin``: the key of the item the staleness started at. An item's *own* reasons have
  its own key; reasons it inherited have the key of the item upstream.
* ``kind``: ``time`` (its review is overdue), ``release`` or ``vulnerability`` (a feed, or an
  admin, flagged it; ADR 0014 decision 25 brings the feeds), or ``upstream`` (inherited).
* ``text``: what the Content view shows, e.g. "Its golden template Ubuntu 24.04 is stale".
* ``source``: what raised it: a feed entry, ``clock``, or for ``upstream`` the origin's key.
* ``at``: when it was first raised (ISO 8601).

Rules:

* Due = last review (or, never reviewed, the baseline: when the item was first tracked)
  plus its cycle, 90 to 120 days.
* Release and vulnerability flags flow *down* the dependency edges, from what is depended on
  to what depends on it (golden template -> range template -> scenario -> module -> course).
  Every item reached gets one ``upstream`` reason per origin. An overdue review does not
  flow down: each item has its own clock.
* Mark reviewed clears the item's own reasons and the reasons it pushed downstream. Reasons
  the item inherited stay until their origin is reviewed.
* A retired item is no source: retiring clears what it caused downstream, and it is never
  pushed again.
* Cycles in the edges are walked once (a visited set), so they neither loop nor add a
  second reason.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

# What the content clock covers (ADR 0014 decision 22), with the glossary's names.
ITEM_TYPES: dict[str, str] = {
    "course": "course",
    "module": "module",
    "lab": "lab",
    "scenario": "scenario",
    "range_template": "range template",
    "golden_template": "golden template",
}

DEFAULT_CYCLE_DAYS = 90
MIN_CYCLE_DAYS = 90
MAX_CYCLE_DAYS = 120
AMBER_DAYS = 14  # the clock badge turns amber this many days before the due date

KIND_TIME = "time"
KIND_RELEASE = "release"
KIND_VULNERABILITY = "vulnerability"
KIND_UPSTREAM = "upstream"
FLAG_KINDS = (KIND_RELEASE, KIND_VULNERABILITY)  # what flag_stale accepts, and what propagates
TIME_SOURCE = "clock"

Key = str
Reason = dict


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def check_cycle(days: int) -> int:
    """``days`` when it is a permitted review cycle, else ValueError."""
    if not isinstance(days, int) or isinstance(days, bool) or not MIN_CYCLE_DAYS <= days <= MAX_CYCLE_DAYS:
        raise ValueError(f"a review cycle is between {MIN_CYCLE_DAYS} and {MAX_CYCLE_DAYS} days")
    return days


def due_at(*, baseline: datetime, last_reviewed: datetime | None, cycle_days: int) -> datetime:
    """When the next review is due."""
    return _utc(last_reviewed or baseline) + timedelta(days=cycle_days)


def overdue_text(due: datetime, now: datetime) -> str | None:
    """The ``time`` reason's text once ``due`` has passed, else None."""
    late = _utc(now) - _utc(due)
    if late <= timedelta(0):
        return None
    days = late.days
    if days == 0:
        return "Review overdue (due today)"
    return f"Review overdue by {days} day{'s' if days != 1 else ''}"


def clock(due: datetime, now: datetime, *, stale: bool) -> tuple[str, int]:
    """The clock badge: (``green`` | ``amber`` | ``stale``, whole days until due)."""
    days = (_utc(due) - _utc(now)).days
    if stale or _utc(due) <= _utc(now):
        return "stale", days
    return ("amber" if days <= AMBER_DAYS else "green"), days


def used_by(depends_on: Mapping[Key, Iterable[Key]]) -> dict[Key, set[Key]]:
    """Invert ``item -> what it depends on`` into ``item -> what depends on it``."""
    out: dict[Key, set[Key]] = {}
    for item, deps in depends_on.items():
        for dep in deps:
            out.setdefault(dep, set()).add(item)
    return out


def _valid(reason) -> bool:
    return isinstance(reason, dict) and all(isinstance(reason.get(k), str) for k in ("origin", "kind", "text"))


class Graph:
    """Stale reasons over the dependency edges. Mutates ``reasons`` in place; every
    operation returns the keys whose reasons changed (what to write back)."""

    def __init__(self, reasons: Mapping[Key, list], used_by: Mapping[Key, Iterable[Key]],
                 names: Mapping[Key, tuple[str, str]], *, retired: Iterable[Key] = ()):
        self.reasons: dict[Key, list[Reason]] = {k: [r for r in (v or []) if _valid(r)] for k, v in reasons.items()}
        self.used_by = {k: sorted(set(v)) for k, v in used_by.items()}
        self.names = names
        # Retired items are no source: their flags reach nothing downstream (retiring one
        # clears what it caused, and a rebuild must not bring it back).
        self.retired = set(retired)

    # -- reading --
    def is_stale(self, item: Key) -> bool:
        return bool(self.reasons.get(item))

    def dependents(self, item: Key) -> list[tuple[Key, list[Key]]]:
        """Everything downstream of ``item``, each once, with its shortest path from ``item``
        (breadth first; a visited set makes cycles safe). ``item`` itself is not included."""
        seen = {item}
        out: list[tuple[Key, list[Key]]] = []
        queue: deque[tuple[Key, list[Key]]] = deque([(item, [item])])
        while queue:
            node, path = queue.popleft()
            for nxt in self.used_by.get(node, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                out.append((nxt, [*path, nxt]))
                queue.append((nxt, [*path, nxt]))
        return out

    def _name(self, key: Key) -> str:
        label, title = self.names.get(key, ("item", key))
        return f"{label} {title}"

    def _upstream_text(self, origin: Key, path: list[Key]) -> str:
        text = f"Its {self._name(origin)} is stale"
        if len(path) > 2:  # origin -> ... -> the item before this one -> this one
            text += f" (through {self._name(path[-2])})"
        return text

    # -- writing --
    def _push(self, origin: Key, at: str, previous: Mapping[tuple[Key, Key], str] | None = None) -> set[Key]:
        changed: set[Key] = set()
        for key, path in self.dependents(origin):
            mine = self.reasons.setdefault(key, [])
            if any(r["origin"] == origin for r in mine):
                continue
            first_seen = (previous or {}).get((key, origin), at)
            mine.append({"origin": origin, "kind": KIND_UPSTREAM, "text": self._upstream_text(origin, path),
                         "source": origin, "at": first_seen})
            changed.add(key)
        return changed

    def flag_stale(self, item: Key, reason: str, source: str, *, kind: str = KIND_RELEASE,
                   at: datetime | None = None) -> set[Key]:
        """Flag ``item`` stale because of ``source`` (a feed entry, an admin), and everything
        downstream of it. The hook the feeds (ADR 0014 decision 25, issue #287) call."""
        if kind not in FLAG_KINDS:
            raise ValueError(f"kind must be one of {FLAG_KINDS}")
        when = _utc(at or datetime.now(UTC)).isoformat()
        mine = self.reasons.setdefault(item, [])
        changed: set[Key] = set()
        if not any(r["origin"] == item and r["kind"] == kind and r.get("source") == source for r in mine):
            mine.append({"origin": item, "kind": kind, "text": reason, "source": source, "at": when})
            changed.add(item)
        if item in self.retired:
            return changed
        return changed | self._push(item, when)

    def set_overdue(self, item: Key, text: str | None, *, at: datetime | None = None) -> bool:
        """Set (``text``) or clear (None) the item's own ``time`` reason. Never propagates."""
        mine = self.reasons.setdefault(item, [])
        current = next((r for r in mine if r["origin"] == item and r["kind"] == KIND_TIME), None)
        if text is None:
            if current is None:
                return False
            mine.remove(current)
            return True
        if current is not None:
            if current["text"] == text:
                return False
            current["text"] = text
            return True
        mine.append({"origin": item, "kind": KIND_TIME, "text": text, "source": TIME_SOURCE,
                     "at": _utc(at or datetime.now(UTC)).isoformat()})
        return True

    def mark_reviewed(self, item: Key) -> set[Key]:
        """Clear ``item``'s own reasons, and every reason anywhere that started at it."""
        changed: set[Key] = set()
        for key, mine in self.reasons.items():
            kept = [r for r in mine if r["origin"] != item]
            if len(kept) != len(mine):
                self.reasons[key] = kept
                changed.add(key)
        return changed

    def repropagate(self, *, at: datetime | None = None) -> set[Key]:
        """Rebuild every ``upstream`` reason from the items' own flags over the current edges
        (after the edges changed). A reason that survives keeps the time it was first seen."""
        when = _utc(at or datetime.now(UTC)).isoformat()
        before = {k: [dict(r) for r in v] for k, v in self.reasons.items()}
        previous = {(k, r["origin"]): r.get("at", when) for k, v in self.reasons.items() for r in v
                    if r["kind"] == KIND_UPSTREAM}
        for key in list(self.reasons):
            self.reasons[key] = [r for r in self.reasons[key] if r["kind"] != KIND_UPSTREAM]
        origins = sorted({r["origin"] for v in self.reasons.values() for r in v
                          if r["kind"] in FLAG_KINDS and r["origin"] not in self.retired})
        for origin in origins:
            self._push(origin, when, previous)

        def norm(rs):
            return sorted((r["origin"], r["kind"], r["text"], str(r.get("source")), str(r.get("at"))) for r in rs)

        keys = set(before) | set(self.reasons)
        return {k for k in keys if norm(before.get(k, [])) != norm(self.reasons.get(k, []))}
