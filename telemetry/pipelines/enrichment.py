"""Telemetry event enrichment — MITRE ATT&CK, GeoIP, user/range context.

All enrichment functions are **pure transformations** that mutate the event
dict in-place and return it.  They can be composed into a pipeline::

    from telemetry.pipelines.enrichment import enrich_event
    event = enrich_event(event, user_cache=cache, range_cache=cache)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("telemetry.enrichment")

# ═══════════════════════════════════════════════════════════════════
#  MITRE ATT&CK Lookup
# ═══════════════════════════════════════════════════════════════════

MITRE_TECHNIQUES: dict[str, dict[str, str]] = {
    # Initial Access
    "T1190": {"name": "Exploit Public-Facing Application", "tactic": "initial-access"},
    "T1133": {"name": "External Remote Services", "tactic": "initial-access"},
    "T1078": {"name": "Valid Accounts", "tactic": "initial-access"},
    "T1566": {"name": "Phishing", "tactic": "initial-access"},
    "T1566.001": {"name": "Spearphishing Attachment", "tactic": "initial-access"},
    "T1566.002": {"name": "Spearphishing Link", "tactic": "initial-access"},
    # Execution
    "T1059": {"name": "Command and Scripting Interpreter", "tactic": "execution"},
    "T1059.001": {"name": "PowerShell", "tactic": "execution"},
    "T1059.003": {"name": "Windows Command Shell", "tactic": "execution"},
    "T1059.004": {"name": "Unix Shell", "tactic": "execution"},
    "T1053": {"name": "Scheduled Task/Job", "tactic": "execution"},
    "T1053.005": {"name": "Scheduled Task", "tactic": "execution"},
    # Persistence
    "T1547": {"name": "Boot or Logon Autostart Execution", "tactic": "persistence"},
    "T1547.001": {"name": "Registry Run Keys / Startup Folder", "tactic": "persistence"},
    "T1136": {"name": "Create Account", "tactic": "persistence"},
    # Privilege Escalation
    "T1055": {"name": "Process Injection", "tactic": "privilege-escalation"},
    "T1068": {"name": "Exploitation for Privilege Escalation", "tactic": "privilege-escalation"},
    # Defense Evasion
    "T1027": {"name": "Obfuscated Files or Information", "tactic": "defense-evasion"},
    "T1070": {"name": "Indicator Removal", "tactic": "defense-evasion"},
    "T1070.004": {"name": "File Deletion", "tactic": "defense-evasion"},
    "T1562": {"name": "Impair Defenses", "tactic": "defense-evasion"},
    "T1562.001": {"name": "Disable or Modify Tools", "tactic": "defense-evasion"},
    # Credential Access
    "T1110": {"name": "Brute Force", "tactic": "credential-access"},
    "T1110.001": {"name": "Password Guessing", "tactic": "credential-access"},
    "T1110.003": {"name": "Password Spraying", "tactic": "credential-access"},
    "T1003": {"name": "OS Credential Dumping", "tactic": "credential-access"},
    "T1003.001": {"name": "LSASS Memory", "tactic": "credential-access"},
    "T1555": {"name": "Credentials from Password Stores", "tactic": "credential-access"},
    # Discovery
    "T1046": {"name": "Network Service Discovery", "tactic": "discovery"},
    "T1082": {"name": "System Information Discovery", "tactic": "discovery"},
    "T1083": {"name": "File and Directory Discovery", "tactic": "discovery"},
    "T1087": {"name": "Account Discovery", "tactic": "discovery"},
    "T1018": {"name": "Remote System Discovery", "tactic": "discovery"},
    # Lateral Movement
    "T1021": {"name": "Remote Services", "tactic": "lateral-movement"},
    "T1021.001": {"name": "Remote Desktop Protocol", "tactic": "lateral-movement"},
    "T1021.002": {"name": "SMB/Windows Admin Shares", "tactic": "lateral-movement"},
    "T1021.004": {"name": "SSH", "tactic": "lateral-movement"},
    "T1570": {"name": "Lateral Tool Transfer", "tactic": "lateral-movement"},
    # Collection
    "T1005": {"name": "Data from Local System", "tactic": "collection"},
    "T1039": {"name": "Data from Network Shared Drive", "tactic": "collection"},
    "T1074": {"name": "Data Staged", "tactic": "collection"},
    # Exfiltration
    "T1048": {"name": "Exfiltration Over Alternative Protocol", "tactic": "exfiltration"},
    "T1041": {"name": "Exfiltration Over C2 Channel", "tactic": "exfiltration"},
    "T1567": {"name": "Exfiltration Over Web Service", "tactic": "exfiltration"},
    # Command & Control
    "T1071": {"name": "Application Layer Protocol", "tactic": "command-and-control"},
    "T1105": {"name": "Ingress Tool Transfer", "tactic": "command-and-control"},
    "T1572": {"name": "Protocol Tunneling", "tactic": "command-and-control"},
    # Impact
    "T1486": {"name": "Data Encrypted for Impact", "tactic": "impact"},
    "T1489": {"name": "Service Stop", "tactic": "impact"},
    "T1490": {"name": "Inhibit System Recovery", "tactic": "impact"},
}


def enrich_mitre(event: dict[str, Any]) -> dict[str, Any]:
    """Add ``mitre_technique_name`` and ``mitre_tactic`` from technique ID."""
    technique_id = event.get("mitre_technique")
    if technique_id and technique_id in MITRE_TECHNIQUES:
        info = MITRE_TECHNIQUES[technique_id]
        event["mitre_technique_name"] = info["name"]
        event.setdefault("mitre_tactic", info["tactic"])
    return event


# ═══════════════════════════════════════════════════════════════════
#  GeoIP Enrichment (mock for dev, pluggable for production)
# ═══════════════════════════════════════════════════════════════════

# Mock GeoIP database — maps known RFC-5737 / documentation CIDRs
_MOCK_GEOIP: dict[str, dict[str, Any]] = {
    "198.51.100.": {"country_iso_code": "US", "city_name": "Ashburn", "location": {"lat": 39.0438, "lon": -77.4874}},
    "203.0.113.": {"country_iso_code": "CN", "city_name": "Beijing", "location": {"lat": 39.9042, "lon": 116.4074}},
    "192.0.2.": {"country_iso_code": "GB", "city_name": "London", "location": {"lat": 51.5074, "lon": -0.1278}},
    "10.": {"country_iso_code": "XX", "city_name": "Internal", "location": {"lat": 0.0, "lon": 0.0}},
    "172.16.": {"country_iso_code": "XX", "city_name": "Internal", "location": {"lat": 0.0, "lon": 0.0}},
    "192.168.": {"country_iso_code": "XX", "city_name": "Internal", "location": {"lat": 0.0, "lon": 0.0}},
}


def _mock_geoip_lookup(ip: str) -> dict[str, Any] | None:
    """Return mock geo data based on IP prefix."""
    for prefix, geo in _MOCK_GEOIP.items():
        if ip.startswith(prefix):
            return dict(geo)  # copy
    return None


# Pluggable lookup — replace with maxmind / ipinfo for production
_geoip_lookup: Callable[[str], dict[str, Any] | None] = _mock_geoip_lookup


def set_geoip_provider(fn: Callable[[str], dict[str, Any] | None]) -> None:
    """Override the GeoIP lookup function (e.g. with MaxMind reader)."""
    global _geoip_lookup
    _geoip_lookup = fn


def enrich_geoip(event: dict[str, Any]) -> dict[str, Any]:
    """Add ``geo`` object from ``source_ip``."""
    ip = event.get("source_ip")
    if ip:
        geo = _geoip_lookup(ip)
        if geo:
            event.setdefault("geo", {}).update(geo)
    return event


# ═══════════════════════════════════════════════════════════════════
#  User Context Enrichment
# ═══════════════════════════════════════════════════════════════════


def enrich_user_context(
    event: dict[str, Any],
    user_cache: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Resolve ``user_id`` → ``display_name``, ``role`` from cache.

    Parameters
    ----------
    event : dict
        Must contain ``user_id`` to enrich.
    user_cache : dict | None
        Mapping of ``{user_id: {"display_name": ..., "role": ...}}``.
    """
    if user_cache is None:
        return event
    user_id = event.get("user_id")
    if user_id and user_id in user_cache:
        info = user_cache[user_id]
        event["user_display_name"] = info.get("display_name", user_id)
        event["user_role"] = info.get("role", "unknown")
    return event


