"""Tests for schema validation of content YAML files."""

from pathlib import Path

import pytest
import yaml

CONTENT_DIR = Path("content")


class TestInjectPacks:
    @pytest.fixture
    def inject_files(self):
        return list(CONTENT_DIR.glob("inject-packs/*.yaml"))

    def test_inject_packs_exist(self, inject_files):
        assert len(inject_files) >= 1, "No inject pack files found"

    def test_inject_packs_valid_yaml(self, inject_files):
        for f in inject_files:
            with open(f, encoding="utf-8-sig") as fh:
                data = yaml.safe_load(fh)
            assert "id" in data, f"{f.name} missing 'id'"
            assert "injects" in data, f"{f.name} missing 'injects'"

    def test_inject_packs_have_mitre(self, inject_files):
        for f in inject_files:
            with open(f, encoding="utf-8-sig") as fh:
                data = yaml.safe_load(fh)
            assert "mitre_attack" in data, f"{f.name} should have mitre_attack"


class TestDetectionRules:
    @pytest.fixture
    def detection_files(self):
        return list(CONTENT_DIR.glob("detections/*.yaml"))

    def test_detection_rules_exist(self, detection_files):
        assert len(detection_files) >= 1

    def test_detection_rules_valid_yaml(self, detection_files):
        for f in detection_files:
            with open(f, encoding="utf-8-sig") as fh:
                data = yaml.safe_load(fh)
            assert "title" in data, f"{f.name} missing 'title'"
            assert "detection" in data, f"{f.name} missing 'detection'"
            assert "level" in data, f"{f.name} missing 'level'"


class TestRangeTemplates:
    @pytest.fixture
    def template_files(self):
        return list(CONTENT_DIR.glob("ranges/*/template.yaml"))

    def test_templates_exist(self, template_files):
        assert len(template_files) >= 1

    def test_templates_valid_yaml(self, template_files):
        for f in template_files:
            with open(f, encoding="utf-8-sig") as fh:
                data = yaml.safe_load(fh)
            assert "assets" in data or "nodes" in data or "network" in data, f"{f.name} needs assets/nodes/network"


class TestDatasets:
    @pytest.fixture
    def dataset_files(self):
        return list(CONTENT_DIR.glob("datasets/*.yaml"))

    def test_datasets_exist(self, dataset_files):
        assert len(dataset_files) >= 1

    def test_datasets_valid_yaml(self, dataset_files):
        for f in dataset_files:
            with open(f, encoding="utf-8-sig") as fh:
                docs = list(yaml.safe_load_all(fh))
            # Find the first non-None document
            data = next((d for d in docs if d is not None), None)
            assert data is not None, f"{f.name} has no valid YAML document"
            assert "events" in data, f"{f.name} missing 'events'"
