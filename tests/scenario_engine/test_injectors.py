"""Tests for scenario engine injector/validator framework."""


class TestInjectorRegistry:
    def test_import_injectors(self):
        """Verify injector modules can be imported."""
        from scenario_engine.injectors import (
            dns_spike,
        )

        assert dns_spike is not None

    def test_dns_spike_injector_class_exists(self):
        from scenario_engine.injectors.dns_spike import DnsSpikeInjector

        assert hasattr(DnsSpikeInjector, "execute")

    def test_http_burst_injector_class_exists(self):
        from scenario_engine.injectors.http_burst import HttpBurstInjector

        assert hasattr(HttpBurstInjector, "execute")


class TestValidatorRegistry:
    def test_import_validators(self):
        from scenario_engine.validators import opensearch_query

        assert opensearch_query is not None

    def test_opensearch_validator_class_exists(self):
        from scenario_engine.validators.opensearch_query import OpenSearchQueryValidator

        assert hasattr(OpenSearchQueryValidator, "validate")


class TestInjectorExecution:
    def test_dns_spike_execute(self):
        from scenario_engine.injectors.dns_spike import DnsSpikeInjector

        inj = DnsSpikeInjector({"domains": ["evil.com"], "count": 5})
        result = inj.execute({})
        assert result["total_queries"] == 5
        assert len(result["results"]) == 5

    def test_http_burst_execute(self):
        from scenario_engine.injectors.http_burst import HttpBurstInjector

        inj = HttpBurstInjector({"url": "http://c2.evil.com/beacon", "count": 3, "method": "POST"})
        result = inj.execute({})
        assert result["total_requests"] == 3
        assert result["url"] == "http://c2.evil.com/beacon"

    def test_email_phish_execute(self):
        from scenario_engine.injectors.email_phish import EmailPhishInjector

        inj = EmailPhishInjector({"sender": "a@b.com", "recipient": "v@c.com", "subject": "Test"})
        result = inj.execute({})
        assert result["delivered"] is True


class TestScenarioYAML:
    def test_load_apt_breach_scenario(self):
        from pathlib import Path

        import yaml

        path = Path("scenario-engine/examples/apt-breach.yaml")
        if path.exists():
            with open(path, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f)
            assert "id" in data or "name" in data
            assert "timeline" in data or "events" in data

    def test_load_insider_threat_scenario(self):
        from pathlib import Path

        import yaml

        path = Path("scenario-engine/examples/insider-threat.yaml")
        if path.exists():
            with open(path, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f)
            assert "id" in data or "name" in data
