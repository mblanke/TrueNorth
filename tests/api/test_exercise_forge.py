"""Tests for Exercise Forge API endpoints."""

from unittest.mock import AsyncMock, patch

import pytest


class TestForgePresets:
    def test_list_presets(self, client):
        resp = client.get("/exercise-forge/presets")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 4
        for preset in data:
            assert "name" in preset
            assert "difficulty" in preset
            assert "duration_minutes" in preset
            assert "objective_count" in preset
            assert "focus_areas" in preset
            assert "description" in preset

    def test_preset_difficulty_values(self, client):
        resp = client.get("/exercise-forge/presets")
        data = resp.json()
        difficulties = {p["difficulty"] for p in data}
        assert difficulties == {"beginner", "intermediate", "advanced", "expert"}


class TestForgePreview:
    @patch("app.routers.exercise_forge._call_forge_ai", new_callable=AsyncMock)
    def test_preview_with_manual_indicators(self, mock_ai, client):
        mock_ai.return_value = (
            "name: Test Exercise\ntimeline:\n  - inject: phish_email\n    t: '0:05'\n    technique: T1566.001",
            "gpt-4o",
        )
        payload = {
            "indicators": [
                {
                    "indicator_type": "ipv4",
                    "value": "10.0.0.1",
                    "severity": "high",
                    "mitre_attack_ids": ["T1566.001"],
                }
            ],
            "difficulty": "intermediate",
            "duration_minutes": 60,
            "objective_count": 4,
        }
        resp = client.post("/exercise-forge/preview", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert "scenario_yaml" in data
        assert data["model_used"] == "gpt-4o"
        assert data["indicators_used"] == 1
        assert "T1566.001" in data["mitre_techniques"]
        assert data["estimated_duration_minutes"] == 60

    def test_preview_no_indicators_returns_422(self, client):
        payload = {
            "indicators": [],
            "difficulty": "beginner",
            "duration_minutes": 30,
            "objective_count": 3,
        }
        resp = client.post("/exercise-forge/preview", json=payload)
        assert resp.status_code == 422

    def test_preview_invalid_difficulty_returns_422(self, client):
        payload = {
            "indicators": [{"indicator_type": "ipv4", "value": "1.2.3.4", "severity": "high"}],
            "difficulty": "invalid_level",
            "duration_minutes": 30,
            "objective_count": 3,
        }
        resp = client.post("/exercise-forge/preview", json=payload)
        assert resp.status_code == 422


class TestForgeGenerate:
    @patch("app.routers.exercise_forge._call_forge_ai", new_callable=AsyncMock)
    def test_generate_creates_exercise(self, mock_ai, client, db_session):
        """Generate endpoint creates Scenario + Exercise + ForgedExercise records."""
        from app.models import Range, Template

        # Use the seeded default tenant (matches AUTH_DISABLED dev user)
        dev_tenant_id = "00000000-0000-0000-0000-000000000001"

        tpl = Template(name="Test TPL", version="1.0", yaml="nodes: []", tenant_id=dev_tenant_id)
        db_session.add(tpl)
        db_session.flush()

        rng = Range(
            name="Forge Range",
            template_id=tpl.id,
            tenant_id=dev_tenant_id,
            state="ready",
        )
        db_session.add(rng)
        db_session.commit()

        mock_ai.return_value = (
            "name: APT29 Replay\ntimeline:\n  - inject: c2_beacon\n    t: '0:05'\n    technique: T1059.001\nobjectives:\n  - detect lateral movement",
            "gpt-4o",
        )

        payload = {
            "indicators": [
                {
                    "indicator_type": "domain",
                    "value": "evil-c2.example.com",
                    "severity": "critical",
                    "mitre_attack_ids": ["T1059.001", "T1071.001"],
                    "description": "Known C2 domain",
                },
            ],
            "difficulty": "advanced",
            "duration_minutes": 120,
            "objective_count": 5,
            "range_template": "medium-enterprise",
            "focus_areas": ["detection", "containment"],
        }
        resp = client.post("/exercise-forge/generate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert "exercise_id" in data
        assert "scenario_id" in data
        assert data["model_used"] == "gpt-4o"
        assert data["indicators_used"] == 1
        assert "T1059.001" in data["mitre_techniques"]

    def test_generate_no_indicators_returns_422(self, client):
        payload = {
            "indicators": [],
            "difficulty": "beginner",
            "duration_minutes": 30,
            "objective_count": 3,
        }
        resp = client.post("/exercise-forge/generate", json=payload)
        assert resp.status_code == 422


class TestForgeSchemas:
    def test_forge_request_defaults(self):
        from app.schemas import ForgeRequest

        req = ForgeRequest(
            indicators=[],
        )
        assert req.difficulty == "intermediate"
        assert req.duration_minutes == 60
        assert req.objective_count == 4
        assert req.range_template == "small-enterprise"

    def test_forge_request_validates_difficulty(self):
        from app.schemas import ForgeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ForgeRequest(indicators=[], difficulty="legendary")

    def test_forge_request_validates_duration_range(self):
        from app.schemas import ForgeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ForgeRequest(indicators=[], duration_minutes=5)  # too low
        with pytest.raises(ValidationError):
            ForgeRequest(indicators=[], duration_minutes=999)  # too high

    def test_forge_indicator_in_minimal(self):
        from app.schemas import ForgeIndicatorIn

        ind = ForgeIndicatorIn(indicator_type="ipv4", value="10.0.0.1")
        assert ind.severity == "medium"
        assert ind.mitre_attack_ids == []

    def test_forge_preset_out(self):
        from app.schemas import ForgePresetOut

        p = ForgePresetOut(
            name="Quick Drill",
            difficulty="beginner",
            duration_minutes=30,
            objective_count=3,
            focus_areas=["detection"],
            description="Short drill",
        )
        assert p.name == "Quick Drill"


class TestForgeMitreExtraction:
    """Test the MITRE technique regex extraction helper."""

    def _get_extract_mitre(self):
        from app.routers.exercise_forge import _extract_mitre

        return _extract_mitre

    def test_extracts_techniques(self):
        extract = self._get_extract_mitre()
        yaml_text = """
name: Test
timeline:
  - inject: phish
    technique: T1566.001
  - inject: exec
    technique: T1059.003
  - inject: persist
    technique: T1053.005
"""
        result = extract(yaml_text)
        assert "T1566.001" in result
        assert "T1059.003" in result
        assert "T1053.005" in result
        # Should deduplicate
        assert len(result) == len(set(result))

    def test_extracts_base_technique_ids(self):
        extract = self._get_extract_mitre()
        yaml_text = "technique: T1059\ntechnique: T1566"
        result = extract(yaml_text)
        assert "T1059" in result
        assert "T1566" in result
