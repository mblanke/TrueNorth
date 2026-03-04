"""Telemetry ingestion pipeline — reads from Redis streams, writes to OpenSearch.

Reads events from the ``telemetry:ingest`` Redis stream, batches them, applies
transforms, and bulk-indexes into daily OpenSearch indices.

Configuration (environment variables):
    REDIS_URL               Redis connection string (default: redis://localhost:6379/0)
    OPENSEARCH_URL          OpenSearch endpoint  (default: http://localhost:9200)
    OPENSEARCH_INDEX_PREFIX  Index name prefix   (default: truenorth-telemetry)
    BATCH_SIZE              Max events per flush (default: 500)
    FLUSH_INTERVAL_MS       Max ms between flushes (default: 2000)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from opensearchpy import AsyncOpenSearch, helpers as os_helpers

from .schemas import IngestBatch, PipelineStats
from .transforms import apply_all_transforms

# ── Configuration ──────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
INDEX_PREFIX = os.getenv("OPENSEARCH_INDEX_PREFIX", "truenorth-telemetry")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500"))
FLUSH_INTERVAL_MS = int(os.getenv("FLUSH_INTERVAL_MS", "2000"))

STREAM_KEY = "telemetry:ingest"
CONSUMER_GROUP = "pipeline-workers"
CONSUMER_NAME = f"worker-{uuid.uuid4().hex[:8]}"

MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0  # seconds — exponential backoff base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("telemetry.ingest")


# ── Helpers ────────────────────────────────────────────────────────────
def _index_name() -> str:
    """Return today's index name, e.g. truenorth-telemetry-2026.02.26."""
    return f"{INDEX_PREFIX}-{datetime.now(timezone.utc).strftime('%Y.%m.%d')}"


def _parse_stream_message(msg_id: bytes, fields: dict[bytes, bytes]) -> dict[str, Any]:
    """Decode a Redis stream message into a plain dict."""
    decoded: dict[str, Any] = {}
    for k, v in fields.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        # Attempt JSON parse for nested objects
        try:
            import json
            decoded[key] = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            decoded[key] = val
    decoded["_stream_id"] = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
    return decoded


async def _ensure_consumer_group(rds: aioredis.Redis) -> None:
    """Create the consumer group if it does not already exist."""
    try:
        await rds.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("Created consumer group '%s' on stream '%s'", CONSUMER_GROUP, STREAM_KEY)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.debug("Consumer group '%s' already exists", CONSUMER_GROUP)
        else:
            raise


async def _bulk_index(os_client: AsyncOpenSearch, batch: IngestBatch) -> tuple[int, int]:
    """Bulk-index a batch of events into OpenSearch.

    Returns (success_count, error_count).
    """
    if not batch.events:
        return 0, 0

    actions = []
    idx = _index_name()
    for evt in batch.events:
        doc = evt.model_dump(mode="json")
        actions.append({"_index": idx, "_source": doc})

    success, errors = await os_helpers.async_bulk(os_client, actions, raise_on_error=False)
    error_count = len(errors) if isinstance(errors, list) else 0
    return success, error_count


