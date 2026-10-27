"""endoflife.date: release cycles, their end-of-life dates and latest point releases.

``GET https://endoflife.date/api/<product>.json`` once per product in the product map. Each
release cycle the map names gives:

* an end-of-life entry (``eol:<product>:<cycle>``, kind ``release``) dated its EOL date,
  when it has one (the clock flags it once that date has passed);
* a release entry (``release:<product>:<cycle>:<latest>``, kind ``release``) for the
  cycle's latest point release, so a new ISO in the same cycle is noticed.

A product endoflife.date does not know (HTTP 404) is a per-product error, not a failed feed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from .base import BaseFeedSource, FeedEntry, FeedError, FeedMalformedError, FeedProduct, FeedResult, eol_key

BASE_URL = "https://endoflife.date/api"


def _day(value) -> datetime | None:
    try:
        d = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


class EndOfLifeSource(BaseFeedSource):
    name = "endoflife"
    label = "endoflife.date (releases and end of life)"

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")

    def fetch(self, http, products: Sequence[FeedProduct], *, since: datetime | None, now: datetime) -> FeedResult:
        wanted: dict[str, set[str]] = {}
        for p in products:
            if p.eol_product and p.eol_cycle:
                wanted.setdefault(p.eol_product.strip().lower(), set()).add(p.eol_cycle.strip().lower())
        out = FeedResult()
        failed = 0
        for product in sorted(wanted):
            try:
                cycles = http.get_json(f"{self.base_url}/{product}.json")
                if not isinstance(cycles, list):
                    raise FeedMalformedError(f"endoflife.date's {product} is not a list of release cycles")
            except FeedError as exc:
                failed += 1
                out.errors.append(f"{product}: {exc}")
                continue
            out.entries.extend(self.entries(product, cycles, wanted[product], now))
        if wanted and failed == len(wanted):
            raise FeedError("; ".join(out.errors))
        return out

    @staticmethod
    def entries(product: str, cycles: list, wanted: set[str], now: datetime) -> list[FeedEntry]:
        out = []
        for c in cycles:
            if not isinstance(c, dict) or str(c.get("cycle", "")).strip().lower() not in wanted:
                continue
            cycle = str(c["cycle"]).strip()
            key = eol_key(product, cycle)
            eol = c.get("eol")
            eol_at = _day(eol)
            if eol_at is not None or eol is True:
                when = eol_at or now
                out.append(FeedEntry(
                    key=key, source="endoflife", kind="release", products=(key,), at=when,
                    title=f"{product} {cycle} end of life",
                    data={"product": product, "cycle": cycle, "eol": eol_at.date().isoformat() if eol_at else None}))
            latest = str(c.get("latest") or "").strip()
            if latest:
                out.append(FeedEntry(
                    key=f"release:{product}:{cycle}:{latest}".lower(), source="endoflife", kind="release",
                    products=(key,), at=_day(c.get("latestReleaseDate")) or now,
                    title=f"{product} {latest} released",
                    data={"product": product, "cycle": cycle, "latest": latest}))
        return out
