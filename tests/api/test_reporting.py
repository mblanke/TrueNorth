"""Tests for the TrueNorth Range reporting module."""

import pytest
from app.reporting import ReportGenerator, ReportResult, ReportType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gen(**kw) -> ReportGenerator:
    return ReportGenerator(**kw)


# ---------------------------------------------------------------------------
# ReportType enum
# ---------------------------------------------------------------------------


class TestReportType:
    def test_all_types_present(self):
        expected = {
            "exercise_aar",
            "range_inventory",
            "team_performance",
            "tenant_usage",
            "compliance_audit",
            "scenario_results",
        }
        actual = {rt.value for rt in ReportType}
        assert expected.issubset(actual)

    def test_enum_is_str(self):
        for rt in ReportType:
            assert isinstance(rt.value, str)


# ---------------------------------------------------------------------------
# ReportResult dataclass
# ---------------------------------------------------------------------------


class TestReportResult:
    def test_defaults(self):
        rr = ReportResult(
            report_type=ReportType.EXERCISE_AAR,
            format="json",
            storage_url="s3://reports/aar.json",
        )
        assert rr.generated_at  # auto timestamp
        assert rr.metadata == {}

    def test_to_dict(self):
        rr = ReportResult(
            report_type=ReportType.TEAM_PERFORMANCE,
            format="csv",
            storage_url="s3://reports/team.csv",
            metadata={"rows": 42},
        )
        d = rr.to_dict()
        assert d["report_type"] == "team_performance"
        assert d["format"] == "csv"
        assert d["metadata"]["rows"] == 42


# ---------------------------------------------------------------------------
# AAR report
# ---------------------------------------------------------------------------


class TestAARReport:
    @pytest.mark.asyncio
    async def test_structure(self):
        gen = _gen()
        report = await gen.generate_aar_report("ex-100")
        assert report["report_type"] == "exercise_aar"
        assert report["exercise_id"] == "ex-100"
        assert "executive_summary" in report
        assert isinstance(report["timeline"], list)
        assert len(report["timeline"]) > 0
        assert isinstance(report["objectives"], list)
        assert isinstance(report["findings"], list)
        assert isinstance(report["scores"], dict)
        assert "overall" in report["scores"]
        assert isinstance(report["recommendations"], list)

    @pytest.mark.asyncio
    async def test_scores_are_numeric(self):
        gen = _gen()
        report = await gen.generate_aar_report("ex-200")
        for key, val in report["scores"].items():
            assert isinstance(val, (int, float)), f"{key} score is not numeric"


# ---------------------------------------------------------------------------
# Range inventory report
# ---------------------------------------------------------------------------


class TestRangeInventoryReport:
    @pytest.mark.asyncio
    async def test_structure(self):
        gen = _gen()
        report = await gen.generate_range_inventory("rng-1")
        assert report["report_type"] == "range_inventory"
        assert report["range_id"] == "rng-1"
        assert isinstance(report["vms"], list)
        assert isinstance(report["networks"], list)
        assert isinstance(report["services"], list)

    @pytest.mark.asyncio
    async def test_vm_fields(self):
        gen = _gen()
        report = await gen.generate_range_inventory("rng-1")
        vm = report["vms"][0]
        assert "name" in vm
        assert "ip" in vm
        assert "os" in vm
        assert "status" in vm


# ---------------------------------------------------------------------------
# Team performance report
# ---------------------------------------------------------------------------


class TestTeamPerformanceReport:
    @pytest.mark.asyncio
    async def test_structure(self):
        gen = _gen()
        report = await gen.generate_team_performance("team-alpha")
        assert report["report_type"] == "team_performance"
        assert report["team_id"] == "team-alpha"
        assert isinstance(report["exercises_completed"], int)
        assert isinstance(report["average_score"], (int, float))
        assert isinstance(report["strengths"], list)
        assert isinstance(report["weaknesses"], list)
        assert isinstance(report["history"], list)

    @pytest.mark.asyncio
    async def test_with_date_range(self):
        gen = _gen()
        report = await gen.generate_team_performance("team-beta", date_range=("2026-01-01", "2026-02-28"))
        assert report["date_range"] == ["2026-01-01", "2026-02-28"]

    @pytest.mark.asyncio
    async def test_history_entries(self):
        gen = _gen()
        report = await gen.generate_team_performance("team-alpha")
        for entry in report["history"]:
            assert "exercise_id" in entry
            assert "date" in entry
            assert "score" in entry


