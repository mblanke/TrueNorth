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
from typing import Any

import httpx

logger = logging.getLogger("telemetry.bootstrap")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")

# Replicas: a fixed count from OPENSEARCH_REPLICAS, else auto_expand_replicas "0-1" (none on
# a single node, one when a second joins). A fixed 1 on the default single-node install
# left every index unassignable-replica "yellow", and the API's deep health "degraded".
_REPLICAS = os.getenv("OPENSEARCH_REPLICAS", "").strip()
REPLICA_SETTINGS: dict = {"number_of_replicas": int(_REPLICAS)} if _REPLICAS else {"auto_expand_replicas": "0-1"}
ISM_CONFIG_INDEX = ".opendistro-ism-config"

# The filesystem snapshot repository scripts/backup/backup.sh snapshots into. The location is
# the node's path.repo (compose.prod.yml: the os-snapshots bind under TN_DATA_ROOT). No
# OPENSEARCH_SNAPSHOT_REPO: none is registered and telemetry is not backed up.
SNAPSHOT_REPO = os.getenv("OPENSEARCH_SNAPSHOT_REPO", "").strip()
SNAPSHOT_LOCATION = os.getenv("OPENSEARCH_SNAPSHOT_LOCATION", "/usr/share/opensearch/snapshots").strip()

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
                    # Warm keeps the hot-phase replica settings (REPLICA_SETTINGS); a fixed
                    # replica_count here would turn a single node yellow again after 7 days.
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
            {
                "index_patterns": [
                    "range-events-*",
                    "exercise-events-*",
                    "security-events-*",
                    "network-events-*",
                    "system-events-*",
                ]
            },
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

