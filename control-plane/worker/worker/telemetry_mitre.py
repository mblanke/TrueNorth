"""MITRE ATT&CK technique tagging for telemetry on its way into a range's index.

Every event written to ``range-<id>`` (``POST /telemetry/{range_id}/events`` in the API,
``ingest_telemetry_batch`` in the worker) leaves with a normalised ``mitre_technique``
list when a technique is known, so analysts and detection rules can search
``mitre_technique:T1059`` whichever sensor or inject produced the event. It replaces the
stub that sat in the removed telemetry-pipeline (``tag_mitre_attack``).

Where the technique comes from, first match wins:

1. The event says so: ``mitre_technique`` (a string or a list) or ``technique_id`` (what
   scenario-engine injectors emit). Values are upper-cased and kept only when they look
   like an ATT&CK ID (``T1234`` / ``T1234.001``); an event that names only junk falls
   through to the next rule.
2. ``event_type`` in ``EVENT_TYPE_TECHNIQUES`` (case-insensitive; a dotted type such as
   ``sysmon.process_exec`` is also tried by its last segment).

Nothing matched leaves the event untouched: no empty list, no guessed technique.

This file exists twice, byte for byte: control-plane/api/app/telemetry_mitre.py and
control-plane/worker/worker/telemetry_mitre.py (the two images share no code, and the
API may not import the worker). tests/contracts/test_telemetry_mitre.py fails if they
drift; edit one, copy it over the other.
"""

from __future__ import annotations

import re
from typing import Any

TECHNIQUE_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")

# event_type -> ATT&CK technique IDs. Generic sensor event types plus the scenario-engine
# injector names that emit telemetry without a technique of their own.
EVENT_TYPE_TECHNIQUES: dict[str, tuple[str, ...]] = {
    "dns_query": ("T1071.004",),  # Application Layer Protocol: DNS
    "dns_spike": ("T1071.004",),
    "http_request": ("T1071.001",),  # Application Layer Protocol: Web Protocols
    "http_burst": ("T1071.001",),
    "process_exec": ("T1059",),  # Command and Scripting Interpreter
    "simulated_execution": ("T1059",),
    "file_create": ("T1105",),  # Ingress Tool Transfer
    "registry_mod": ("T1112",),  # Modify Registry
    "login_attempt": ("T1110",),  # Brute Force
    "lateral_move": ("T1021",),  # Remote Services
    "network_scan": ("T1046",),  # Network Service Discovery
    "email_phish": ("T1566.001",),  # Phishing: Spearphishing Attachment
    "identity_new_admin_user": ("T1136",),  # Create Account
    "c2_beacon": ("T1071", "T1573"),  # Application Layer Protocol + Encrypted Channel
    "data_exfil": ("T1041",),  # Exfiltration Over C2 Channel
}


def _explicit(event: dict[str, Any]) -> list[str]:
    raw = event.get("mitre_technique")
    if raw is None:
        raw = event.get("technique_id")
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for v in values:
        if isinstance(v, str) and TECHNIQUE_ID.match(v.strip().upper()):
            out.append(v.strip().upper())
    return out


def _by_event_type(event: dict[str, Any]) -> list[str]:
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        return []
    key = event_type.strip().lower()
    found = EVENT_TYPE_TECHNIQUES.get(key) or EVENT_TYPE_TECHNIQUES.get(key.rsplit(".", 1)[-1], ())
    return list(found)


def techniques_for(event: dict[str, Any]) -> list[str]:
    """The ATT&CK technique IDs this event carries, sorted and de-duplicated."""
    return sorted(set(_explicit(event) or _by_event_type(event)))


def tag_event(event: dict[str, Any]) -> dict[str, Any]:
    """Set ``event["mitre_technique"]`` to its technique list, in place; returns *event*.

    An event with no recognisable technique keeps whatever it had (a non-ID value is
    left for a human to read rather than silently dropped).
    """
    found = techniques_for(event)
    if found:
        event["mitre_technique"] = found
    return event
