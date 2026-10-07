"""TrueNorth Range — threat intelligence feed backend interface (ADR 0001).

A feed backend turns a feed source (a URL it fetches, or content an operator uploaded)
into indicator records. It never touches the database: storing them, tenant-scoped, is
``app/threat_intel_sync.py``'s job, so every backend gets the same upsert and isolation.

Errors are typed so the API can answer the right status without knowing the backend:

  FeedSourceError   the request cannot be served as asked (no URL, a refused destination,
                    an upload to a backend that only fetches)                   -> 422
  FeedMalformedError     the source answered but is not a feed this backend reads    -> 422
  FeedUnreachableError   the source could not be fetched (DNS, refused, timeout, non-2xx) -> 502
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class FeedError(RuntimeError):
    """Base for every feed backend failure."""


class FeedSourceError(FeedError, ValueError):
    """The pull cannot be done as asked (the caller's or the feed's configuration)."""


class FeedMalformedError(FeedError, ValueError):
    """The content is not a feed this backend can read at all (not a bad row: a bad feed)."""


class FeedUnreachableError(FeedError):
    """The feed source could not be fetched."""


@dataclass(frozen=True)
class FeedIndicator:
    """One indicator as a feed states it. ``indicator_type`` and ``value`` are normalised."""

    indicator_type: str
    value: str
    first_seen: datetime | None = None
    mitre_attack_ids: tuple[str, ...] = ()
    severity: str | None = None
    confidence: int | None = None
    name: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class FeedRejection:
    """A row the backend refused, and why. ``row`` is 1-based, counting the header as row 1.
    The reason never repeats the row's content, so a fetched page cannot be read back
    through rejection messages."""

    row: int
    reason: str


@dataclass
class FeedPull:
    indicators: list[FeedIndicator] = field(default_factory=list)
    rejected: list[FeedRejection] = field(default_factory=list)


class BaseFeedBackend(ABC):
    """What every threat intelligence feed backend must do."""

    #: True when the backend reads operator-uploaded content (``fetch(content=...)``).
    accepts_upload: bool = False

    @abstractmethod
    def fetch(self, url: str | None, content: bytes | None = None) -> FeedPull:
        """Indicators from ``content`` when given, otherwise from ``url``.

        Raises ``FeedSourceError``, ``FeedMalformedError`` or ``FeedUnreachableError``; never returns
        a partial pull for a feed it could not read. Rows it cannot use are returned in
        ``FeedPull.rejected``, not raised.
        """
        ...

    def health_check(self) -> bool:
        return True
