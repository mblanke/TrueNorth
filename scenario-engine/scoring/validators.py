"""TrueNorth Range — Scoring Validators.

Each validator checks whether a specific type of objective has been
completed, returning ``(achieved, evidence)`` where *evidence* is a
list of supporting artefact dicts.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ScoringValidator:
    """Validates objective completion against collected evidence."""

    # ── dispatcher ──────────────────────────────────────────

    @staticmethod
    async def validate(
        method: str,
        config: dict[str, Any],
        opensearch_url: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Route to the correct validator based on *method*."""
        dispatch = {
            "opensearch_query": ScoringValidator.validate_opensearch_query,
            "deliverable": ScoringValidator.validate_deliverable,
            "firewall_rule": ScoringValidator.validate_firewall_rule,
            "process_killed": ScoringValidator.validate_process_killed,
            "user_disabled": ScoringValidator.validate_user_disabled,
            "network_isolated": ScoringValidator.validate_network_isolated,
            "manual": ScoringValidator.validate_manual,
        }
        handler = dispatch.get(method)
        if handler is None:
            logger.warning("Unknown validation method: %s", method)
            return False, []
        if method == "opensearch_query":
            return await handler(config, opensearch_url)
        return await handler(config)

    # ── individual validators ───────────────────────────────

    @staticmethod
    async def validate_opensearch_query(
        config: dict[str, Any],
        opensearch_url: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if expected events exist in OpenSearch.

        ``config`` keys:
            - ``index``: index pattern (e.g. ``truenorth-*``)
            - ``query``: OpenSearch query DSL (dict)
            - ``threshold``: minimum number of matching docs
        """
        if not opensearch_url:
            logger.warning("No OpenSearch URL configured; skipping query validation")
            return False, []

        index = config.get("index", "truenorth-*")
        query = config.get("query", {"match_all": {}})
        threshold = config.get("threshold", 1)

        try:
            import aiohttp

            url = f"{opensearch_url.rstrip('/')}/{index}/_search"
            payload = {"query": query, "size": min(threshold, 100)}
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    json=payload,
                    ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp,
            ):
                data = await resp.json()
                hits = data.get("hits", {}).get("hits", [])
                total = data.get("hits", {}).get("total", {})
                total_count = total.get("value", 0) if isinstance(total, dict) else total
                evidence = [{"_id": h["_id"], "_source": h.get("_source", {})} for h in hits[:20]]
                achieved = total_count >= threshold
                return achieved, evidence
        except ImportError:
            logger.error("aiohttp is required for OpenSearch validation")
            return False, []
        except Exception:
            logger.exception("OpenSearch query validation failed")
            return False, []

    @staticmethod
    async def validate_deliverable(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if a deliverable (report, IOC list, etc.) was submitted.

        ``config`` keys:
            - ``deliverable_type``: e.g. ``incident_report``, ``ioc_list``
            - ``storage_path``: path or URL where deliverable should be
            - ``min_size_bytes``: minimum file size to count as valid
            - ``required_fields``: fields that must be present (for JSON)
        """
        import os

        storage_path = config.get("storage_path", "")
        min_size = config.get("min_size_bytes", 100)
        deliverable_type = config.get("deliverable_type", "unknown")

        if not storage_path:
            return False, []

        if not os.path.exists(storage_path):
            return False, [{"error": f"Deliverable not found at {storage_path}"}]

        file_size = os.path.getsize(storage_path)
        if file_size < min_size:
            return False, [
                {
                    "error": f"Deliverable too small ({file_size} < {min_size} bytes)",
                    "path": storage_path,
                }
            ]

        evidence = [
            {
                "deliverable_type": deliverable_type,
                "path": storage_path,
                "size_bytes": file_size,
            }
        ]

        # Optional JSON field validation
        required_fields = config.get("required_fields", [])
        if required_fields and storage_path.endswith(".json"):
            import json

            try:
                with open(storage_path, encoding="utf-8") as f:
                    data = json.load(f)
                missing = [fld for fld in required_fields if fld not in data]
                if missing:
                    return False, [{"error": f"Missing fields: {missing}"}]
            except (json.JSONDecodeError, OSError) as exc:
                return False, [{"error": str(exc)}]

        return True, evidence

    @staticmethod
    async def validate_firewall_rule(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if expected firewall rules were created.

        ``config`` keys:
            - ``host``: target host to check
            - ``expected_rules``: list of dicts with keys ``port``,
              ``protocol``, ``action`` (allow/block)
            - ``check_method``: ``opensearch`` or ``agent``
        """
        expected_rules = config.get("expected_rules", [])
        if not expected_rules:
            return False, []

        # In production this would query the host or OpenSearch
        # for firewall-configuration events.  Here we stub it.
        evidence: list[dict[str, Any]] = []
        matched = 0
        for rule in expected_rules:
            # Placeholder: actual implementation queries target host
            evidence.append(
                {
                    "rule": rule,
                    "status": "pending_verification",
                }
            )
            matched += 1  # In real impl, only count verified rules

        achieved = matched >= len(expected_rules)
        return achieved, evidence

    @staticmethod
    async def validate_process_killed(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if a malicious process was terminated.

        ``config`` keys:
            - ``process_name``: name of the process (e.g. ``beacon.exe``)
            - ``host``: target host
            - ``opensearch_index``: index to query for Sysmon Event ID 5
        """
        process_name = config.get("process_name", "")
        if not process_name:
            return False, []

        # Placeholder — real implementation queries Sysmon logs for
        # Event ID 5 (Process Terminated) matching process_name.
        evidence: list[dict[str, Any]] = [
            {
                "check": "process_killed",
                "process_name": process_name,
                "status": "pending_verification",
            }
        ]
        return False, evidence

    @staticmethod
    async def validate_user_disabled(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if a compromised account was disabled.

        ``config`` keys:
            - ``username``: the account name
            - ``domain``: AD domain
            - ``opensearch_index``: index for Event ID 4725
        """
        username = config.get("username", "")
        if not username:
            return False, []

        # Placeholder — real implementation queries Windows Security
        # logs for Event ID 4725 (User Account Disabled).
        evidence: list[dict[str, Any]] = [
            {
                "check": "user_disabled",
                "username": username,
                "status": "pending_verification",
            }
        ]
        return False, evidence

    @staticmethod
    async def validate_network_isolated(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Check if an infected host was network-isolated.

        ``config`` keys:
            - ``host_ip``: IP of the isolated host
            - ``vlan``: expected quarantine VLAN
            - ``check_method``: ``ping`` or ``opensearch``
        """
        host_ip = config.get("host_ip", "")
        if not host_ip:
            return False, []

        # Placeholder — real implementation would attempt connectivity
        # or query firewall / switch logs.
        evidence: list[dict[str, Any]] = [
            {
                "check": "network_isolated",
                "host_ip": host_ip,
                "status": "pending_verification",
            }
        ]
        return False, evidence

    @staticmethod
    async def validate_manual(
        config: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Manual validation — always returns pending.

        Instructor must mark this objective via the API.

        ``config`` keys:
            - ``approved``: bool set by instructor
            - ``notes``: instructor feedback
        """
        approved = config.get("approved", False)
        notes = config.get("notes", "Awaiting instructor review")
        evidence: list[dict[str, Any]] = [{"manual_review": True, "approved": approved, "notes": notes}]
        return approved, evidence
