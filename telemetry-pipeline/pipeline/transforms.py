"""Event transformation and enrichment functions for the telemetry pipeline."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def normalize_timestamp(event: dict[str, Any]) -> dict[str, Any]:
    """Ensure timestamp is ISO-8601 and add @timestamp field for OpenSearch."""
    raw_ts = event.get("timestamp")
    if raw_ts is None:
        event["timestamp"] = datetime.now(UTC).isoformat()
    elif isinstance(raw_ts, (int, float)):
        # Assume epoch seconds
        event["timestamp"] = datetime.fromtimestamp(raw_ts, tz=UTC).isoformat()
    elif isinstance(raw_ts, str):
        # Try to parse and re-format to ensure consistency
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            event["timestamp"] = dt.isoformat()
        except ValueError:
            logger.warning("Unparseable timestamp '%s', using current time", raw_ts)
            event["timestamp"] = datetime.now(UTC).isoformat()

    event["@timestamp"] = event["timestamp"]
    return event


def enrich_metadata(event: dict[str, Any]) -> dict[str, Any]:
    """Pull range_id and tenant_id from nested metadata if present."""
    metadata = event.get("metadata") or event.get("raw_data", {}).get("metadata", {})
    if metadata:
        if "range_id" not in event and "range_id" in metadata:
            event["range_id"] = metadata["range_id"]
        if "tenant_id" not in event and "tenant_id" in metadata:
            event["tenant_id"] = metadata["tenant_id"]
    return event


def enrich_geoip(event: dict[str, Any]) -> dict[str, Any]:
    """Stub: GeoIP enrichment for source_ip and dest_ip.

    In production this would use a MaxMind GeoLite2 database or similar
    service to resolve IP addresses to geographic locations.
    """
    for ip_field in ("source_ip", "dest_ip"):
        ip_addr = event.get(ip_field)
        if ip_addr and not ip_addr.startswith(("10.", "172.", "192.168.", "127.")):
            # Placeholder — real implementation would do GeoIP lookup
            event.setdefault("geo", {})[ip_field] = {
                "country": "UNKNOWN",
                "city": "UNKNOWN",
                "lat": 0.0,
                "lon": 0.0,
            }
    return event


def tag_mitre_attack(event: dict[str, Any]) -> dict[str, Any]:
    """Stub: Tag events with MITRE ATT&CK technique IDs based on event_type.

    A production implementation would use a rules engine or ML classifier.
    This stub provides basic keyword-based mapping for common event types.
    """
    technique_map: dict[str, list[str]] = {
        "dns_query": ["T1071.004"],       # Application Layer Protocol: DNS
        "http_request": ["T1071.001"],    # Application Layer Protocol: Web
        "process_exec": ["T1059"],        # Command and Scripting Interpreter
        "file_create": ["T1105"],         # Ingress Tool Transfer
        "registry_mod": ["T1112"],        # Modify Registry
        "login_attempt": ["T1110"],       # Brute Force
        "lateral_move": ["T1021"],        # Remote Services
        "c2_beacon": ["T1071", "T1573"],  # App Layer Protocol + Encrypted Channel
        "data_exfil": ["T1041"],          # Exfiltration Over C2 Channel
    }
    event_type = event.get("event_type", "")
    matched = technique_map.get(event_type, [])
    if matched:
        existing_tags = set(event.get("tags", []))
        existing_tags.update(matched)
        event["tags"] = sorted(existing_tags)
    return event


def apply_all_transforms(event: dict[str, Any]) -> dict[str, Any]:
    """Apply the full transform pipeline to a single event."""
    event = normalize_timestamp(event)
    event = enrich_metadata(event)
    event = enrich_geoip(event)
    event = tag_mitre_attack(event)
    return event
