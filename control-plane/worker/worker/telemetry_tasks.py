"""TrueNorth Range - telemetry batch ingest (ingest_telemetry_batch).

Moved out of tasks.py (ADR 0003). The Celery name is unchanged
(``worker.tasks.ingest_telemetry_batch``, routed to the ``telemetry`` queue), and
``from worker.tasks import ingest_telemetry_batch`` still works. Events go to the
range's own index (``range_index``), MITRE-tagged on the way (telemetry_mitre.py, the
same rules as the API's ``POST /telemetry/{range_id}/events``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from .celery_app import app
from .detection import range_index
from .telemetry_mitre import tag_event

logger = logging.getLogger("truenorth.worker")


def bulk_body(range_id: str, events: list[dict]) -> str:
    """The ``_bulk`` NDJSON for *events*: range id, timestamp and MITRE tags filled in."""
    index = range_index(range_id)
    lines = []
    for event in events:
        event.setdefault("range_id", range_id)
        event.setdefault("@timestamp", datetime.now(UTC).isoformat())
        tag_event(event)
        lines.append(json.dumps({"index": {"_index": index}}))
        lines.append(json.dumps(event))
    return "".join(line + "\n" for line in lines)


@app.task(bind=True, name="worker.tasks.ingest_telemetry_batch")
def ingest_telemetry_batch(self, range_id: str, events: list[dict]):
    """Batch-ingest telemetry events into OpenSearch.

    At scale, the API buffers events and dispatches to this task
    to avoid blocking request threads on OpenSearch I/O.
    """
    import httpx

    os_url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
    body = bulk_body(range_id, events)

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{os_url}/_bulk",
                content=body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            resp.raise_for_status()
            result = resp.json()
            errors = result.get("errors", False)
            if errors:
                failed = [item for item in result.get("items", []) if item.get("index", {}).get("error")]
                logger.warning(f"[telemetry] {len(failed)} events failed indexing")
    except Exception as e:
        logger.error(f"[telemetry] OpenSearch ingest error: {e}")
        raise

    logger.info(f"[telemetry] Ingested {len(events)} events for range {range_id}")
    return {"indexed": len(events), "range_id": range_id}
