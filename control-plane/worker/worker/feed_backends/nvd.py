"""The NVD CVE API 2.0: CVEs published for the products in the product map.

``GET https://services.nvd.nist.gov/rest/json/cves/2.0`` with ``virtualMatchString`` (the
product's CPE) and a **last-modified** window: from the stored cursor (the end of the last
complete window; the first pull looks back ``FIRST_WINDOW_DAYS``) to now, at most the 120
days the API allows. Not the publication date: NVD adds a CVE's CPEs and score days or
weeks after publishing it, and that analysis modifies it, so a CVE is caught when it
becomes matchable. Only CVEs scoring at least ``min_cvss`` (CVSS v4.0/v3.x base score, the
highest given; ``TN_FEEDS_NVD_MIN_CVSS``, default 9.0) are kept, as ``nvd:<CVE>`` (kind
``vulnerability``) dated their publication; the content clock still ignores those
published before an item was first tracked.

The API needs no key. Without one it allows 5 requests in 30 seconds, so requests are
spaced ``6.5`` seconds apart; with a key (``TN_FEEDS_NVD_API_KEY``, sent as the ``apiKey``
header and never logged or shown) 50, spaced ``0.7`` seconds. A product whose request
fails is reported and the cursor is not moved, so the next pull covers the window again.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from .base import BaseFeedSource, FeedEntry, FeedError, FeedMalformedError, FeedProduct, FeedResult, cpe_key

URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
FIRST_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 120
PAGE_SIZE = 2000
SPACING_PUBLIC = 6.5
SPACING_WITH_KEY = 0.7


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _nvd_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000")


def _published(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def score(cve: dict) -> tuple[float | None, str]:
    """The highest base score the CVE's metrics give, and its severity."""
    best: tuple[float | None, str] = (None, "")
    metrics = cve.get("metrics") if isinstance(cve.get("metrics"), dict) else {}
    for name in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
        for m in metrics.get(name) or []:
            data = m.get("cvssData") if isinstance(m, dict) else None
            if not isinstance(data, dict):
                continue
            try:
                s = float(data.get("baseScore"))
            except (TypeError, ValueError):
                continue
            if best[0] is None or s > best[0]:
                best = (s, str(data.get("baseSeverity") or m.get("baseSeverity") or ""))
    return best


class NvdSource(BaseFeedSource):
    name = "nvd"
    label = "NVD CVE API 2.0"

    def __init__(self, *, api_key: str | None = None, min_cvss: float = 9.0, url: str = URL,
                 sleep: Callable[[float], None] | None = None):
        self._api_key = (api_key or "").strip() or None
        self.min_cvss = min_cvss
        self.url = url
        self._sleep = sleep
        self._requests = 0

    def __repr__(self) -> str:  # never the key
        return f"NvdSource(min_cvss={self.min_cvss}, api_key={'set' if self._api_key else 'none'})"

    def _get(self, http, params: dict):
        if self._requests:
            (self._sleep or _pause)(SPACING_WITH_KEY if self._api_key else SPACING_PUBLIC)
        self._requests += 1
        return http.get_json(self.url, params=params, headers={"apiKey": self._api_key} if self._api_key else None)

    def window(self, since: datetime | None, now: datetime) -> tuple[datetime, datetime]:
        start = since or (now - timedelta(days=FIRST_WINDOW_DAYS))
        return max(start, now - timedelta(days=MAX_WINDOW_DAYS)), now

    def fetch(self, http, products: Sequence[FeedProduct], *, since: datetime | None, now: datetime) -> FeedResult:
        cpes = sorted({p.cpe.strip() for p in products if p.cpe})
        start, end = self.window(since, now)
        out = FeedResult()
        if not cpes:
            out.cursor = end.isoformat()
            return out
        found: dict[str, FeedEntry] = {}
        for cpe in cpes:
            try:
                for cve in self._pages(http, cpe, start, end):
                    entry = self.entry(cve, cpe_key(cpe))
                    if entry is None:
                        continue
                    if entry.key in found:  # one CVE, several products
                        prev = found[entry.key]
                        entry = FeedEntry(**{**prev.__dict__, "products": tuple(sorted({*prev.products,
                                                                                          *entry.products}))})
                    found[entry.key] = entry
            except FeedError as exc:
                out.errors.append(f"{cpe}: {exc}")
        if len(out.errors) == len(cpes):
            raise FeedError("; ".join(out.errors))
        out.entries = list(found.values())
        out.cursor = None if out.errors else end.isoformat()
        return out

    def _pages(self, http, cpe: str, start: datetime, end: datetime):
        index = 0
        while True:
            doc = self._get(http, {"virtualMatchString": cpe, "lastModStartDate": _nvd_time(start),
                                   "lastModEndDate": _nvd_time(end), "resultsPerPage": PAGE_SIZE,
                                   "startIndex": index})
            vulns = doc.get("vulnerabilities") if isinstance(doc, dict) else None
            if not isinstance(vulns, list):
                raise FeedMalformedError("the NVD answer has no vulnerabilities list")
            for v in vulns:
                if isinstance(v, dict) and isinstance(v.get("cve"), dict):
                    yield v["cve"]
            index += len(vulns)
            try:
                total = int(doc.get("totalResults", 0))
            except (TypeError, ValueError):
                total = 0
            if not vulns or index >= total:
                return

    def entry(self, cve: dict, product: str) -> FeedEntry | None:
        cve_id = str(cve.get("id") or "")
        if not cve_id.startswith("CVE-") or str(cve.get("vulnStatus", "")).lower() == "rejected":
            return None
        s, severity = score(cve)
        if s is None or s < self.min_cvss:
            return None
        published = _published(cve.get("published"))
        if published is None:
            return None
        summary = next((d.get("value", "") for d in cve.get("descriptions") or []
                        if isinstance(d, dict) and d.get("lang") == "en"), "")
        return FeedEntry(key=f"nvd:{cve_id}", source="nvd", kind="vulnerability", products=(product,), at=published,
                         title=str(summary)[:500],
                         data={"cve": cve_id, "cvss": s, "severity": severity.upper()})
