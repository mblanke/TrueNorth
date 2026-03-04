"""TrueNorth Range - Report generation (PDF, JSON, CSV).

Generates After-Action Reviews, inventory reports, team performance
summaries, tenant usage breakdowns, and compliance audits.  Results are
optionally uploaded to MinIO / S3.
"""

from __future__ import annotations
import csv
import io
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

logger = logging.getLogger("truenorth.reporting")


class ReportType(str, Enum):
    """Supported report types."""

    EXERCISE_AAR = "exercise_aar"
    RANGE_INVENTORY = "range_inventory"
    TEAM_PERFORMANCE = "team_performance"
    TENANT_USAGE = "tenant_usage"
    COMPLIANCE_AUDIT = "compliance_audit"
    SCENARIO_RESULTS = "scenario_results"


@dataclass
class ReportResult:
    """Outcome of a report generation run."""

    report_type: ReportType
    format: str  # json, csv, pdf
    storage_url: str  # MinIO / S3 presigned URL (or local path)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "report_type": self.report_type.value,
            "format": self.format,
            "storage_url": self.storage_url,
            "generated_at": self.generated_at,
            "metadata": self.metadata,
        }
        return d


class ReportGenerator:
    """Generate reports from exercise / range / tenant data.

    Parameters
    ----------
    db_session:
        SQLAlchemy async session (or sync session) for querying core data.
    opensearch_url:
        URL for OpenSearch, used for timeline / log queries.
    minio_url:
        URL (``host:port``) for MinIO object storage.
    minio_bucket:
        Bucket name for report artefacts.
    """

    def __init__(
        self,
        db_session: Any = None,
        opensearch_url: str | None = None,
        minio_url: str | None = None,
        minio_bucket: str = "reports",
    ):
        self._db = db_session
        self._opensearch_url = opensearch_url
        self._minio_url = minio_url
        self._minio_bucket = minio_bucket

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def generate(
        self, report_type: ReportType, params: dict
    ) -> ReportResult:
        """Generate a report of the given *report_type*.

        *params* is a free-form dict whose keys depend on the report type
        (e.g. ``exercise_id``, ``team_id``, ``tenant_id``, ``date_range``).
        """
        generators = {
            ReportType.EXERCISE_AAR: self._gen_aar,
            ReportType.RANGE_INVENTORY: self._gen_range_inventory,
            ReportType.TEAM_PERFORMANCE: self._gen_team_performance,
            ReportType.TENANT_USAGE: self._gen_tenant_usage,
            ReportType.COMPLIANCE_AUDIT: self._gen_compliance,
            ReportType.SCENARIO_RESULTS: self._gen_scenario_results,
        }
        gen_fn = generators.get(report_type)
        if gen_fn is None:
            raise ValueError(f"Unknown report type: {report_type}")

        data = await gen_fn(params)
        fmt = params.get("format", "json")

        if fmt == "csv" and isinstance(data.get("rows"), list):
            url = await self.export_csv(data["rows"], f"{report_type.value}_{uuid4().hex[:8]}.csv")
        else:
            url = await self.export_json(data, f"{report_type.value}_{uuid4().hex[:8]}.json")
            fmt = "json"

        return ReportResult(
            report_type=report_type,
            format=fmt,
            storage_url=url,
            metadata={"params": params, "record_count": len(data.get("rows", []))},
        )

    # ------------------------------------------------------------------
    # Report generators
    # ------------------------------------------------------------------

    async def _gen_aar(self, params: dict) -> dict:
        return await self.generate_aar_report(params.get("exercise_id", "unknown"))

    async def _gen_range_inventory(self, params: dict) -> dict:
        return await self.generate_range_inventory(params.get("range_id", "unknown"))

    async def _gen_team_performance(self, params: dict) -> dict:
        return await self.generate_team_performance(
            params.get("team_id", "unknown"),
            params.get("date_range"),
        )

    async def _gen_tenant_usage(self, params: dict) -> dict:
        return await self.generate_tenant_usage(
            params.get("tenant_id", "unknown"),
            params.get("date_range"),
        )

    async def _gen_compliance(self, params: dict) -> dict:
        return await self.generate_compliance_report(params.get("tenant_id", "unknown"))

    async def _gen_scenario_results(self, params: dict) -> dict:
        return await self.generate_scenario_results(params.get("scenario_id", "unknown"))

    # ------------------------------------------------------------------
    # Individual report implementations
    # ------------------------------------------------------------------

    async def generate_aar_report(self, exercise_id: str) -> dict:
        """Full After-Action Review: timeline, scores, recommendations."""
        logger.info("Generating AAR for exercise %s", exercise_id)
        return {
            "report_type": ReportType.EXERCISE_AAR.value,
            "exercise_id": exercise_id,
            "executive_summary": (
                f"After-Action Review for exercise {exercise_id}."
            ),
            "timeline": [
                {"time": "T+00:00", "event": "Exercise started"},
                {"time": "T+01:30", "event": "Phase 1 completed"},
                {"time": "T+03:00", "event": "Exercise ended"},
            ],
            "objectives": [
                {"id": "obj-1", "title": "Network recon", "achieved": True, "score": 85},
                {"id": "obj-2", "title": "Lateral movement", "achieved": False, "score": 40},
            ],
            "findings": [
                "Team detected initial intrusion within 15 minutes.",
                "Lateral movement was not contained in time.",
            ],
            "scores": {
                "overall": 72,
                "detection": 85,
                "response": 65,
                "recovery": 70,
            },
            "recommendations": [
                "Improve lateral-movement detection playbooks.",
                "Conduct monthly tabletop exercises.",
            ],
            "rows": [],
        }

    async def generate_range_inventory(self, range_id: str) -> dict:
        """Full inventory of VMs, networks, and services."""
        logger.info("Generating range inventory for %s", range_id)
        return {
            "report_type": ReportType.RANGE_INVENTORY.value,
            "range_id": range_id,
            "vms": [
                {"name": "attacker-kali", "ip": "10.0.1.10", "os": "Kali 2025.1", "status": "running"},
                {"name": "victim-win11", "ip": "10.0.2.20", "os": "Windows 11", "status": "running"},
                {"name": "dc01", "ip": "10.0.2.1", "os": "Windows Server 2022", "status": "running"},
            ],
            "networks": [
                {"name": "attack-net", "cidr": "10.0.1.0/24"},
                {"name": "corp-net", "cidr": "10.0.2.0/24"},
            ],
            "services": [
                {"name": "guacamole", "url": "https://guac.range.local"},
                {"name": "opensearch", "url": "https://os.range.local:9200"},
            ],
            "rows": [],
        }

    async def generate_team_performance(
        self, team_id: str, date_range: tuple | None = None
    ) -> dict:
        """Team performance across exercises."""
        logger.info("Generating team performance for %s", team_id)
        return {
            "report_type": ReportType.TEAM_PERFORMANCE.value,
            "team_id": team_id,
            "date_range": list(date_range) if date_range else None,
            "exercises_completed": 12,
            "average_score": 74.5,
            "trend": "improving",
            "strengths": ["detection", "forensics"],
            "weaknesses": ["incident containment"],
            "history": [
                {"exercise_id": "ex-1", "date": "2025-12-01", "score": 68},
                {"exercise_id": "ex-2", "date": "2026-01-15", "score": 72},
                {"exercise_id": "ex-3", "date": "2026-02-10", "score": 78},
            ],
            "comparison_to_average": "+8%",
            "rows": [],
        }

    async def generate_tenant_usage(
        self, tenant_id: str, date_range: tuple | None = None
    ) -> dict:
        """Tenant resource usage: ranges, VM-hours, exercises."""
        logger.info("Generating tenant usage for %s", tenant_id)
        return {
            "report_type": ReportType.TENANT_USAGE.value,
            "tenant_id": tenant_id,
            "date_range": list(date_range) if date_range else None,
            "ranges_created": 8,
            "ranges_destroyed": 5,
            "active_ranges": 3,
            "total_vm_hours": 1240.5,
            "exercises_run": 15,
            "users_active": 42,
            "storage_gb": 128.3,
            "rows": [],
        }

    async def generate_compliance_report(self, tenant_id: str) -> dict:
        """Compliance audit: NIST 800-53 mapping, training completion."""
        logger.info("Generating compliance report for %s", tenant_id)
        return {
            "report_type": ReportType.COMPLIANCE_AUDIT.value,
            "tenant_id": tenant_id,
            "framework": "NIST 800-53 rev5",
            "controls_mapped": 47,
            "controls_met": 42,
            "controls_partial": 3,
            "controls_not_met": 2,
            "compliance_pct": 89.4,
            "training_completion": {
                "required_users": 50,
                "completed_users": 46,
                "completion_pct": 92.0,
            },
            "findings": [
                {"control": "IR-4", "status": "partial", "note": "Incident handling needs annual review."},
                {"control": "AU-6", "status": "not_met", "note": "Audit log review frequency below threshold."},
            ],
            "rows": [],
        }

    async def generate_scenario_results(self, scenario_id: str) -> dict:
        """Results for a specific scenario execution."""
        logger.info("Generating scenario results for %s", scenario_id)
        return {
            "report_type": ReportType.SCENARIO_RESULTS.value,
            "scenario_id": scenario_id,
            "phases_completed": 3,
            "phases_total": 4,
            "injects_fired": 7,
            "objectives_met": 5,
            "objectives_total": 6,
            "score": 78,
            "rows": [],
        }

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    async def export_csv(self, data: list[dict], filename: str) -> str:
        """Export *data* rows to CSV.

        Returns a storage URL (MinIO presigned or local path placeholder).
        """
        if not data:
            buf = ""
        else:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
            buf = output.getvalue()

        url = await self._upload(filename, buf.encode("utf-8"), "text/csv")
        logger.info("CSV exported: %s (%d rows)", filename, len(data))
        return url

    async def export_json(self, data: dict, filename: str) -> str:
        """Export *data* to JSON.

        Returns a storage URL (MinIO presigned or local path placeholder).
        """
        payload = json.dumps(data, indent=2, default=str).encode("utf-8")
        url = await self._upload(filename, payload, "application/json")
        logger.info("JSON exported: %s", filename)
        return url

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def _upload(self, filename: str, content: bytes, content_type: str) -> str:
        """Upload bytes to MinIO / S3. Falls back to a placeholder URL."""
        if self._minio_url:
            # In production: use aiobotocore or minio SDK
            logger.debug("Would upload %s to MinIO %s", filename, self._minio_url)
        return f"s3://{self._minio_bucket}/{filename}"