# ═══════════════════════════════════════════════════════════════════
#  Range Context Enrichment
# ═══════════════════════════════════════════════════════════════════


def enrich_range_context(
    event: dict[str, Any],
    range_cache: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Resolve ``range_id`` → ``range_name``, ``tenant_name`` from cache.

    Parameters
    ----------
    event : dict
        Must contain ``range_id`` to enrich.
    range_cache : dict | None
        Mapping of ``{range_id: {"name": ..., "tenant_id": ..., "tenant_name": ...}}``.
    """
    if range_cache is None:
        return event
    range_id = event.get("range_id")
    if range_id and range_id in range_cache:
        info = range_cache[range_id]
        event["range_name"] = info.get("name", range_id)
        event.setdefault("tenant_id", info.get("tenant_id", ""))
        event["tenant_name"] = info.get("tenant_name", "")
    return event


# ═══════════════════════════════════════════════════════════════════
#  Timestamp Normalisation
# ═══════════════════════════════════════════════════════════════════

_TIMESTAMP_FIELDS = ("@timestamp", "timestamp", "event_time", "ts")


def normalise_timestamp(event: dict[str, Any]) -> dict[str, Any]:
    """Ensure ``@timestamp`` is present in UTC ISO 8601 format."""
    for field in _TIMESTAMP_FIELDS:
        raw = event.get(field)
        if raw is None:
            continue
        # Already ISO string
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                event["@timestamp"] = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                return event
            except ValueError:
                pass
        # Epoch seconds / millis
        if isinstance(raw, (int, float)):
            if raw > 1e12:
                raw = raw / 1000.0
            dt = datetime.fromtimestamp(raw, tz=timezone.utc)
            event["@timestamp"] = dt.isoformat().replace("+00:00", "Z")
            return event

    # Fallback — inject current UTC time
    event["@timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return event


# ═══════════════════════════════════════════════════════════════════
#  Composite Enrichment Pipeline
# ═══════════════════════════════════════════════════════════════════


def enrich_event(
    event: dict[str, Any],
    *,
    user_cache: dict[str, dict[str, str]] | None = None,
    range_cache: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Run all enrichment steps on an event (in-place)."""
    normalise_timestamp(event)
    enrich_mitre(event)
    enrich_geoip(event)
    enrich_user_context(event, user_cache=user_cache)
    enrich_range_context(event, range_cache=range_cache)
    return event