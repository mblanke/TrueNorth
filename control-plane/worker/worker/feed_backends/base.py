"""The content feeds' adapter interface (ADR 0001; ADR 0014 decision 25, issue #287).

A feed source reads one public release or vulnerability feed (endoflife.date, CISA KEV,
the NVD CVE API) and turns what it says about the products the golden templates are built
from into ``FeedEntry`` records. It never touches the database: storing the entries is
``content_feeds/store.py``'s job, and turning them into stale flags is the content clock's
sync (``content_review_sync.py``), so a pull and a signed bundle import go through the
same pipeline.

A source only ever sends GET requests, through ``FeedHttp`` (``http.py``), and never sends
anything of the site's: no tenant data, no content names. What it asks for is the public
product names in the product map (``content/catalogue/feed_products.yaml``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

KIND_RELEASE = "release"
KIND_VULNERABILITY = "vulnerability"
KINDS = (KIND_RELEASE, KIND_VULNERABILITY)


class FeedError(RuntimeError):
    """Base for every feed source failure. The message is shown on the feeds status panel:
    it names the host and what went wrong, never a URL with its query, a header or a key."""


class FeedUnreachableError(FeedError):
    """The feed could not be fetched (DNS, refused, timeout, proxy, non-2xx)."""


class FeedMalformedError(FeedError, ValueError):
    """The feed answered with something that is not the feed."""


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def eol_key(product: str, cycle: str) -> str:
    return f"eol:{_norm(product)}:{_norm(cycle)}"


def kev_key(vendor: str, product: str) -> str:
    return f"kev:{_norm(vendor)}:{_norm(product)}"


def cpe_key(cpe: str) -> str:
    return f"cpe:{_norm(cpe)}"


@dataclass(frozen=True)
class FeedProduct:
    """One thing a golden template is built from, as the feeds name it.

    ``name`` is what the Content view says ("Ubuntu 24.04"). ``eol_product``/``eol_cycle``
    are endoflife.date's product and release cycle; ``release`` the point release the
    template's ISO is (a newer one in the same cycle raises a release flag). ``cpe`` is the
    NVD CPE match string (vendor, product, version). ``kev`` pairs are CISA KEV's
    ``vendorProject`` and ``product``.
    """

    name: str
    eol_product: str | None = None
    eol_cycle: str | None = None
    release: str | None = None
    cpe: str | None = None
    kev: tuple[tuple[str, str], ...] = ()

    def product_keys(self) -> set[str]:
        out = set()
        if self.eol_product and self.eol_cycle:
            out.add(eol_key(self.eol_product, self.eol_cycle))
        if self.cpe:
            out.add(cpe_key(self.cpe))
        out.update(kev_key(v, p) for v, p in self.kev)
        return out


@dataclass(frozen=True)
class FeedEntry:
    """One fact a feed states, normalised. The same shape travels in a signed feed bundle.

    ``key`` is unique across every feed ("eol:ubuntu:20.04", "release:ubuntu:24.04:24.04.6",
    "kev:CVE-2024-1234", "nvd:CVE-2024-1234"). ``products`` are the product keys it is
    about (``FeedProduct.product_keys``). ``at`` is when it took effect: the end-of-life date, the
    release date, the date CISA added it, the date NVD published it.
    """

    key: str
    source: str
    kind: str
    products: tuple[str, ...]
    at: datetime
    title: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "source": self.source, "kind": self.kind, "products": sorted(self.products),
                "at": self.at.astimezone(UTC).isoformat(), "title": self.title, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, raw: dict) -> FeedEntry:
        if not isinstance(raw, dict):
            raise FeedMalformedError("a feed entry is not an object")
        try:
            at = datetime.fromisoformat(str(raw["at"]))
            products = tuple(str(p) for p in raw["products"])
            entry = cls(key=str(raw["key"]), source=str(raw["source"]), kind=str(raw["kind"]), products=products,
                        at=at if at.tzinfo else at.replace(tzinfo=UTC), title=str(raw.get("title") or ""),
                        data=dict(raw.get("data") or {}))
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedMalformedError("a feed entry lacks key, source, kind, products or at") from exc
        if entry.kind not in KINDS or not entry.key or not entry.products:
            raise FeedMalformedError(f"feed entry {entry.key!r} is not a release or vulnerability entry")
        return entry


@dataclass
class FeedResult:
    """What one pull of one source gave. ``cursor``: where the next pull starts (the NVD's
    publication window); None keeps the stored one. ``errors``: per-product problems that
    did not stop the rest (a product the feed does not know)."""

    entries: list[FeedEntry] = field(default_factory=list)
    cursor: str | None = None
    errors: list[str] = field(default_factory=list)


class BaseFeedSource(ABC):
    """What every content feed source must do."""

    #: The registry key, and the ``source`` of the entries it makes.
    name: str = ""
    #: What the feeds status panel calls it.
    label: str = ""

    @abstractmethod
    def fetch(self, http, products: Sequence[FeedProduct], *, since: datetime | None,
              now: datetime) -> FeedResult:
        """The entries the feed has about ``products``. ``http`` is a ``FeedHttp`` (GET
        only); ``since`` is the stored cursor's time (None: first pull).

        Raises ``FeedError`` when the feed as a whole could not be read; never returns a
        partial pull of a feed it could not read."""
        ...

    def health_check(self) -> bool:
        return True
