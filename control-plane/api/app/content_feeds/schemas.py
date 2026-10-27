"""Wire shapes of the content feeds (ADR 0014 decision 25). Kept out of ``app.schemas`` (ADR 0003).

A **signed feed bundle** is one JSON document::

    {"format": "truenorth-feed-bundle/1",
     "generated_at": "2026-10-27T04:00:00+00:00",
     "entries": [{"key": "eol:ubuntu:20.04", "source": "endoflife", "kind": "release",
                  "products": ["eol:ubuntu:20.04"], "at": "2025-05-31T00:00:00+00:00",
                  "title": "ubuntu 20.04 end of life", "data": {"eol": "2025-05-31", ...}}, ...],
     "signature": {"alg": "Ed25519", "key_id": "...", "public_key": "...", "signed_sha256": "...",
                   "value": "..."}}

``entries`` are what the online pull stores (``worker/feed_backends/base.py`` ``FeedEntry``).
The signature is the range certification export's construction (Ed25519 over the canonical
JSON of the document without ``signature``; ``app/range_certification/signing.py``),
verified against the site's configured public key, never the one the bundle carries.
``scripts/build_feed_bundle.py`` makes one on a connected machine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

BUNDLE_FORMAT = "truenorth-feed-bundle/1"
MAX_ENTRIES = 100_000
FeedMode = Literal["off", "online", "bundle"]
SourceName = Literal["endoflife", "cisa_kev", "nvd"]
# Which key prefixes each source makes (the content clock's matching reads the prefix).
PREFIXES: dict[str, tuple[str, ...]] = {"endoflife": ("eol:", "release:"), "cisa_kev": ("kev:",), "nvd": ("nvd:",)}


class FeedEntryIn(BaseModel):
    key: str = Field(min_length=5, max_length=200)
    source: SourceName
    kind: Literal["release", "vulnerability"]
    products: list[str] = Field(min_length=1, max_length=50)
    at: datetime
    title: str = Field("", max_length=500)
    data: dict = Field(default_factory=dict)

    @field_validator("products")
    @classmethod
    def _products(cls, value: list[str]) -> list[str]:
        if any(not p or len(p) > 200 for p in value):
            raise ValueError("each product key is 1 to 200 characters")
        return value

    @model_validator(mode="after")
    def _prefix(self) -> FeedEntryIn:
        if not self.key.startswith(PREFIXES[self.source]):
            raise ValueError(f"a {self.source} entry's key starts with {' or '.join(PREFIXES[self.source])}")
        if self.at.tzinfo is None:
            raise ValueError("at carries a timezone")
        return self


class FeedBundleIn(BaseModel):
    format: Literal["truenorth-feed-bundle/1"]
    generated_at: datetime
    entries: list[FeedEntryIn] = Field(max_length=MAX_ENTRIES)


class FeedSourceOut(BaseModel):
    source: str
    label: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str = ""
    entries: int = Field(0, description="Entries the last pull or import brought")
    generated_at: datetime | None = Field(None, description="Bundle: when it was made")
    key_id: str | None = Field(None, description="Bundle: the key that signed it")


class FeedStatusOut(BaseModel):
    mode: FeedMode = Field(description="off (the default), online (the daily pull) or bundle (air-gapped imports)")
    can_import: bool = Field(description="The caller may import a signed feed bundle now")
    bundle_key_id: str | None = Field(None, description="The configured bundle public key's id")
    bundle_key_problem: str | None = Field(None, description="Why bundles cannot be imported, when they cannot")
    sources: list[FeedSourceOut]
    entries: int = Field(description="Feed entries stored")
    flagged_items: int = Field(description="Content the caller can see that a feed has flagged")


class FeedImportOut(BaseModel):
    entries: int
    new: int
    updated: int
    generated_at: datetime
    key_id: str
