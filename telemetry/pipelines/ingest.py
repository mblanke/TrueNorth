"""Telemetry event ingestion — async bulk ingest to OpenSearch.

Features:
- Async event buffer with flush-on-threshold (1 000 events or 5 s)
- Bulk API batching
- Dead-letter queue for failed events
- Metric tracking (events/sec, errors, batch sizes)

Usage::

    ingestor = EventIngestor("http://opensearch:9200")
    await ingestor.start()
    await ingestor.ingest("range_event", {"range_id": "r1", ...})
    await ingestor.flush()
    await ingestor.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

logger = logging.getLogger("telemetry.ingest")

EventType = Literal["range_event", "exercise_event", "security_event", "network_event"]

INDEX_MAP: dict[EventType, str] = {
    "range_event": "range-events",
    "exercise_event": "exercise-events",
    "security_event": "security-events",
    "network_event": "network-events",
}

PIPELINE_MAP: dict[EventType, str] = {
    "range_event": "range-events-pipeline",
    "security_event": "security-events-pipeline",
}


@dataclass
class IngestMetrics:
    """Counters for observability."""

    events_received: int = 0
    events_indexed: int = 0
    events_failed: int = 0
    batches_sent: int = 0
    bytes_sent: int = 0
    last_flush_time: float = 0.0
    _start_time: float = field(default_factory=time.monotonic)

    @property
    def events_per_second(self) -> float:
        elapsed = time.monotonic() - self._start_time
        return self.events_indexed / elapsed if elapsed > 0 else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "events_received": self.events_received,
            "events_indexed": self.events_indexed,
            "events_failed": self.events_failed,
            "batches_sent": self.batches_sent,
            "bytes_sent": self.bytes_sent,
            "events_per_second": round(self.events_per_second, 2),
        }


class DeadLetterQueue:
    """In-memory DLQ with optional file persistence."""

    def __init__(self, max_size: int = 10_000, persist_path: str | None = None):
        self._queue: deque[dict[str, Any]] = deque(maxlen=max_size)
        self._persist_path = persist_path

    def put(self, event: dict[str, Any], error: str) -> None:
        entry = {
            "event": event,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._queue.append(entry)
        if self._persist_path:
            self._persist(entry)

    def drain(self, max_items: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for _ in range(min(max_items, len(self._queue))):
            items.append(self._queue.popleft())
        return items

    @property
    def size(self) -> int:
        return len(self._queue)

    def _persist(self, entry: dict[str, Any]) -> None:
        try:
            with open(self._persist_path, "a") as f:  # type: ignore[arg-type]
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("DLQ persist failed: %s", exc)


class EventIngestor:
    """Async bulk event ingestor for OpenSearch.

    Parameters
    ----------
    opensearch_url : str
        Base OpenSearch URL.
    batch_size : int
        Flush after this many buffered events (default 1000).
    flush_interval : float
        Seconds between automatic flushes (default 5.0).
    dlq_path : str | None
        Optional path for dead-letter file persistence.
    """

    def __init__(
        self,
        opensearch_url: str | None = None,
        batch_size: int = 1000,
        flush_interval: float = 5.0,
        dlq_path: str | None = None,
    ):
        self._url = (opensearch_url or os.getenv("OPENSEARCH_URL", "http://localhost:9200")).rstrip("/")
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: list[tuple[EventType, dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._running = False
        self._client: httpx.AsyncClient | None = None
        self.metrics = IngestMetrics()
        self.dlq = DeadLetterQueue(persist_path=dlq_path)

    async def start(self) -> None:
        """Start the periodic flush loop."""
        self._client = httpx.AsyncClient(base_url=self._url, timeout=30.0)
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("EventIngestor started — batch=%d interval=%.1fs", self._batch_size, self._flush_interval)

    async def stop(self) -> None:
        """Flush remaining events and shut down."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
        if self._client:
            await self._client.aclose()
        logger.info("EventIngestor stopped — %s", json.dumps(self.metrics.snapshot()))

    async def ingest(self, event_type: EventType, event: dict[str, Any]) -> None:
        """Buffer a single event for bulk indexing."""
        if "@timestamp" not in event:
            event["@timestamp"] = datetime.now(timezone.utc).isoformat()
        self.metrics.events_received += 1
        async with self._lock:
            self._buffer.append((event_type, event))
            if len(self._buffer) >= self._batch_size:
                await self._flush_buffer()

    async def ingest_batch(self, event_type: EventType, events: list[dict[str, Any]]) -> None:
        """Buffer multiple events at once."""
        for event in events:
            await self.ingest(event_type, event)

    async def flush(self) -> None:
        """Force-flush the buffer."""
        async with self._lock:
            await self._flush_buffer()

    async def _periodic_flush(self) -> None:
        """Background task that flushes on interval."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            if self._buffer:
                async with self._lock:
                    await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Send buffered events via bulk API."""
        if not self._buffer:
            return

        batch = list(self._buffer)
        self._buffer.clear()

        ndjson_lines: list[str] = []
        for event_type, event in batch:
            index_alias = INDEX_MAP.get(event_type, "range-events")
            action = {"index": {"_index": index_alias}}
            pipeline = PIPELINE_MAP.get(event_type)
            if pipeline:
                action["index"]["pipeline"] = pipeline
            ndjson_lines.append(json.dumps(action))
            ndjson_lines.append(json.dumps(event))

        body = "\n".join(ndjson_lines) + "\n"
        body_bytes = len(body.encode("utf-8"))

        try:
            assert self._client is not None
            resp = await self._client.post(
                "/_bulk",
                content=body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            result = resp.json()

            self.metrics.batches_sent += 1
            self.metrics.bytes_sent += body_bytes
            self.metrics.last_flush_time = time.monotonic()

            if result.get("errors"):
                for i, item in enumerate(result.get("items", [])):
                    idx_result = item.get("index", {})
                    if idx_result.get("status", 200) >= 400:
                        self.metrics.events_failed += 1
                        self.dlq.put(batch[i][1], str(idx_result.get("error", "unknown")))
                    else:
                        self.metrics.events_indexed += 1
            else:
                self.metrics.events_indexed += len(batch)

            logger.debug("Bulk flush: %d events, %d bytes, errors=%s", len(batch), body_bytes, result.get("errors"))

        except Exception as exc:
            logger.error("Bulk ingest failed: %s", exc)
            self.metrics.events_failed += len(batch)
            for event_type, event in batch:
                self.dlq.put(event, str(exc))