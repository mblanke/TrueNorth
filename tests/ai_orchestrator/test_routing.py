"""Tests for AI Orchestrator tag-based model routing."""
import importlib.util
import os
import sys

import pytest

# ── Load AI orchestrator module ────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "..", "ai-orchestrator", "app", "main.py")
)
_MOD_NAME = "_ai_orch_routing"
if _MOD_NAME in sys.modules:
    _ai = sys.modules[_MOD_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _MOD_PATH)
    _ai = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = _ai
    _spec.loader.exec_module(_ai)

_find_best_for_tags = _ai._find_best_for_tags
_tag_model = _ai._tag_model
OllamaNode = _ai.OllamaNode


def _node(name, models, healthy=True):
    """Build an OllamaNode with pre-tagged models."""
    n = OllamaNode(name=name, base_url=f"http://{name}:11434")
    n.healthy = healthy
    n.models = models
    n.tagged_models = {m: _tag_model(m) for m in models}
    return n


# ── Tag Routing ────────────────────────────────────────────────────────

class TestTagRouting:
    def test_find_best_prefers_large(self, monkeypatch):
        """70b instruct model is preferred over 13b for 'instruct-large' tag."""
        fleet = {
            "a": _node("a", ["llama3.1:70b-instruct-q5_K_M"]),
            "b": _node("b", ["llama3.1:13b-instruct"]),
        }
        monkeypatch.setattr(_ai, "FLEET", fleet)
        result = _find_best_for_tags(["instruct-large", "instruct", "general"])
        assert result is not None
        model, node = result
        assert "70b" in model
        assert node.name == "a"

    def test_find_best_code_tag(self, monkeypatch):
        """Code-tagged coder model is selected for 'code' tag."""
        fleet = {
            "code": _node("code", ["qwen2.5-coder:32b"]),
            "gen": _node("gen", ["llama3.1:8b"]),
        }
        monkeypatch.setattr(_ai, "FLEET", fleet)
        result = _find_best_for_tags(["code", "general"])
        assert result is not None
        assert "coder" in result[0]

    def test_find_best_fallback(self, monkeypatch):
        """If no instruct-large exists, falls through to general."""
        fleet = {"g": _node("g", ["llama3.1:8b"])}
        monkeypatch.setattr(_ai, "FLEET", fleet)
        result = _find_best_for_tags(["instruct-large", "instruct", "general"])
        assert result is not None
        assert result[1].name == "g"

    def test_no_healthy_nodes_returns_none(self, monkeypatch):
        """All-unhealthy fleet returns None."""
        fleet = {
            "down": _node("down", ["llama3.1:70b-instruct-q5_K_M"], healthy=False),
        }
        monkeypatch.setattr(_ai, "FLEET", fleet)
        assert _find_best_for_tags(["instruct-large", "instruct", "general"]) is None

    def test_load_balancing(self, monkeypatch):
        """Node with fewer in-flight requests is preferred (same model size)."""
        n1 = _node("n1", ["llama3.1:70b-instruct-q5_K_M"])
        n2 = _node("n2", ["llama3.1:70b-instruct-q5_K_M"])
        n1._inflight = 5
        n2._inflight = 0
        fleet = {"n1": n1, "n2": n2}
        monkeypatch.setattr(_ai, "FLEET", fleet)
        result = _find_best_for_tags(["instruct-large"])
        assert result is not None
        assert result[1].name == "n2"

    def test_empty_fleet_returns_none(self, monkeypatch):
        """Empty fleet always returns None."""
        monkeypatch.setattr(_ai, "FLEET", {})
        assert _find_best_for_tags(["general"]) is None

    def test_embedding_tag_routes_to_embedding_model(self, monkeypatch):
        """Embedding tag resolves to bge model, not general LLM."""
        fleet = {
            "e": _node("e", ["bge-m3:latest"]),
            "g": _node("g", ["llama3.1:8b"]),
        }
        monkeypatch.setattr(_ai, "FLEET", fleet)
        result = _find_best_for_tags(["embedding"])
        assert result is not None
        assert "bge" in result[0]

    def test_vision_tag(self, monkeypatch):
        """Vision tag routes to llava model."""
        fleet = {"v": _node("v", ["llava:13b", "llama3.1:8b"])}
        monkeypatch.setattr(_ai, "FLEET", fleet)
        result = _find_best_for_tags(["vision"])
        assert result is not None
        assert "llava" in result[0]