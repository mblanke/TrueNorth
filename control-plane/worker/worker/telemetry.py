"""Range telemetry into the event store, stamped with the server's own ingest time.

``truenorth.ingested_at`` is the clock detection credit trusts (ADR 0005): the exercise
window is matched on it, never on the sender's ``@timestamp``. Whatever a sender put under
``truenorth`` is dropped first, so no sender can place an event inside a window.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import httpx

logger = logging.getLogger("truenorth.worker.telemetry")


def range_index(range_id: str) -> str:
    """The event index a range's telemetry is ingested into."""
    return f"range-{range_id}"


def stamp(event: dict, range_id: str, now: str) -> dict:
    """The event as stored: server-owned ``truenorth`` fields replace any the sender sent."""
    out = {k: v for k, v in event.items() if k != "truenorth" and not k.startswith("truenorth.")}
    out.setdefault("range_id", range_id)
    out.setdefault("@timestamp", now)
    out["truenorth"] = {"ingested_at": now, "range_id": range_id}
    return out


def _client_kwargs() -> dict:
    """OPENSEARCH_USER / OPENSEARCH_PASS / OPENSEARCH_VERIFY_SSL, as the API and the event store read them."""
    kwargs: dict = {"timeout": 30}
    if user := os.getenv("OPENSEARCH_USER"):
        kwargs["auth"] = (user, os.getenv("OPENSEARCH_PASS", ""))
    verify = os.getenv("OPENSEARCH_VERIFY_SSL", "true").strip()
    if verify.lower() in ("0", "false", "no", "off"):
        kwargs["verify"] = False
    elif verify.lower() not in ("", "1", "true", "yes", "on"):
        kwargs["verify"] = verify  # a CA bundle path
    return kwargs


def bulk_ingest(range_id: str, events: list[dict]) -> int:
    """Index ``events`` into the range's index; return how many were sent. Raises on a store error."""
    now = datetime.now(UTC).isoformat()
    index = range_index(range_id)
    body = "".join(
        json.dumps({"index": {"_index": index}}) + "\n" + json.dumps(stamp(e, range_id, now), default=str) + "\n"
        for e in events
    )
    url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200").rstrip("/")
    with httpx.Client(**_client_kwargs()) as client:
        resp = client.post(f"{url}/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"})
        resp.raise_for_status()
        result = resp.json()
    if result.get("errors"):
        failed = [i for i in result.get("items", []) if i.get("index", {}).get("error")]
        logger.warning("[telemetry] %d events failed indexing", len(failed))
    return len(events)
