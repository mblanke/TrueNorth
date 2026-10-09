"""Telemetry indices are green on a single OpenSearch node.

A fixed number_of_replicas: 1 on the default single-node install left every index yellow
(the replica has nowhere to go) and the API's deep health "degraded" (staging, 2026-10-09).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _bootstrap(monkeypatch, replicas: str | None):
    if replicas is None:
        monkeypatch.delenv("OPENSEARCH_REPLICAS", raising=False)
    else:
        monkeypatch.setenv("OPENSEARCH_REPLICAS", replicas)
    sys.path.insert(0, str(REPO / "telemetry"))
    try:
        sys.modules.pop("pipelines.bootstrap", None)
        return importlib.import_module("pipelines.bootstrap")
    finally:
        sys.path.pop(0)


def test_templates_expand_replicas_with_the_cluster_by_default(monkeypatch) -> None:
    bootstrap = _bootstrap(monkeypatch, None)
    for name, template in bootstrap.TEMPLATES.items():
        settings = template["template"]["settings"]
        assert settings.get("auto_expand_replicas") == "0-1", name
        assert "number_of_replicas" not in settings, name


def test_a_fixed_replica_count_is_honoured(monkeypatch) -> None:
    bootstrap = _bootstrap(monkeypatch, "2")
    for template in bootstrap.TEMPLATES.values():
        assert template["template"]["settings"]["number_of_replicas"] == 2


def test_the_ism_policy_never_pins_a_replica_count_in_warm(monkeypatch) -> None:
    bootstrap = _bootstrap(monkeypatch, None)
    states = {s["name"]: s for s in bootstrap.ISM_POLICY["policy"]["states"]}
    assert not any("replica_count" in a for a in states["warm"]["actions"])


def test_existing_indices_are_brought_to_the_replica_settings(monkeypatch) -> None:
    bootstrap = _bootstrap(monkeypatch, None)
    calls = []

    class _Client:
        def put(self, path, params=None, json=None):
            calls.append((path, json))

            class _R:
                status_code = 200

            return _R()

    bootstrap.apply_replica_settings(_Client())
    patterns = {p for t in bootstrap.TEMPLATES.values() for p in t["index_patterns"]} | {".opendistro-ism-config"}
    assert {c[0] for c in calls} == {f"/{p}/_settings" for p in patterns}
    assert all(c[1] == {"index": {"auto_expand_replicas": "0-1"}} for c in calls)
