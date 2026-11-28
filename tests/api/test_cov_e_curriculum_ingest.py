# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/curriculum_ingest.py: extraction, chunking and retrieval.

``test_curriculum_embed_dim.py`` pins the embedding-width resolution and
``test_curriculum_url_guard.py`` pins the SSRF guard; this file covers the rest
of the ingest pipeline: the file-type dispatch (docx / pptx / markdown / html /
refusals), the paragraph-window chunker, the PDF-over-URL branch of
``fetch_url_text``, the embedding fallbacks and the retrieval side
(``rag_search``, ``delete_index``, the lost create race). The documents are real
bytes built with python-docx / python-pptx / pypdf; only the network
collaborators (AI orchestrator, OpenSearch, ``net_guard.fetch``) are faked.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import types

import pytest
import respx
from app import curriculum_ingest as ci
from app import net_guard
from httpx import Response

LOGGER = "truenorth.api.curriculum"
CURRICULUM_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INDEX = f"curriculum-{CURRICULUM_ID}"


@pytest.fixture(autouse=True)
def reset_dim_state():
    """Module-level caches are process-wide; clear them around every test."""
    ci._dim_cache.clear()
    ci._warned_mismatch.clear()
    ci._probed_dim = None
    ci._probe_done = False
    yield
    ci._dim_cache.clear()
    ci._warned_mismatch.clear()
    ci._probed_dim = None
    ci._probe_done = False


def _os() -> str:
    """The vector store the module talks to (app/vector_backends, default opensearch)."""
    from app.vector_backends import get_vector_store

    return get_vector_store().url


def _embed_url() -> str:
    return f"{ci.AI_ORCHESTRATOR_URL}/ai/embedding"


def _mapping_url() -> str:
    return f"{_os()}/{INDEX}/_mapping"


def _index_url() -> str:
    return f"{_os()}/{INDEX}"


def _mapping_response(dimension: int) -> Response:
    return Response(200, json={INDEX: {"mappings": {"properties": {"embedding": {"dimension": dimension}}}}})


def _embedding(width: int, model: str = "bge-small-en-v1.5") -> Response:
    return Response(200, json={"embedding": [0.01] * width, "model_used": model})


# -- text extraction --------------------------------------------------------------------------


def _docx_bytes() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Mandatory reading")
    doc.add_paragraph("")  # blank paragraphs are dropped
    doc.add_paragraph("Chapter one")
    row = doc.add_table(rows=1, cols=2).rows[0]
    row.cells[0].text = "callsign"
    row.cells[1].text = " Talon "
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_a_docx_is_extracted_with_its_tables():
    text = ci.extract_text("lesson.docx", _docx_bytes())
    assert text == "Mandatory reading\nChapter one\ncallsign | Talon"
    # the dispatch also keys off the mime type, not just the extension
    assert (
        ci.extract_text(
            "upload.bin",
            _docx_bytes(),
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        == text
    )


def _pptx_bytes() -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])  # title + content
    slide.shapes.title.text = "Network recon"
    slide.placeholders[1].text_frame.text = "Map the subnet"
    slide.notes_slide.notes_text_frame.text = "Ask for volunteers"
    prs.slides.add_slide(prs.slide_layouts[6])  # blank: produces no text at all
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_a_pptx_is_extracted_with_speaker_notes():
    text = ci.extract_text("deck.pptx", _pptx_bytes())
    assert text.split("\n\n")[0].splitlines() == [
        "[Slide 1]",
        "Network recon",
        "Map the subnet",
        "(Notes: Ask for volunteers)",
    ]
    assert text.split("\n\n")[1] == "[Slide 2]"


def test_markdown_text_and_html_take_their_own_extractors():
    assert ci.extract_text("notes.md", b"# Title\nbody") == "# Title\nbody"
    assert ci.extract_text("notes.markdown", b"intro") == "intro"
    assert ci.extract_text("notes.rst", b"intro") == "intro"
    assert ci.extract_text("raw.txt", b"plain", mime_type="text/plain") == "plain"
    # the html branch keys off the extension without a mime...
    html = ci.extract_text("page.htm", b"<h1>Hi</h1>")
    assert "Hi" in html and "<h1>" not in html
    # ...and off a non-text html mime
    assert "Hi" in ci.extract_text("page.html", b"<h1>Hi</h1>", mime_type="application/xhtml+xml")


