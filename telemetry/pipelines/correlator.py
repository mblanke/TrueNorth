"""Telemetry event correlation engine — pattern-based alert generation.

Consumes enriched events, correlates within time windows, and produces
``correlated_event`` alerts when configurable patterns match.

Supported patterns:
- **Brute force**: N failed logins from same source in window
- **Port scan**: Connections to N distinct ports from same source
- **Lateral movement**: Authentication chain across N hosts from same user
- **Data exfiltration**: Outbound transfer exceeding threshold

Rules are loaded from YAML files in ``telemetry/correlation-rules/``.

Usage::

    engine = CorrelationEngine.from_rules_dir("telemetry/correlation-rules/")
    alert = engine.process_event(enriched_event)
    if alert:
        await ingestor.ingest("security_event", alert)
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("telemetry.correlator")

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════
#  Correlation Rule Model
# ═══════════════════════════════════════════════════════════════════


@dataclass
class CorrelationRule:
    """Defines a correlation pattern."""

    id: str
    name: str
    description: str
    event_filter: dict[str, Any]  # field → value(s) that an event must match
    group_by: list[str]           # fields used to group events (e.g. source_ip)
    threshold: int                # number of matching events to trigger
    window_seconds: float         # sliding time window
    distinct_field: str | None    # count distinct values of this field (e.g. dest_port)
    severity: str = "high"
    mitre_technique: str = ""
    mitre_tactic: str = ""
    alert_name: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CorrelationRule":
        rule = data.get("rule", data)
        return cls(
            id=rule.get("id", str(uuid.uuid4())[:8]),
            name=rule["name"],
            description=rule.get("description", ""),
            event_filter=rule.get("event_filter", {}),
            group_by=rule.get("group_by", []),
            threshold=rule.get("threshold", 5),
            window_seconds=rule.get("window_seconds", 300),
            distinct_field=rule.get("distinct_field"),
            severity=rule.get("severity", "high"),
            mitre_technique=rule.get("mitre_technique", ""),
            mitre_tactic=rule.get("mitre_tactic", ""),
            alert_name=rule.get("alert_name", rule["name"]),
            enabled=rule.get("enabled", True),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CorrelationRule":
        if yaml is None:
            raise ImportError("PyYAML required: pip install pyyaml")
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        return cls.from_dict(data)


# ═══════════════════════════════════════════════════════════════════
#  Time-Windowed Event Bucket
# ═══════════════════════════════════════════════════════════════════


@dataclass
class _EventBucket:
    """Tracks events for a (rule, group_key) pair."""

    events: list[dict[str, Any]] = field(default_factory=list)
    distinct_values: set[str] = field(default_factory=set)
    last_alert_time: float = 0.0

    def add(self, event: dict[str, Any], ts: float, distinct_value: str | None = None) -> None:
        self.events.append({"event": event, "ts": ts})
        if distinct_value is not None:
            self.distinct_values.add(distinct_value)

    def prune(self, window: float, now: float) -> None:
        """Remove events older than the window."""
        cutoff = now - window
        before = len(self.events)
        self.events = [e for e in self.events if e["ts"] >= cutoff]
        if len(self.events) < before:
            # Rebuild distinct values after pruning
            self.distinct_values.clear()

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def distinct_count(self) -> int:
        return len(self.distinct_values)


# ═══════════════════════════════════════════════════════════════════
#  Correlation Engine
# ═══════════════════════════════════════════════════════════════════


class CorrelationEngine:
    """Stateful engine that evaluates events against loaded rules."""

    def __init__(self, rules: list[CorrelationRule] | None = None):
        self._rules: list[CorrelationRule] = rules or []
        # {rule_id: {group_key: _EventBucket}}
        self._buckets: dict[str, dict[str, _EventBucket]] = defaultdict(lambda: defaultdict(_EventBucket))
        self._alert_cooldown: float = 60.0  # min seconds between repeated alerts

    # ── factory ───────────────────────────────────────────────────

    @classmethod
    def from_rules_dir(cls, directory: str | Path) -> "CorrelationEngine":
        """Load all YAML rule files from a directory."""
        rules: list[CorrelationRule] = []
        rule_dir = Path(directory)
        if not rule_dir.is_dir():
            logger.warning("Rules directory not found: %s", rule_dir)
            return cls(rules)
        for path in sorted(rule_dir.glob("*.yaml")):
            try:
                rule = CorrelationRule.from_yaml(path)
                if rule.enabled:
                    rules.append(rule)
                    logger.info("Loaded rule '%s' from %s", rule.name, path.name)
            except Exception as exc:
                logger.error("Failed to load rule %s: %s", path.name, exc)
        return cls(rules)

    def add_rule(self, rule: CorrelationRule) -> None:
        self._rules.append(rule)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    # ── event processing ──────────────────────────────────────────

    def process_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Evaluate event against all rules.  Returns list of generated alerts (may be empty)."""
        now = time.time()
        alerts: list[dict[str, Any]] = []

        for rule in self._rules:
            if not self._matches_filter(event, rule.event_filter):
                continue

            group_key = self._extract_group_key(event, rule.group_by)
            if group_key is None:
                continue

            bucket = self._buckets[rule.id][group_key]
            bucket.prune(rule.window_seconds, now)

            distinct_value: str | None = None
            if rule.distinct_field:
                distinct_value = str(event.get(rule.distinct_field, ""))

            bucket.add(event, now, distinct_value)

            # Check threshold
            effective_count = bucket.distinct_count if rule.distinct_field else bucket.count
            if effective_count >= rule.threshold:
                if now - bucket.last_alert_time >= self._alert_cooldown:
                    alert = self._generate_alert(rule, bucket, group_key, now)
                    alerts.append(alert)
                    bucket.last_alert_time = now

        return alerts

    # ── internals ─────────────────────────────────────────────────

    @staticmethod
    def _matches_filter(event: dict[str, Any], filt: dict[str, Any]) -> bool:
        for field_name, expected in filt.items():
            value = event.get(field_name)
            if isinstance(expected, list):
                if value not in expected:
                    return False
            elif value != expected:
                return False
        return True

    @staticmethod
    def _extract_group_key(event: dict[str, Any], group_by: list[str]) -> str | None:
        parts: list[str] = []
        for field_name in group_by:
            val = event.get(field_name)
            if val is None:
                return None
            parts.append(str(val))
        return "|".join(parts)

    @staticmethod
    def _generate_alert(
        rule: CorrelationRule,
        bucket: _EventBucket,
        group_key: str,
        now: float,
    ) -> dict[str, Any]:
        source_event_count = bucket.count
        source_events = [e["event"] for e in bucket.events[-20:]]  # cap at 20

        alert: dict[str, Any] = {
            "alert_id": str(uuid.uuid4()),
            "alert_name": rule.alert_name,
            "rule_id": rule.id,
            "severity": rule.severity,
            "description": rule.description,
            "group_key": group_key,
            "source_event_count": source_event_count,
            "source_events": source_events,
            "event_type": "correlated_alert",
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        if rule.mitre_technique:
            alert["mitre_technique"] = rule.mitre_technique
        if rule.mitre_tactic:
            alert["mitre_tactic"] = rule.mitre_tactic

        # Propagate common fields from first event
        first = source_events[0] if source_events else {}
        for f in ("range_id", "tenant_id", "exercise_id"):
            if f in first:
                alert[f] = first[f]

        logger.warning(
            "ALERT [%s] %s — %d events from group '%s'",
            rule.severity.upper(), rule.alert_name, source_event_count, group_key,
        )
        return alert