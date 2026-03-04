#!/usr/bin/env python3
"""Bootstrap OpenSearch with index templates, ILM policies, ingest pipelines, and dashboards.

Idempotent — safe to run multiple times.  Checks existence before creating.

Usage:
    python -m telemetry.pipelines.bootstrap --opensearch-url http://localhost:9200
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("telemetry.bootstrap")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")

# ═══════════════════════════════════════════════════════════════════
#  ILM (ISM) Policies
# ═══════════════════════════════════════════════════════════════════

ISM_POLICY: dict[str, Any] = {
    "policy": {
        "description": "TrueNorth Range event lifecycle — hot/warm/cold/delete",
        "default_state": "hot",
        "states": [
            {
                "name": "hot",
                "actions": [
                    {
                        "rollover": {
                            "min_index_age": "7d",
                            "min_primary_shard_size": "50gb",
                        }
                    }
                ],
                "transitions": [{"state_name": "warm", "conditions": {"min_index_age": "7d"}}],
            },
            {
                "name": "warm",
                "actions": [
                    {"replica_count": {"number_of_replicas": 1}},
                    {"force_merge": {"max_num_segments": 1}},
                ],
                "transitions": [{"state_name": "cold", "conditions": {"min_index_age": "30d"}}],
            },
            {
                "name": "cold",
                "actions": [
                    {"read_only": {}},
                    {"replica_count": {"number_of_replicas": 0}},
                ],
                "transitions": [{"state_name": "delete", "conditions": {"min_index_age": "90d"}}],
            },
            {
                "name": "delete",
                "actions": [{"delete": {}}],
                "transitions": [],
            },
        ],
        "ism_template": [
            {"index_patterns": ["range-events-*", "exercise-events-*", "security-events-*", "network-events-*", "system-events-*"]},
        ],
    }
}

# ═══════════════════════════════════════════════════════════════════
#  Index Templates
# ═══════════════════════════════════════════════════════════════════

_COMMON_MAPPINGS: dict[str, Any] = {
    "@timestamp": {"type": "date"},
    "range_id": {"type": "keyword"},
    "tenant_id": {"type": "keyword"},
    "exercise_id": {"type": "keyword"},
    "event_type": {"type": "keyword"},
    "source_ip": {"type": "ip"},
    "dest_ip": {"type": "ip"},
    "source_port": {"type": "integer"},
    "dest_port": {"type": "integer"},
    "protocol": {"type": "keyword"},
    "action": {"type": "keyword"},
    "severity": {"type": "keyword"},
    "hostname": {"type": "keyword"},
    "username": {"type": "keyword"},
    "raw_log": {"type": "text"},
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "range-events": {
        "index_patterns": ["range-events-*"],
        "template": {
            "settings": {
                "number_of_shards": 2,
                "number_of_replicas": 1,
                "index.refresh_interval": "5s",
                "plugins.index_state_management.rollover_alias": "range-events",
            },
            "mappings": {
                "properties": {
                    **_COMMON_MAPPINGS,
                    "inject": {"type": "boolean"},
                    "process_name": {"type": "keyword"},
                    "process_id": {"type": "integer"},
                    "parent_process": {"type": "keyword"},
                    "command_line": {"type": "text"},
                    "file_path": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "hash_md5": {"type": "keyword"},
                    "hash_sha256": {"type": "keyword"},
                    "geo": {
                        "properties": {
                            "country_iso_code": {"type": "keyword"},
                            "city_name": {"type": "keyword"},
                            "location": {"type": "geo_point"},
                        }
                    },
                    "user_agent": {
                        "properties": {
                            "original": {"type": "text"},
                            "name": {"type": "keyword"},
                            "os": {"type": "keyword"},
                        }
                    },
                }
            },
        },
    },
    "exercise-events": {
        "index_patterns": ["exercise-events-*"],
        "template": {
            "settings": {
                "number_of_shards": 2,
                "number_of_replicas": 1,
                "index.refresh_interval": "5s",
                "plugins.index_state_management.rollover_alias": "exercise-events",
            },
            "mappings": {
                "properties": {
                    **_COMMON_MAPPINGS,
                    "objective_ref": {"type": "keyword"},
                    "score_delta": {"type": "float"},
                    "team_id": {"type": "keyword"},
                    "phase": {"type": "keyword"},
                }
            },
        },
    },
    "security-events": {
        "index_patterns": ["security-events-*"],
        "template": {
            "settings": {
                "number_of_shards": 3,
                "number_of_replicas": 1,
                "index.refresh_interval": "5s",
                "plugins.index_state_management.rollover_alias": "security-events",
            },
            "mappings": {
                "properties": {
                    **_COMMON_MAPPINGS,
                    "mitre_technique": {"type": "keyword"},
                    "mitre_tactic": {"type": "keyword"},
                    "mitre_technique_name": {"type": "keyword"},
                    "alert_name": {"type": "keyword"},
                    "alert_severity": {"type": "keyword"},
                    "detection_rule": {"type": "keyword"},
                    "confidence": {"type": "float"},
                    "ioc_type": {"type": "keyword"},
                    "ioc_value": {"type": "keyword"},
                }
            },
        },
    },
    "network-events": {
        "index_patterns": ["network-events-*"],
        "template": {
            "settings": {
                "number_of_shards": 3,
                "number_of_replicas": 1,
                "index.refresh_interval": "5s",
                "plugins.index_state_management.rollover_alias": "network-events",
            },
            "mappings": {
                "properties": {
                    **_COMMON_MAPPINGS,
                    "bytes_in": {"type": "long"},
                    "bytes_out": {"type": "long"},
                    "packets_in": {"type": "long"},
                    "packets_out": {"type": "long"},
                    "dns_query": {"type": "keyword"},
                    "dns_type": {"type": "keyword"},
                    "http_method": {"type": "keyword"},
                    "http_status": {"type": "integer"},
                    "url": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "tls_version": {"type": "keyword"},
                    "tls_cipher": {"type": "keyword"},
                    "geo": {
                        "properties": {
                            "country_iso_code": {"type": "keyword"},
                            "city_name": {"type": "keyword"},
                            "location": {"type": "geo_point"},
                        }
                    },
                }
            },
        },
    },
    "system-events": {
        "index_patterns": ["system-events-*"],
        "template": {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
                "index.refresh_interval": "10s",
                "plugins.index_state_management.rollover_alias": "system-events",
            },
            "mappings": {
                "properties": {
                    **_COMMON_MAPPINGS,
                    "cpu_percent": {"type": "float"},
                    "memory_percent": {"type": "float"},
                    "disk_used_percent": {"type": "float"},
                    "load_1m": {"type": "float"},
                    "load_5m": {"type": "float"},
                    "load_15m": {"type": "float"},
                    "service_name": {"type": "keyword"},
                    "service_state": {"type": "keyword"},
                }
            },
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
#  Ingest Pipelines
# ═══════════════════════════════════════════════════════════════════

INGEST_PIPELINES: dict[str, dict[str, Any]] = {
    "range-events-pipeline": {
        "description": "Range event enrichment — timestamp normalisation, geoip, user-agent parsing",
        "processors": [
            {
                "date": {
                    "field": "@timestamp",
                    "formats": [
                        "ISO8601",
                        "yyyy-MM-dd'T'HH:mm:ss.SSSZ",
                        "yyyy-MM-dd HH:mm:ss",
                        "epoch_millis",
                    ],
                    "target_field": "@timestamp",
                    "ignore_failure": True,
                }
            },
            {
                "geoip": {
                    "field": "source_ip",
                    "target_field": "geo",
                    "ignore_missing": True,
                    "ignore_failure": True,
                }
            },
            {
                "user_agent": {
                    "field": "user_agent.original",
                    "target_field": "user_agent",
                    "ignore_missing": True,
                    "ignore_failure": True,
                }
            },
            {
                "lowercase": {
                    "field": "event_type",
                    "ignore_missing": True,
                    "ignore_failure": True,
                }
            },
        ],
    },
    "security-events-pipeline": {
        "description": "Security event enrichment — MITRE ATT&CK lookup, severity classification",
        "processors": [
            {
                "date": {
                    "field": "@timestamp",
                    "formats": ["ISO8601", "epoch_millis"],
                    "target_field": "@timestamp",
                    "ignore_failure": True,
                }
            },
            {
                "script": {
                    "lang": "painless",
                    "description": "Classify severity based on alert fields",
                    "source": """
                        if (ctx.containsKey('alert_severity')) { return; }
                        String action = ctx.containsKey('action') ? ctx.action : '';
                        if (action == 'blocked' || action == 'denied') {
                            ctx.alert_severity = 'medium';
                        } else if (action == 'exploit' || action == 'exfiltration') {
                            ctx.alert_severity = 'critical';
                        } else {
                            ctx.alert_severity = 'low';
                        }
                    """,
                    "ignore_failure": True,
                }
            },
            {
                "script": {
                    "lang": "painless",
                    "description": "Map MITRE technique IDs to human-readable names (subset)",
                    "source": """
                        Map techniques = new HashMap();
                        techniques.put('T1110', 'Brute Force');
                        techniques.put('T1110.001', 'Password Guessing');
                        techniques.put('T1110.003', 'Password Spraying');
                        techniques.put('T1046', 'Network Service Discovery');
                        techniques.put('T1021', 'Remote Services');
                        techniques.put('T1021.001', 'Remote Desktop Protocol');
                        techniques.put('T1059', 'Command and Scripting Interpreter');
                        techniques.put('T1059.001', 'PowerShell');
                        techniques.put('T1053', 'Scheduled Task/Job');
                        techniques.put('T1078', 'Valid Accounts');
                        techniques.put('T1048', 'Exfiltration Over Alternative Protocol');
                        techniques.put('T1071', 'Application Layer Protocol');
                        techniques.put('T1105', 'Ingress Tool Transfer');
                        techniques.put('T1003', 'OS Credential Dumping');
                        techniques.put('T1027', 'Obfuscated Files or Information');
                        techniques.put('T1055', 'Process Injection');
                        techniques.put('T1070', 'Indicator Removal');
                        techniques.put('T1041', 'Exfiltration Over C2 Channel');
                        if (ctx.containsKey('mitre_technique') && techniques.containsKey(ctx.mitre_technique)) {
                            ctx.mitre_technique_name = techniques.get(ctx.mitre_technique);
                        }
                    """,
                    "ignore_failure": True,
                }
            },
        ],
    },
}

# ═══════════════════════════════════════════════════════════════════
#  Dashboard Saved Objects (minimal placeholder)
# ═══════════════════════════════════════════════════════════════════

DASHBOARD_OBJECTS: list[dict[str, Any]] = [
    {
        "type": "visualization",
        "id": "tn-events-over-time",
        "attributes": {
            "title": "TrueNorth — Events Over Time",
            "description": "Line chart of event volume across all indices",
            "visState": json.dumps({
                "type": "line",
                "aggs": [
                    {"id": "1", "type": "count", "schema": "metric"},
                    {"id": "2", "type": "date_histogram", "schema": "segment", "params": {"field": "@timestamp", "interval": "auto"}},
                ],
            }),
        },
    },
    {
        "type": "dashboard",
        "id": "tn-overview-dashboard",
        "attributes": {
            "title": "TrueNorth Range — Overview Dashboard",
            "description": "High-level view of all telemetry streams",
            "panelsJSON": json.dumps([
                {"panelIndex": "1", "gridData": {"x": 0, "y": 0, "w": 48, "h": 15}, "panelRefName": "panel_0"},
            ]),
        },
        "references": [
            {"name": "panel_0", "type": "visualization", "id": "tn-events-over-time"},
        ],
    },
]


# ═══════════════════════════════════════════════════════════════════
#  Bootstrap Functions
# ═══════════════════════════════════════════════════════════════════

def _client() -> httpx.Client:
    return httpx.Client(base_url=OPENSEARCH_URL, timeout=30.0)


def _exists(client: httpx.Client, path: str) -> bool:
    """HEAD check — returns True if resource exists (2xx)."""
    try:
        resp = client.head(path)
        return resp.is_success
    except httpx.HTTPError:
        return False


def create_ism_policy(client: httpx.Client) -> None:
    """Create or update the ISM lifecycle policy."""
    policy_id = "tn-event-lifecycle"
    if _exists(client, f"/_plugins/_ism/policies/{policy_id}"):
        resp = client.put(f"/_plugins/_ism/policies/{policy_id}", json=ISM_POLICY)
        logger.info("ISM policy updated: %s %s", resp.status_code, resp.text[:200])
    else:
        resp = client.put(f"/_plugins/_ism/policies/{policy_id}", json=ISM_POLICY)
        logger.info("ISM policy created: %s %s", resp.status_code, resp.text[:200])


def create_index_templates(client: httpx.Client) -> None:
    """Create all index templates (idempotent — PUT is upsert)."""
    for name, body in TEMPLATES.items():
        resp = client.put(f"/_index_template/{name}", json=body)
        logger.info("Index template '%s': %s", name, resp.status_code)


def create_ingest_pipelines(client: httpx.Client) -> None:
    """Create ingest pipelines (idempotent — PUT is upsert)."""
    for name, body in INGEST_PIPELINES.items():
        resp = client.put(f"/_ingest/pipeline/{name}", json=body)
        logger.info("Ingest pipeline '%s': %s", name, resp.status_code)


def create_initial_indices(client: httpx.Client) -> None:
    """Create initial write-alias indices if they don't exist."""
    for name in TEMPLATES:
        alias = name
        index = f"{name}-000001"
        if not _exists(client, f"/{index}"):
            body = {"aliases": {alias: {"is_write_index": True}}}
            resp = client.put(f"/{index}", json=body)
            logger.info("Initial index '%s': %s", index, resp.status_code)
        else:
            logger.info("Initial index '%s' already exists, skipping", index)


