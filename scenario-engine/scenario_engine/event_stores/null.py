"""Offline event store: fixed events, matched only by ``field:value`` terms.

For tests and air-gapped dry runs. It is not a Lucene engine: a query matches an event
when every ``field:value`` term (``*`` wildcards allowed, AND only) matches. Anything
it cannot parse matches nothing.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from .base import BaseEventStore, EventHits

_TERM = re.compile(r'([\w.@]+):("([^"]*)"|\S+)')


class NullEventStore(BaseEventStore):
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = list(events or [])

    async def search(self, index: str, query: str | dict[str, Any], size: int = 20) -> EventHits:
        if not isinstance(query, str):
            return EventHits(total=0)
        # (field, raw value, inner text when the value was "quoted")
        terms = [(f, inner if raw.startswith('"') else raw) for f, raw, inner in _TERM.findall(query)]
        if not terms:
            return EventHits(total=0)
        matched = [e for e in self.events if all(fnmatch.fnmatch(str(_get(e, f)), pat) for f, pat in terms)]
        return EventHits(total=len(matched), hits=[{"_id": str(i), "_source": e} for i, e in enumerate(matched[:size])])


def _get(event: dict[str, Any], dotted: str) -> Any:
    """``url.domain`` as a flat key first (ECS-style docs), then as a nested path."""
    if dotted in event:
        return event[dotted]
    cur: Any = event
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