def test_an_unsupported_type_names_the_file_and_the_mime():
    with pytest.raises(ValueError) as refused:
        ci.extract_text("asset.exe", b"MZ")
    assert str(refused.value) == "Unsupported file type: asset.exe (unknown mime)"

    with pytest.raises(ValueError) as by_mime:
        ci.extract_text("asset", b"\x89PNG", mime_type="image/png")
    assert str(by_mime.value) == "Unsupported file type: asset (image/png)"


# -- URL ingest -------------------------------------------------------------------------------


def test_a_broken_or_zero_byte_limit_falls_back(monkeypatch):
    monkeypatch.setenv("CURRICULUM_URL_MAX_BYTES", "not-a-number")
    assert ci._url_max_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv("CURRICULUM_URL_MAX_BYTES", "0")
    assert ci._url_max_bytes() == 1


def test_a_pdf_url_keeps_its_filename_as_the_title(monkeypatch):
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    writer.write(buf)

    def fake_fetch(url, **_):
        return types.SimpleNamespace(content_type="application/pdf", body=buf.getvalue())

    monkeypatch.setattr(net_guard, "fetch", fake_fetch)

    title, text = asyncio.run(ci.fetch_url_text("https://docs.example.org/handbook.pdf"))
    assert (title, text) == ("handbook.pdf", "")  # a blank page extracts to nothing

    # a URL with no last path segment falls back to the whole URL as the title
    title, _ = asyncio.run(ci.fetch_url_text("https://docs.example.org/files/"))
    assert title == "https://docs.example.org/files/"


# -- chunking ---------------------------------------------------------------------------------


def test_empty_and_whitespace_yield_no_chunks():
    assert ci.chunk_text("") == []
    assert ci.chunk_text("  \n\n\n\n  ") == []


def test_a_short_text_is_one_untouched_chunk():
    assert ci.chunk_text("one two three") == ["one two three"]


def test_runs_of_blank_lines_are_collapsed():
    assert ci.chunk_text("a\n\n\n\n\nb") == ["a\n\nb"]


def test_long_text_breaks_on_paragraph_boundaries_with_overlap():
    text = "\n\n".join(f"Paragraph {i} explains a facet of the exercise in two full sentences." for i in range(200))
    assert len(text) > 2 * ci.CHUNK_CHARS

    chunks = ci.chunk_text(text)

    assert len(chunks) > 2
    for chunk in chunks:
        assert len(chunk) <= ci.CHUNK_CHARS
        # every break lands after a sentence (the window always holds a paragraph past halfway)
        assert chunk.endswith(".")
    assert chunks[0].startswith("Paragraph 0 explains")
    assert chunks[-1].endswith(text.rsplit("\n\n", 1)[-1])
    # consecutive windows overlap by about CHUNK_OVERLAP characters
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt[:80] in prev


def test_prose_without_paragraphs_breaks_on_sentences():
    text = "The gateway accepts exactly one configuration per vlan. " * 100
    chunks = ci.chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.endswith(".")
        assert len(chunk) <= ci.CHUNK_CHARS


def test_prose_without_any_separators_is_cut_raw():
    text = "x" * (3 * ci.CHUNK_CHARS + 500)
    chunks = ci.chunk_text(text)
    assert chunks[0] == "x" * ci.CHUNK_CHARS
    # the windows cover the whole text, overlap included
    assert sum(len(c) for c in chunks) == len(text) + (len(chunks) - 1) * ci.CHUNK_OVERLAP