# Per-range indices (range-<range id> and range-<range id>-<date>): what detection credit is
# judged against (ADR 0005). Every document passes through INGESTED_AT_PIPELINE as the
# index's final pipeline, so the exercise window rests on the cluster's own clock whoever
# wrote the event. Strings map to keyword (with a .text sub-field for free-text search), so a
# scenario query such as url.domain:*northwind-update.example* matches the whole value
# rather than analysed tokens. Low priority: the named families below (range-events-*,
# ...) keep their own templates.
INGESTED_AT_PIPELINE = "truenorth-ingested-at"
RANGE_TEMPLATE: dict[str, Any] = {
    "index_patterns": ["range-*"],
    "priority": 10,
    "template": {
        "settings": {"index.final_pipeline": INGESTED_AT_PIPELINE, "index.refresh_interval": "5s", **REPLICA_SETTINGS},
        "mappings": {
            "dynamic_templates": [
                {
                    "strings_as_keywords": {
                        "match_mapping_type": "string",
                        "mapping": {
                            "type": "keyword",
                            "ignore_above": 8191,
                            "fields": {"text": {"type": "text"}},
                        },
                    }
                }
            ],
            "properties": {
                "@timestamp": {"type": "date"},
                "truenorth": {"properties": {"ingested_at": {"type": "date_nanos"}}},
                # Inject labels (scenario_engine GROUND_TRUTH_FIELD): kept in _source for
                # staff, never indexed, so no query (a Student's detection above all) can
                # match on them. Answer keys are written on observable fields (ADR 0005).
                "tn_ground_truth": {"type": "object", "enabled": False},
            },
        },
    },
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "range": RANGE_TEMPLATE,
    "range-events": {
        "index_patterns": ["range-events-*"],
        "priority": 100,
        "template": {
            "settings": {
                "number_of_shards": 2,
                **REPLICA_SETTINGS,
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
                **REPLICA_SETTINGS,
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
                **REPLICA_SETTINGS,
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
                **REPLICA_SETTINGS,
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
                **REPLICA_SETTINGS,
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
    INGESTED_AT_PIPELINE: {
        "description": "Server ingest time for detection credit (ADR 0005); drops any the sender supplied",
        "processors": [
            {
                "script": {
                    "lang": "painless",
                    # Both a nested truenorth object and flat "truenorth.*" keys: a flat key left
                    # beside the stamp would index as a second value of the same field.
                    "source": "ctx.remove('truenorth'); ctx.keySet().removeIf(k -> k.startsWith('truenorth.'));",
                }
            },
            {"set": {"field": "truenorth.ingested_at", "value": "{{_ingest.timestamp}}"}},
        ],
    },
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
            "visState": json.dumps(
                {
                    "type": "line",
                    "aggs": [
                        {"id": "1", "type": "count", "schema": "metric"},
                        {
                            "id": "2",
                            "type": "date_histogram",
                            "schema": "segment",
                            "params": {"field": "@timestamp", "interval": "auto"},
                        },
                    ],
                }
            ),
        },
    },
    {
        "type": "dashboard",
        "id": "tn-overview-dashboard",
        "attributes": {
            "title": "TrueNorth Range — Overview Dashboard",
            "description": "High-level view of all telemetry streams",
            "panelsJSON": json.dumps(
                [
                    {"panelIndex": "1", "gridData": {"x": 0, "y": 0, "w": 48, "h": 15}, "panelRefName": "panel_0"},
                ]
            ),
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
    """OPENSEARCH_USER / OPENSEARCH_PASS / OPENSEARCH_VERIFY_SSL, as the API and worker read them."""
    kwargs: dict[str, Any] = {"base_url": OPENSEARCH_URL, "timeout": 30.0}
    if user := os.getenv("OPENSEARCH_USER"):
        kwargs["auth"] = (user, os.getenv("OPENSEARCH_PASS", ""))
    verify = os.getenv("OPENSEARCH_VERIFY_SSL", "true").strip()
    if verify.lower() in ("0", "false", "no", "off"):
        kwargs["verify"] = False
    elif verify.lower() not in ("", "1", "true", "yes", "on"):
        kwargs["verify"] = verify
    return httpx.Client(**kwargs)


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
        if name == "range":  # per-range indices are created by their first event
            continue
        alias = name
        index = f"{name}-000001"
        if not _exists(client, f"/{index}"):
            body = {"aliases": {alias: {"is_write_index": True}}}
            resp = client.put(f"/{index}", json=body)
            logger.info("Initial index '%s': %s", index, resp.status_code)
        else:
            logger.info("Initial index '%s' already exists, skipping", index)


def apply_replica_settings(client: httpx.Client) -> None:
    """Bring indices created before a template change to REPLICA_SETTINGS.

    A template applies only to new indices; an existing single-node install would otherwise
    stay yellow until its indices roll over. The ISM plugin's own config index is created
    with one replica too, and on a single node it alone keeps the cluster yellow.
    """
    patterns = [p for t in TEMPLATES.values() for p in t["index_patterns"]] + [ISM_CONFIG_INDEX]
    for pattern in patterns:
        resp = client.put(
            f"/{pattern}/_settings", params={"allow_no_indices": "true"}, json={"index": REPLICA_SETTINGS}
        )
        logger.info("Replica settings on '%s': %s", pattern, resp.status_code)


def register_snapshot_repository(client: httpx.Client, name: str = "", location: str = "") -> bool:
    """Register (or re-register: PUT is an upsert) the fs snapshot repository; False if none.

    Unlike the steps above, a failure raises: the nightly backup snapshots into this
    repository, and finding out at 02:17 that it was never registered fails the whole backup.
    OpenSearch verifies a repository when it is registered (the node writes a test blob), so
    a location outside path.repo or not writable by the opensearch user fails here.
    """
    name = name or SNAPSHOT_REPO
    location = location or SNAPSHOT_LOCATION
    if not name:
        logger.info("Snapshot repository: none (OPENSEARCH_SNAPSHOT_REPO unset)")
        return False
    body = {"type": "fs", "settings": {"location": location, "compress": True}}
    resp = client.put(f"/_snapshot/{name}", json=body)
    if not resp.is_success:
        raise RuntimeError(f"snapshot repository '{name}' at {location}: HTTP {resp.status_code} {resp.text[:300]}")
    logger.info("Snapshot repository '%s' at %s: %s", name, location, resp.status_code)
    return True


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
        create_ingest_pipelines(client)  # first: the range template names one as its final pipeline
        create_index_templates(client)
        create_initial_indices(client)
        apply_replica_settings(client)
        register_snapshot_repository(client)
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