# ---------------------------------------------------------------------------
# Tenant usage report
# ---------------------------------------------------------------------------


class TestTenantUsageReport:
    @pytest.mark.asyncio
    async def test_structure(self):
        gen = _gen()
        report = await gen.generate_tenant_usage("tenant-1")
        assert report["report_type"] == "tenant_usage"
        assert report["tenant_id"] == "tenant-1"
        assert isinstance(report["ranges_created"], int)
        assert isinstance(report["total_vm_hours"], (int, float))
        assert isinstance(report["exercises_run"], int)
        assert isinstance(report["users_active"], int)


# ---------------------------------------------------------------------------
# Compliance report
# ---------------------------------------------------------------------------


class TestComplianceReport:
    @pytest.mark.asyncio
    async def test_structure(self):
        gen = _gen()
        report = await gen.generate_compliance_report("tenant-2")
        assert report["report_type"] == "compliance_audit"
        assert report["framework"] == "NIST 800-53 rev5"
        assert isinstance(report["controls_mapped"], int)
        assert isinstance(report["compliance_pct"], (int, float))
        assert isinstance(report["training_completion"], dict)
        assert isinstance(report["findings"], list)

    @pytest.mark.asyncio
    async def test_findings_structure(self):
        gen = _gen()
        report = await gen.generate_compliance_report("tenant-2")
        for finding in report["findings"]:
            assert "control" in finding
            assert "status" in finding
            assert "note" in finding


# ---------------------------------------------------------------------------
# Scenario results report
# ---------------------------------------------------------------------------


class TestScenarioResultsReport:
    @pytest.mark.asyncio
    async def test_structure(self):
        gen = _gen()
        report = await gen.generate_scenario_results("sc-42")
        assert report["report_type"] == "scenario_results"
        assert report["scenario_id"] == "sc-42"
        assert isinstance(report["score"], (int, float))


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


class TestCSVExport:
    @pytest.mark.asyncio
    async def test_export_csv_returns_url(self):
        gen = _gen()
        rows = [
            {"name": "vm1", "status": "running"},
            {"name": "vm2", "status": "stopped"},
        ]
        url = await gen.export_csv(rows, "test.csv")
        assert url.startswith("s3://")
        assert "test.csv" in url

    @pytest.mark.asyncio
    async def test_export_csv_empty(self):
        gen = _gen()
        url = await gen.export_csv([], "empty.csv")
        assert url.startswith("s3://")

    @pytest.mark.asyncio
    async def test_export_json_returns_url(self):
        gen = _gen()
        url = await gen.export_json({"key": "value"}, "test.json")
        assert url.startswith("s3://")
        assert "test.json" in url


# ---------------------------------------------------------------------------
# generate() dispatcher
# ---------------------------------------------------------------------------


class TestGenerateDispatcher:
    @pytest.mark.asyncio
    async def test_dispatch_aar(self):
        gen = _gen()
        result = await gen.generate(ReportType.EXERCISE_AAR, {"exercise_id": "ex-dispatch"})
        assert isinstance(result, ReportResult)
        assert result.report_type == ReportType.EXERCISE_AAR
        assert result.format == "json"
        assert result.storage_url.startswith("s3://")

    @pytest.mark.asyncio
    async def test_dispatch_team_performance(self):
        gen = _gen()
        result = await gen.generate(ReportType.TEAM_PERFORMANCE, {"team_id": "t-1"})
        assert result.report_type == ReportType.TEAM_PERFORMANCE

    @pytest.mark.asyncio
    async def test_dispatch_unknown_raises(self):
        gen = _gen()
        with pytest.raises(ValueError, match="Unknown report type"):
            await gen.generate("not_a_type", {})

    @pytest.mark.asyncio
    async def test_report_result_to_dict(self):
        gen = _gen()
        result = await gen.generate(ReportType.RANGE_INVENTORY, {"range_id": "rng-x"})
        d = result.to_dict()
        assert d["report_type"] == "range_inventory"
        assert "generated_at" in d
