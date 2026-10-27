"""TrueNorth Range — content feed source registry (ADR 0001; ADR 0014 decision 25).

Usage:
    from worker.feed_backends import ONLINE_SOURCES, get_feed_source

    for name in ONLINE_SOURCES:
        result = get_feed_source(name).fetch(http, products, since=cursor, now=now)

Sources:
    endoflife — endoflife.date: release cycles, end-of-life dates, latest point releases
    cisa_kev  — CISA's Known Exploited Vulnerabilities catalogue
    nvd       — the NVD CVE API 2.0 (key optional: TN_FEEDS_NVD_API_KEY)
    null      — fetches nothing (offline sites, tests)

Adding a source:
    1. Create feed_backends/<name>.py implementing BaseFeedSource
    2. Add it to _REGISTRY (and ONLINE_SOURCES if the daily pull should read it)
    3. tests/contracts/test_adapter_contracts.py enrols it automatically
"""

from __future__ import annotations

import os

from .base import (
    BaseFeedSource,
    FeedEntry,
    FeedError,
    FeedMalformedError,
    FeedProduct,
    FeedResult,
    FeedUnreachableError,
)
from .cisa_kev import CisaKevSource
from .endoflife import EndOfLifeSource
from .http import FeedHttp
from .null import NullFeedSource
from .nvd import NvdSource

__all__ = [
    "ONLINE_SOURCES",
    "BaseFeedSource",
    "FeedEntry",
    "FeedError",
    "FeedHttp",
    "FeedMalformedError",
    "FeedProduct",
    "FeedResult",
    "FeedUnreachableError",
    "get_feed_source",
]

DEFAULT_MIN_CVSS = 9.0


def min_cvss() -> float:
    try:
        value = float(os.getenv("TN_FEEDS_NVD_MIN_CVSS", "").strip() or DEFAULT_MIN_CVSS)
    except ValueError:
        return DEFAULT_MIN_CVSS
    return min(max(value, 0.0), 10.0)


def _nvd() -> NvdSource:
    return NvdSource(api_key=os.getenv("TN_FEEDS_NVD_API_KEY", ""), min_cvss=min_cvss())


_REGISTRY = {
    "endoflife": EndOfLifeSource,
    "cisa_kev": CisaKevSource,
    "nvd": _nvd,
    "null": NullFeedSource,
}

# What the daily pull reads, in this order.
ONLINE_SOURCES = ("endoflife", "cisa_kev", "nvd")


def get_feed_source(name: str) -> BaseFeedSource:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown content feed source {name!r}; known: {', '.join(sorted(_REGISTRY))}") from None
    return factory()
