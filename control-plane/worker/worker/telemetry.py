"""Range telemetry as the event store keeps it, stamped with the server's own ingest time.

``truenorth.ingested_at`` is the clock detection credit trusts (ADR 0005): the exercise
window is matched on it, never on the sender's ``@timestamp``. Whatever a sender put under
``truenorth`` is dropped first, so no sender can place an event inside a window.

``telemetry_tasks.ingest_telemetry_batch`` writes with these: the range's index, each event
stamped, and the store's credentials (OPENSEARCH_USER / OPENSEARCH_PASS /
OPENSEARCH_VERIFY_SSL, as the API and the scenario engine's event store read them).
"""

from __future__ import annotations

import os


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


def client_kwargs(timeout: float = 30) -> dict:
    """httpx.Client arguments for the event store: basic auth when OPENSEARCH_USER is set, and
    TLS verification from OPENSEARCH_VERIFY_SSL (true | false | a CA bundle path)."""
    kwargs: dict = {"timeout": timeout}
    if user := os.getenv("OPENSEARCH_USER"):
        kwargs["auth"] = (user, os.getenv("OPENSEARCH_PASS", ""))
    verify = os.getenv("OPENSEARCH_VERIFY_SSL", "true").strip()
    if verify.lower() in ("0", "false", "no", "off"):
        kwargs["verify"] = False
    elif verify.lower() not in ("", "1", "true", "yes", "on"):
        kwargs["verify"] = verify  # a CA bundle path
    return kwargs
