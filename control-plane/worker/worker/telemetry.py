"""Range telemetry as the event store keeps it, stamped with the server's own ingest time.

``truenorth.ingested_at`` is the clock detection credit trusts (ADR 0005): the exercise
window is matched on it, never on the sender's ``@timestamp``. Whatever a sender put under
``truenorth`` is dropped first, so no sender can place an event inside a window.

``bulk_ingest`` (the worker's write to the store, replacing the inline one in
``tasks.ingest_telemetry_batch``) lands with the tasks.py half of ADR 0005:
``.agent-patches/s4-detection-tasks.patch``.
"""

from __future__ import annotations


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
