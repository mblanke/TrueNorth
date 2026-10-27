"""Which feed entries make which content stale (ADR 0014 decision 25): pure, no database.

An entry flags an item when it names one of the item's products (``FeedProduct.product_keys``) and:

* **end of life** (``eol:...``): its date has passed;
* **new release** (``release:...``): the product pins the point release its ISO is
  (``release``) and the entry's latest release in that cycle is newer;
* **vulnerability** (``kev:...``, ``nvd:...``): it is dated on or after the day the item was
  first tracked, so turning feeds on does not flag years of history at once. What is
  published afterwards is flagged.

Each (item, entry) is flagged once: ``raised`` holds those already flagged (the
``content_feed_flags`` table), so a reviewed flag never comes back.

The reason's text is what the Content view shows, e.g. "Ubuntu 20.04 reached EOL
2025-05-31", "CVE-2024-1234 in CISA KEV (Exchange Server 2019): ...".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from ..feed_backends.base import KINDS, FeedProduct

MAX_TEXT = 240


@dataclass(frozen=True)
class Flag:
    item: str  # the review row's id
    entry_key: str
    kind: str
    text: str


@dataclass(frozen=True)
class Tracked:
    """An item the feeds may flag: its products and when it was first tracked."""

    products: tuple[FeedProduct, ...]
    since: datetime


def _utc(dt) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _version(value: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(p) for p in parts) if parts else None


def newer(latest: str, pinned: str) -> bool:
    a, b = _version(latest), _version(pinned)
    if a is None or b is None:
        return latest.strip().lower() != pinned.strip().lower()
    return a > b


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def text_for(entry: Mapping, product: FeedProduct) -> str | None:
    """The reason the entry gives for ``product``, or None when it gives none."""
    key, data = str(entry["key"]), entry.get("data") or {}
    at = _utc(entry["at"])
    if key.startswith("eol:"):
        return f"{product.name} reached EOL {data.get('eol') or at.date().isoformat()}"
    if key.startswith("release:"):
        latest = str(data.get("latest") or "")
        if not product.release or not latest or not newer(latest, product.release):
            return None
        return f"{product.name}: {latest} released (the template is built from {product.release})"
    cve = str(data.get("cve") or key.split(":", 1)[-1])
    if key.startswith("kev:"):
        title = str(entry.get("title") or "")
        return _clip(f"{cve} in CISA KEV ({product.name})" + (f": {title}" if title and title != cve else ""))
    if key.startswith("nvd:"):
        cvss = data.get("cvss")
        return _clip(f"{cve} in NVD, CVSS {cvss} ({product.name})" if cvss is not None
                     else f"{cve} in NVD ({product.name})")
    return None


def flags_for(items: Mapping[str, Tracked], entries: Iterable[Mapping], raised: set[tuple[str, str]],
              now: datetime) -> list[Flag]:
    """New flags, at most one per (item, entry), in a stable order."""
    now = _utc(now)
    by_product: dict[str, list[tuple[str, FeedProduct, datetime]]] = {}
    for item, tracked in items.items():
        for product in tracked.products:
            for k in product.product_keys():
                by_product.setdefault(k, []).append((item, product, _utc(tracked.since)))
    out: dict[tuple[str, str], Flag] = {}
    for entry in sorted(entries, key=lambda e: str(e["key"])):
        kind, key = str(entry.get("kind")), str(entry["key"])
        if kind not in KINDS:
            continue
        at = _utc(entry["at"])
        if at > now:
            continue  # an end-of-life date still to come
        for pk in sorted(set(entry.get("products") or [])):
            for item, product, since in by_product.get(pk, []):
                if (item, key) in raised or (item, key) in out:
                    continue
                if kind == "vulnerability" and at.date() < since.date():
                    continue
                text = text_for(entry, product)
                if text:
                    out[(item, key)] = Flag(item=item, entry_key=key, kind=kind, text=text)
    return sorted(out.values(), key=lambda f: (f.item, f.entry_key))
