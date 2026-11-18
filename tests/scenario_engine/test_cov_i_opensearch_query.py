"""COV-I: scenario_engine/validators/opensearch_query.py — every branch of
``OpenSearchQueryValidator.validate`` against the shipped offline store.

Whole-slice coverage before this file: 80.0% (20/25 statements); the missing lines were
the no-query refusal and the store-failure paths.
"""

from __future__ import annotations

import asyncio

from scenario_engine.event_stores import EventHits, NullEventStore
from scenario_engine.validators.opensearch_query import DEFAULT_INDEX, OpenSearchQueryValidator


class _Recording(NullEventStore):
    """The shipped offline store, remembering what it was asked (index, size)."""

    def __init__(self, events=None):
        super().__init__(events)
        self.asked: list[tuple[str, object, int]] = []

    async def search(self, index, query, size=20, offset=0):
        self.asked.append((index, query, size))
        return await super().search(index, query, size, offset)


class _Broken(NullEventStore):
    async def search(self, index, query, size=20, offset=0):
        raise ConnectionError("store down")


def _validate(params, store, context=None):
    return OpenSearchQueryValidator(params).validate({"event_store": store, **(context or {})})


def test_a_matching_query_passes_at_the_default_min_hits():
    store = _Recording([{"event_type": "http", "url.domain": "cdn.example"}])
    assert _validate({"query": "event_type:http AND url.domain:cdn.example"}, store) is True
    assert store.asked == [(DEFAULT_INDEX, "event_type:http AND url.domain:cdn.example", 1)]


def test_fewer_hits_than_min_hits_fails():
    store = _Recording([{"event_type": "http"}])
    assert _validate({"query": "event_type:http", "min_hits": 2}, store) is False


def test_min_hits_is_read_from_the_param_not_the_environment():
    # A string min_hits (as YAML often delivers it) and a 0 both floor at 1.
    one = _Recording([{"a": "b"}])
    assert _validate({"query": "a:b", "min_hits": "1"}, one) is True
    assert _validate({"query": "a:b", "min_hits": 0}, _Recording([{"a": "b"}])) is True
    assert _validate({"query": "a:b", "min_hits": 0}, _Recording([])) is False


def test_the_index_param_names_the_index_asked():
    store = _Recording([])
    _validate({"query": "a:b", "index": "range-r-1,range-r-1-*"}, store)
    assert store.asked[0][0] == "range-r-1,range-r-1-*"


def test_no_query_is_refused_not_match_all():
    # Before the refusal any event at all passed a queryless objective.
    assert _validate({}, _Recording([{"a": "b"}])) is False
    assert _validate({"query": ""}, _Recording([{"a": "b"}])) is False


def test_no_event_store_in_context_fails_without_raising():
    assert _validate({"query": "a:b"}, None) is False
    assert OpenSearchQueryValidator({"query": "a:b"}).validate({}) is False


def test_a_store_error_means_not_achieved():
    assert _validate({"query": "a:b"}, _Broken()) is False


def test_validation_inside_a_running_loop_fails_cleanly():
    """The engine calls validators from a worker thread; a caller that already holds a
    loop cannot run asyncio.run and must get False, not a RuntimeError."""

    async def inside():
        return _validate({"query": "a:b"}, _Recording([{"a": "b"}]))

    assert asyncio.run(inside()) is False


def test_the_offline_store_pages_one_hit_at_a_time():
    store = _Recording([{"a": "b"}] * 3)
    hits = asyncio.run(store.search(DEFAULT_INDEX, "a:b", size=1, offset=2))
    assert isinstance(hits, EventHits) and hits.total == 3 and len(hits.hits) == 1
    assert hits.hits[0]["_id"] == "2" and hits.hits[0]["_source"] == {"a": "b"}