# -- embedding --------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_any_other_transport_failure_indexes_text_only(caplog):
    respx.post(_embed_url()).mock(side_effect=RuntimeError("nameserver exploded"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        vector, model = await ci.embed_text("chunk text")

    assert vector is None and model == ""
    assert any("Embedding unavailable" in r.message for r in caplog.records), caplog.text


# -- the dimension probe ----------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_the_probe_runs_once():
    respx.post(_embed_url()).mock(return_value=_embedding(384))

    assert await ci._probe_embedding_dim() == 384
    assert await ci._probe_embedding_dim() == 384  # answered from the cache, no second call
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_pin_that_contradicts_the_model_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(ci, "_EMBED_DIM_PIN", 512)
    respx.post(_embed_url()).mock(return_value=_embedding(384))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await ci._probe_embedding_dim() == 384

    assert any("contradicts" in r.message for r in caplog.records), caplog.text


# -- index lifecycle --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_the_resolved_width_is_cached_per_index():
    mapping = respx.get(_mapping_url()).mock(return_value=Response(404))
    embed = respx.post(_embed_url()).mock(return_value=_embedding(384))
    respx.put(_index_url()).mock(return_value=Response(200, json={"acknowledged": True}))

    assert await ci.ensure_index(CURRICULUM_ID) == 384
    assert await ci.ensure_index(CURRICULUM_ID) == 384  # served from the cache
    assert mapping.call_count == 1 and embed.call_count == 1
    assert ci._dim_cache == {INDEX: 384}


@pytest.mark.asyncio
@respx.mock
async def test_a_lost_create_race_adopts_the_width_that_won():
    """The PUT fails because another worker created the index first: adopt its width."""
    # mapping reads: the pre-create check (404), create_index's own re-check, then the adopt
    respx.get(_mapping_url()).mock(side_effect=[Response(404), _mapping_response(384), _mapping_response(384)])
    respx.post(_embed_url()).mock(return_value=_embedding(384))
    respx.put(_index_url()).mock(return_value=Response(409, json={"error": "version_conflict"}))

    assert await ci.ensure_index(CURRICULUM_ID) == 384
    assert ci._dim_cache[INDEX] == 384


@pytest.mark.asyncio
@respx.mock
async def test_no_chunks_indexes_nothing():
    respx.get(_mapping_url()).mock(return_value=Response(404))
    respx.post(_embed_url()).mock(return_value=Response(503))
    respx.put(_index_url()).mock(return_value=Response(200, json={"acknowledged": True}))
    bulk = respx.post(f"{_os()}/_bulk").mock(return_value=Response(200, json={"items": []}))

    assert await ci.index_chunks(CURRICULUM_ID, "doc-1", "notes.md", []) == (0, "")
    assert bulk.call_count == 0


# -- retrieval --------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_query_that_embeds_to_the_index_width_searches_knn():
    respx.post(_embed_url()).mock(return_value=_embedding(384))
    respx.get(_mapping_url()).mock(return_value=_mapping_response(384))
    search = respx.post(f"{_os()}/{INDEX}/_search").mock(
        return_value=Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {"_score": 1.5, "_source": {"text": "first chunk", "filename": "notes.md", "chunk_ordinal": 0}},
                        {"_source": {"text": "second chunk", "chunk_ordinal": 3}},
                    ]
                }
            },
        )
    )

    hits = await ci.rag_search(CURRICULUM_ID, "how do vlans work", k=2)

    assert [h["text"] for h in hits] == ["first chunk", "second chunk"]
    assert hits[0]["score"] == 1.5 and hits[0]["filename"] == "notes.md"
    assert hits[1]["filename"] == "" and hits[1]["chunk_ordinal"] == 3
    sent = json.loads(search.calls[0].request.content)
    assert sent == {
        "size": 2,
        "query": {"knn": {"embedding": {"vector": [0.01] * 384, "k": 2}}},
        "_source": ["text", "filename", "chunk_ordinal"],
    }


@pytest.mark.asyncio
@respx.mock
async def test_a_query_that_cannot_embed_falls_back_to_bm25():
    respx.post(_embed_url()).mock(return_value=Response(503))
    respx.get(_mapping_url()).mock(return_value=_mapping_response(384))
    search = respx.post(f"{_os()}/{INDEX}/_search").mock(return_value=Response(200, json={"hits": {"hits": []}}))

    assert await ci.rag_search(CURRICULUM_ID, "gateway") == []

    sent = json.loads(search.calls[0].request.content)
    assert sent["query"] == {"match": {"text": {"query": "gateway"}}}


@pytest.mark.asyncio
@respx.mock
async def test_a_query_vector_of_the_wrong_width_is_dropped_loudly(caplog):
    respx.post(_embed_url()).mock(return_value=_embedding(384))
    respx.get(_mapping_url()).mock(return_value=_mapping_response(1024))
    search = respx.post(f"{_os()}/{INDEX}/_search").mock(return_value=Response(200, json={"hits": {"hits": []}}))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await ci.rag_search(CURRICULUM_ID, "gateway") == []

    assert any("mismatch" in r.message.lower() for r in caplog.records), caplog.text
    sent = json.loads(search.calls[0].request.content)
    assert "knn" not in sent["query"]


@pytest.mark.asyncio
@respx.mock
async def test_deleting_an_index_also_drops_its_cached_width_and_warnings():
    respx.delete(f"{_os()}/{INDEX}").mock(return_value=Response(200, json={"acknowledged": True}))
    ci._dim_cache[INDEX] = 384
    ci._warned_mismatch.add((INDEX, 384, 1024))
    other = ("curriculum-other", 1, 2)
    ci._warned_mismatch.add(other)

    await ci.delete_index(CURRICULUM_ID)

    assert INDEX not in ci._dim_cache
    assert other in ci._warned_mismatch
    assert (INDEX, 384, 1024) not in ci._warned_mismatch
