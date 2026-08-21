"""Embedding-width resolution for curriculum RAG indexing.

The width of an embedding vector is a property of the served model, not a
setting, so `curriculum_ingest` resolves it instead of trusting a constant:

    live index mapping  ->  model probe  ->  EMBED_DIM pin  ->  fallback

These tests pin that order and — more importantly — pin the requirement that a
disagreement is *logged*. The original bug was not that a wrong width broke
indexing; it was that a wrong width dropped every vector, fell back to BM25, and
still reported success, so semantic search could be off indefinitely with
nothing in the logs to say so.
"""

from __future__ import annotations

import json
import logging

import pytest
import respx
from httpx import Response

CURRICULUM_ID = "11111111-2222-3333-4444-555555555555"
INDEX = f"curriculum-{CURRICULUM_ID}"
LOGGER = "truenorth.api.curriculum"


@pytest.fixture(autouse=True)
def reset_dim_state():
    """Module-level caches are process-wide; clear them around every test."""
    from app import curriculum_ingest as ci

    def _clear():
        ci._dim_cache.clear()
        ci._warned_mismatch.clear()
        ci._probed_dim = None
        ci._probe_done = False

    _clear()
    yield
    _clear()


def _embed_url() -> str:
    from app import curriculum_ingest as ci

    return f"{ci.AI_ORCHESTRATOR_URL}/ai/embedding"


def _mapping_url() -> str:
    from app import curriculum_ingest as ci

    return f"{ci.OPENSEARCH_URL}/{INDEX}/_mapping"


def _index_url() -> str:
    from app import curriculum_ingest as ci

    return f"{ci.OPENSEARCH_URL}/{INDEX}"


def _mapping_response(dimension: int) -> Response:
    return Response(
        200,
        json={INDEX: {"mappings": {"properties": {"embedding": {"dimension": dimension}}}}},
    )


def _embedding_response(width: int, model: str = "bge-small-en-v1.5") -> Response:
    return Response(200, json={"embedding": [0.01] * width, "model_used": model})


class TestEnsureIndex:
    @pytest.mark.asyncio
    @respx.mock
    async def test_adopts_the_width_the_model_actually_returns(self):
        """With no index yet, the probe decides — not the pin, not the default."""
        from app import curriculum_ingest as ci

        respx.get(_mapping_url()).mock(return_value=Response(404))
        respx.post(_embed_url()).mock(return_value=_embedding_response(384))
        created = respx.put(_index_url()).mock(return_value=Response(200, json={"acknowledged": True}))

        assert await ci.ensure_index(CURRICULUM_ID) == 384

        sent = json.loads(created.calls[0].request.content)
        assert sent["mappings"]["properties"]["embedding"]["dimension"] == 384

    @pytest.mark.asyncio
    @respx.mock
    async def test_existing_index_beats_the_model_and_says_so(self, caplog):
        """A live index's width cannot be changed in place, so it wins — loudly."""
        from app import curriculum_ingest as ci

        respx.get(_mapping_url()).mock(return_value=_mapping_response(1024))
        respx.post(_embed_url()).mock(return_value=_embedding_response(384))

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert await ci.ensure_index(CURRICULUM_ID) == 1024

        assert any("mismatch" in r.message.lower() for r in caplog.records), caplog.text

    @pytest.mark.asyncio
    @respx.mock
    async def test_pin_is_honoured_only_when_the_model_is_unreachable(self, monkeypatch):
        from app import curriculum_ingest as ci

        monkeypatch.setattr(ci, "_EMBED_DIM_PIN", 512)
        respx.get(_mapping_url()).mock(return_value=Response(404))
        respx.post(_embed_url()).mock(return_value=Response(503))
        respx.put(_index_url()).mock(return_value=Response(200, json={"acknowledged": True}))

        assert await ci.ensure_index(CURRICULUM_ID) == 512

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_when_there_is_neither_probe_nor_pin(self, monkeypatch):
        from app import curriculum_ingest as ci

        monkeypatch.setattr(ci, "_EMBED_DIM_PIN", None)
        respx.get(_mapping_url()).mock(return_value=Response(404))
        respx.post(_embed_url()).mock(return_value=Response(503))
        respx.put(_index_url()).mock(return_value=Response(200, json={"acknowledged": True}))

        assert await ci.ensure_index(CURRICULUM_ID) == ci.EMBED_DIM_FALLBACK


class TestIndexChunks:
    @pytest.mark.asyncio
    @respx.mock
    async def test_a_dropped_vector_is_never_silent(self, caplog):
        """The regression that started this: wrong width, no embedding, no warning."""
        from app import curriculum_ingest as ci

        respx.get(_mapping_url()).mock(return_value=_mapping_response(1024))
        respx.post(_embed_url()).mock(return_value=_embedding_response(384))
        bulk = respx.post(f"{ci.OPENSEARCH_URL}/_bulk").mock(
            return_value=Response(200, json={"items": [{"index": {"status": 201}}]})
        )

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            indexed, _ = await ci.index_chunks(CURRICULUM_ID, "doc-1", "notes.md", ["some chunk text"])

        assert indexed == 1
        # The chunk is still searchable by BM25, but carries no vector.
        assert '"embedding"' not in bulk.calls[0].request.content.decode()
        assert any("mismatch" in r.message.lower() for r in caplog.records), caplog.text

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_matching_vector_is_indexed(self):
        from app import curriculum_ingest as ci

        respx.get(_mapping_url()).mock(return_value=_mapping_response(384))
        respx.post(_embed_url()).mock(return_value=_embedding_response(384))
        bulk = respx.post(f"{ci.OPENSEARCH_URL}/_bulk").mock(
            return_value=Response(200, json={"items": [{"index": {"status": 201}}]})
        )

        indexed, model = await ci.index_chunks(CURRICULUM_ID, "doc-1", "notes.md", ["some chunk text"])

        assert indexed == 1
        assert model == "bge-small-en-v1.5"
        assert '"embedding"' in bulk.calls[0].request.content.decode()