def create_dashboards(client: httpx.Client) -> None:
    """Import saved objects into OpenSearch Dashboards."""
    for obj in DASHBOARD_OBJECTS:
        obj_type = obj["type"]
        obj_id = obj["id"]
        path = f"/_dashboards/api/saved_objects/{obj_type}/{obj_id}"
        if _exists(client, path):
            logger.info("Dashboard object '%s/%s' exists, skipping", obj_type, obj_id)
            continue
        resp = client.post(path, json={"attributes": obj["attributes"]})
        logger.info("Dashboard object '%s/%s': %s", obj_type, obj_id, resp.status_code)


def bootstrap(opensearch_url: str | None = None) -> None:
    """Run the full bootstrap sequence."""
    global OPENSEARCH_URL
    if opensearch_url:
        OPENSEARCH_URL = opensearch_url

    logger.info("Bootstrapping OpenSearch at %s", OPENSEARCH_URL)
    with _client() as client:
        create_ism_policy(client)
        create_index_templates(client)
        create_ingest_pipelines(client)
        create_initial_indices(client)
        try:
            create_dashboards(client)
        except Exception as exc:
            logger.warning("Dashboard import skipped (Dashboards may not be available): %s", exc)
    logger.info("Bootstrap complete")


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap OpenSearch for TrueNorth Range")
    parser.add_argument(
        "--opensearch-url",
        default=os.getenv("OPENSEARCH_URL", "http://localhost:9200"),
        help="OpenSearch base URL (default: http://localhost:9200)",
    )
    args = parser.parse_args()
    bootstrap(args.opensearch_url)


if __name__ == "__main__":
    main()