"""TrueNorth Range — Telemetry Pipelines.

Modules:
    bootstrap   — OpenSearch index templates, ILM policies, ingest pipelines
    ingest      — Async bulk event ingestion with DLQ
    enrichment  — MITRE ATT&CK, GeoIP, user/range context enrichment
    correlator  — Pattern-based event correlation and alert generation
"""

from .bootstrap import bootstrap
from .correlator import CorrelationEngine, CorrelationRule
from .enrichment import enrich_event, enrich_geoip, enrich_mitre, normalise_timestamp
from .ingest import DeadLetterQueue, EventIngestor, IngestMetrics

__all__ = [
    "bootstrap",
    "EventIngestor",
    "IngestMetrics",
    "DeadLetterQueue",
    "enrich_event",
    "enrich_mitre",
    "enrich_geoip",
    "normalise_timestamp",
    "CorrelationEngine",
    "CorrelationRule",
]
