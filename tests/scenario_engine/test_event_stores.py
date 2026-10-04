"""Detection scoring runs against any event store, not OpenSearch specifically (ADR 0001)."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from scenario_engine.event_stores import (
    _REGISTRY,
    BaseEventStore,
    NullEventStore,
    OpenSearchEventStore,
    get_event_store,
)
from scenario_engine.scoring.engine import ScoringEngine
from scenario_engine.scoring.validators import ScoringValidator
from scenario_engine.validators.opensearch_query import OpenSearchQueryValidator

ROOT = Path(__file__).resolve().parents[2]
OS = "http://os.test:9200"

EVENTS = [
    {"process_name": "psexec.exe", "host": "ws01"},
    {"process_name": "rundll32.exe", "CommandLine": "rundll32 comsvcs.dll, MiniDump 624"},
    {"event_type": "http", "url.domain": "c2.evil.test"},
]


@pytest.mark.parametrize("name", sorted(_REGISTRY))
def test_registered_stores_implement_the_interface(name):
    cls = _REGISTRY[name]
    assert issubclass(cls, BaseEventStore) and not inspect.isabstract(cls)


def test_unknown_store_is_rejected():
    with pytest.raises(ValueError):
        get_event_store("splunk-someday")


@pytest.mark.parametrize(
    ("query", "total"),
    [
        ("process_name:psexec.exe", 1),
        ("process_name:rundll32.exe AND CommandLine:*comsvcs.dll*MiniDump*", 1),
        ("event_type:http AND url.domain:*evil*", 1),
        ('process_name:"psexec.exe"', 1),
        ("process_name:mimikatz.exe", 0),
        ("not a field query", 0),
    ],
)
def test_null_store_matches_field_terms(query, total):
    assert asyncio.run(NullEventStore(EVENTS).search("any", query)).total == total


@respx.mock
def test_opensearch_store_sends_lucene_as_query_string():
    route = respx.post(f"{OS}/truenorth-events-*/_search").mock(
        return_value=httpx.Response(
            200, json={"hits": {"total": {"value": 2}, "hits": [{"_id": "a", "_source": {"x": 1}}]}}
        )
    )
    found = asyncio.run(OpenSearchEventStore(OS).search("truenorth-events-*", "process_name:psexec.exe", size=5))
    assert found.total == 2 and found.hits == [{"_id": "a", "_source": {"x": 1}}]
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["query"] == {"query_string": {"query": "process_name:psexec.exe"}}
    assert body["size"] == 5


@respx.mock
def test_opensearch_store_passes_dsl_through():
    route = respx.post(f"{OS}/i/_search").mock(return_value=httpx.Response(200, json={"hits": {"total": 0}}))
    asyncio.run(OpenSearchEventStore(OS).search("i", {"match_all": {}}))
    import json

    assert json.loads(route.calls.last.request.content)["query"] == {"match_all": {}}


def test_scoring_validator_uses_any_store():
    store = NullEventStore(EVENTS)
    ok, evidence = asyncio.run(
        ScoringValidator.validate("opensearch_query", {"query": "host:ws01", "threshold": 1}, event_store=store)
    )
    assert ok is True and evidence[0]["_source"]["host"] == "ws01"
    ok, _ = asyncio.run(
        ScoringValidator.validate("opensearch_query", {"query": "host:ws01", "threshold": 2}, event_store=store)
    )
    assert ok is False


@respx.mock
def test_scoring_validator_opensearch_url_still_works():
    """Was aiohttp, which scenario-engine never installed: it always failed outside tests."""
    respx.post(f"{OS}/truenorth-*/_search").mock(
        return_value=httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": []}})
    )
    ok, _ = asyncio.run(ScoringValidator.validate("opensearch_query", {"query": "a:b"}, opensearch_url=OS))
    assert ok is True


def test_store_outage_means_not_achieved_not_a_crash():
    class Down(BaseEventStore):
        async def search(self, index, query, size=20):
            raise ConnectionError("down")

    ok, evidence = asyncio.run(ScoringValidator.validate("opensearch_query", {"query": "a:b"}, event_store=Down()))
    assert (ok, evidence) == (False, [])


def test_scoring_engine_threads_the_store_through():
    eng = ScoringEngine(
        "ex-1",
        [
            {
                "id": "o1",
                "name": "Detect PsExec",
                "max_points": 10,
                "validation_method": "opensearch_query",
                "validation_config": {"query": "process_name:psexec.exe"},
            }
        ],
        event_store=NullEventStore(EVENTS),
    )
    result = asyncio.run(eng.evaluate_objective("o1"))
    assert result.achieved is True


def test_class_validator_reads_the_store_from_context():
    v = OpenSearchQueryValidator({"query": "process_name:psexec.exe"})
    assert v.validate({"event_store": NullEventStore(EVENTS)}) is True
    assert v.validate({}) is False


def _scenario_queries() -> list[tuple[str, str]]:
    out = []
    for path in sorted((ROOT / "content/scenarios").glob("*/scenario.yaml")):
        for obj in yaml.safe_load(path.read_text()).get("objectives", []):
            q = (obj.get("params") or {}).get("query")
            if obj.get("validator") in ("opensearch_query", "validate.opensearch_query") and isinstance(q, str):
                out.append((f"{path.parent.name}:{obj.get('id')}", q))
    return out


@pytest.mark.parametrize(("ref", "query"), _scenario_queries(), ids=[r for r, _ in _scenario_queries()])
def test_scenario_queries_are_plain_lucene_strings(ref, query):
    """Content stays portable: Lucene query_string, no engine-specific DSL embedded."""
    assert query.strip() and not query.lstrip().startswith("{")
