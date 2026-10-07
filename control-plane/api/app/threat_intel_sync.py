"""Store a feed pull as the feed's indicators, inside the feed's tenant.

A pull states the feed's current indicator set:

  - an indicator in the pull and already stored (same feed, type, value) is updated and
    made active again;
  - one not stored yet is created, in the feed's tenant;
  - one stored but no longer in the pull is deactivated (kept, for the record of what
    the feed once said), never deleted.

Matching is always within the one feed, so two feeds (or two tenants) reporting the same
value each keep their own row, and nothing a pull does reaches another tenant's rows.
The feed's ``last_poll_*`` and ``indicator_count`` are updated with the result, success
or failure, so the feed list shows what the last pull did.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .models import ThreatIndicator, ThreatIntelFeed
from .threat_intel_backends import FeedPull, FeedRejection

DEFAULT_CONFIDENCE = 50
DEFAULT_SEVERITY = "medium"
REJECTIONS_SHOWN = 50


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    rejected: int = 0
    rejections: list[FeedRejection] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "partial" if self.rejected else "ok"


def record_failure(feed: ThreatIntelFeed, kind: str) -> None:
    """Mark the feed's last pull as failed (``kind``: unreachable, malformed, refused)."""
    feed.last_poll_at = datetime.now(UTC)
    feed.last_poll_status = f"error: {kind}"[:50]


def apply_pull(db: Session, feed: ThreatIntelFeed, pull: FeedPull) -> SyncResult:
    """Upsert ``pull`` as ``feed``'s indicators. Flushes; the caller commits."""
    result = SyncResult(rejected=len(pull.rejected), rejections=pull.rejected[:REJECTIONS_SHOWN])
    existing = {
        (row.indicator_type, row.value): row
        for row in db.query(ThreatIndicator).filter(
            ThreatIndicator.feed_id == feed.id, ThreatIndicator.tenant_id == feed.tenant_id
        )
    }
    pulled: set[tuple[str, str]] = set()
    for ind in pull.indicators:
        key = (ind.indicator_type, ind.value)
        pulled.add(key)
        row = existing.get(key)
        if row is None:
            row = ThreatIndicator(
                feed_id=feed.id, tenant_id=feed.tenant_id, indicator_type=ind.indicator_type, value=ind.value
            )
            db.add(row)
            existing[key] = row
            result.created += 1
        else:
            result.updated += 1
        row.is_active = True
        row.name = ind.name
        row.description = ind.description
        row.confidence = ind.confidence if ind.confidence is not None else DEFAULT_CONFIDENCE
        row.severity = ind.severity or DEFAULT_SEVERITY
        row.valid_from = ind.first_seen
        row.mitre_attack_ids = json.dumps(list(ind.mitre_attack_ids)) if ind.mitre_attack_ids else None

    for key, row in existing.items():
        if key not in pulled and row.is_active:
            row.is_active = False
            result.deactivated += 1

    feed.last_poll_at = datetime.now(UTC)
    feed.last_poll_status = result.status
    feed.indicator_count = len(pulled)
    db.flush()
    return result
