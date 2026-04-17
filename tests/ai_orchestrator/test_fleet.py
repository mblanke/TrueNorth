"""Tests for AI Orchestrator fleet management, model tagging, and size hints."""

import importlib.util
import os
import sys

# ── Load AI orchestrator module (avoid collision with control-plane app) ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "ai-orchestrator", "app", "main.py"))
_MOD_NAME = "_ai_orch_fleet"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _MOD_PATH)
_ai = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = _ai  # required for @dataclass in Python 3.13
_spec.loader.exec_module(_ai)

_tag_model = _ai._tag_model
_model_size_hint = _ai._model_size_hint
_parse_fleet = _ai._parse_fleet
OllamaNode = _ai.OllamaNode


# ── Model Tagging ──────────────────────────────────────────────────────


class TestModelTagging:
    def test_tag_large_model(self):
        tags = _tag_model("llama3.1:70b-instruct-q5_K_M")
        assert tags == {"general", "large", "instruct", "instruct-large"}

    def test_tag_code_model(self):
        tags = _tag_model("qwen2.5-coder:32b")
        assert "code" in tags
        assert "general" in tags

    def test_tag_embedding_model(self):
        tags = _tag_model("bge-m3:latest")
        assert "embedding" in tags
        assert "general" not in tags

    def test_tag_vision_model(self):
        tags = _tag_model("llava:13b")
        assert "vision" in tags
        assert "general" in tags

    def test_tag_small_instruct(self):
        tags = _tag_model("qwen2.5:14b-instruct")
        assert "instruct" in tags
        assert "general" in tags
        assert "large" not in tags

    def test_tag_unknown_model(self):
        tags = _tag_model("custom-model:latest")
        assert tags == {"general"}

    def test_tag_mixtral_large(self):
        tags = _tag_model("mixtral:8x22b-instruct")
        assert tags == {"general", "large", "instruct", "instruct-large"}


# ── Model Size Hint ────────────────────────────────────────────────────


class TestModelSizeHint:
    def test_size_70b(self):
        assert _model_size_hint("llama3.1:70b-instruct-q5_K_M") == 70

    def test_size_8x22b(self):
        assert _model_size_hint("mixtral:8x22b") == 176

    def test_size_no_size(self):
        assert _model_size_hint("codestral:latest") == 0

    def test_size_32b(self):
        assert _model_size_hint("qwen2.5-coder:32b") == 32


# ── Fleet Parsing ──────────────────────────────────────────────────────


class TestFleetParsing:
    def test_parse_single_node(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_NODES", "gpu1=http://10.0.1.10:11434")
        fleet = _parse_fleet()
        assert len(fleet) == 1
        assert "gpu1" in fleet
        assert fleet["gpu1"].base_url == "http://10.0.1.10:11434"

    def test_parse_multiple_nodes(self, monkeypatch):
        monkeypatch.setenv(
            "OLLAMA_NODES",
            "gpu1=http://10.0.1.10:11434,gpu2=http://10.0.1.11:11434",
        )
        fleet = _parse_fleet()
        assert len(fleet) == 2
        assert "gpu1" in fleet and "gpu2" in fleet

    def test_parse_empty(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_NODES", "")
        fleet = _parse_fleet()
        assert len(fleet) == 0

    def test_parse_malformed(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_NODES", "badentry,also bad,ok=http://host:1234")
        fleet = _parse_fleet()
        assert len(fleet) == 1
        assert "ok" in fleet