# ── Main loop ──────────────────────────────────────────────────────────
class IngestPipeline:
    """Async telemetry ingestion pipeline."""

    def __init__(self) -> None:
        self._running = False
        self._stats = PipelineStats()
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._start_time = 0.0

    async def run(self) -> None:
        """Main entry point — connect and start the read/flush loop."""
        self._running = True
        self._start_time = time.monotonic()

        rds = aioredis.from_url(REDIS_URL, decode_responses=False)
        os_client = AsyncOpenSearch(
            hosts=[OPENSEARCH_URL],
            use_ssl=OPENSEARCH_URL.startswith("https"),
            verify_certs=False,
            ssl_show_warn=False,
        )

        try:
            await _ensure_consumer_group(rds)
            logger.info(
                "Pipeline started — stream=%s group=%s consumer=%s batch_size=%d flush_ms=%d",
                STREAM_KEY, CONSUMER_GROUP, CONSUMER_NAME, BATCH_SIZE, FLUSH_INTERVAL_MS,
            )

            while self._running:
                await self._read_and_process(rds, os_client)
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled, flushing remaining buffer ...")
            await self._flush(os_client)
        finally:
            await rds.aclose()
            await os_client.close()
            self._stats.uptime_seconds = time.monotonic() - self._start_time
            logger.info(
                "Pipeline stopped — received=%d indexed=%d failed=%d batches=%d uptime=%.1fs",
                self._stats.events_received,
                self._stats.events_indexed,
                self._stats.events_failed,
                self._stats.batches_flushed,
                self._stats.uptime_seconds,
            )

    async def _read_and_process(self, rds: aioredis.Redis, os_client: AsyncOpenSearch) -> None:
        """Read from Redis stream and buffer events; flush when threshold reached."""
        flush_interval_s = FLUSH_INTERVAL_MS / 1000.0
        block_ms = max(100, FLUSH_INTERVAL_MS // 2)

        try:
            messages = await rds.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {STREAM_KEY: ">"},
                count=BATCH_SIZE,
                block=block_ms,
            )
        except aioredis.ConnectionError:
            logger.warning("Redis connection lost, retrying in 2s ...")
            await asyncio.sleep(2)
            return

        if messages:
            for _stream, entries in messages:
                for msg_id, fields in entries:
                    raw = _parse_stream_message(msg_id, fields)
                    try:
                        transformed = apply_all_transforms(raw)
                        self._buffer.append(transformed)
                        self._stats.events_received += 1
                    except Exception:
                        logger.exception("Transform failed for message %s", msg_id)
                        self._stats.events_failed += 1

        # Flush if buffer is full or time elapsed
        elapsed = time.monotonic() - self._last_flush
        if len(self._buffer) >= BATCH_SIZE or (self._buffer and elapsed >= flush_interval_s):
            await self._flush(os_client)
            # ACK processed messages
            if messages:
                for _stream, entries in messages:
                    ids = [msg_id for msg_id, _ in entries]
                    if ids:
                        await rds.xack(STREAM_KEY, CONSUMER_GROUP, *ids)

    async def _flush(self, os_client: AsyncOpenSearch) -> None:
        """Flush buffered events to OpenSearch with exponential backoff retry."""
        if not self._buffer:
            return

        from .schemas import TelemetryEvent

        events_to_flush = self._buffer[:]
        self._buffer.clear()

        # Parse into TelemetryEvent models (best effort)
        parsed = []
        for raw in events_to_flush:
            try:
                parsed.append(TelemetryEvent.model_validate(raw))
            except Exception:
                # If validation fails, still try to index with raw data
                try:
                    parsed.append(TelemetryEvent(
                        event_type=raw.get("event_type", "unknown"),
                        timestamp=datetime.now(timezone.utc),
                        range_id=raw.get("range_id", "unknown"),
                        tenant_id=raw.get("tenant_id", "unknown"),
                        raw_data=raw,
                    ))
                except Exception:
                    self._stats.events_failed += 1
                    logger.warning("Skipping un-parseable event")

        batch = IngestBatch(events=parsed, batch_id=uuid.uuid4().hex)

        for attempt in range(MAX_RETRIES):
            try:
                ok, errs = await _bulk_index(os_client, batch)
                self._stats.events_indexed += ok
                self._stats.events_failed += errs
                self._stats.batches_flushed += 1
                self._stats.last_flush_at = datetime.now(timezone.utc)
                self._last_flush = time.monotonic()
                logger.info("Flushed batch %s — %d indexed, %d errors", batch.batch_id[:12], ok, errs)
                return
            except Exception:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Bulk index failed (attempt %d/%d), retrying in %.1fs ...",
                    attempt + 1, MAX_RETRIES, delay, exc_info=True,
                )
                self._stats.backpressure_pauses += 1
                await asyncio.sleep(delay)

        # All retries exhausted
        self._stats.events_failed += batch.size
        logger.error("Batch %s failed after %d retries, dropping %d events", batch.batch_id[:12], MAX_RETRIES, batch.size)

    def shutdown(self) -> None:
        """Signal the pipeline to stop gracefully."""
        logger.info("Shutdown requested")
        self._running = False


# ── Entry point ────────────────────────────────────────────────────────
def main() -> None:
    """Run the ingestion pipeline with graceful shutdown handling."""
    pipeline = IngestPipeline()
    loop = asyncio.new_event_loop()

    def _handle_signal(sig: int, _frame: Any) -> None:
        logger.info("Received signal %s", signal.Signals(sig).name)
        pipeline.shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        loop.run_until_complete(pipeline.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()