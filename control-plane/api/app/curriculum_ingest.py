"""TrueNorth Range — Curriculum Forge ingestion pipeline.

Extract text from uploaded courseware (PDF / DOCX / PPTX / Markdown / text /
URLs), chunk it, embed each chunk via the AI orchestrator, and index into a
per-curriculum OpenSearch index for RAG.

Embedding is best-effort: when no embedding-capable node is reachable (e.g.
dev without the Ollama fleet), chunks are still indexed for BM25 full-text
retrieval and the curriculum remains fully usable — `rag_search` simply
falls back to keyword matching.
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import httpx

logger = logging.getLogger("truenorth.api.curriculum")

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://opensearch:9200").rstrip("/")

CHUNK_CHARS = 3200      # ~800 tokens
CHUNK_OVERLAP = 400

# Embedding width is a property of the served model, not a preference, so it is
# resolved rather than declared. Per index, in order:
#
#   1. the live index mapping  — authoritative. OpenSearch fixes knn_vector width
#                                at index creation and accepts nothing else after.
#   2. a one-shot model probe  — ground truth for anything not yet indexed.
#   3. EMBED_DIM               — operator pin, honoured only when the model
#                                cannot be reached.
#   4. EMBED_DIM_FALLBACK      — last resort so ingest still works offline.
#
# Any disagreement between two of these is logged at WARNING. It used to be
# silent: a wrong width dropped every vector, retrieval fell back to BM25, and
# ingest still reported success — so semantic search could be off for weeks with
# nothing in the logs to say so.
EMBED_DIM_FALLBACK = 1024
_EMBED_DIM_PIN = int(os.environ["EMBED_DIM"]) if os.getenv("EMBED_DIM") else None

_dim_cache: dict[str, int] = {}          # index name -> resolved vector width
_probed_dim: int | None = None           # width reported by the served model
_probe_done = False                      # probe is attempted at most once
_warned_mismatch: set[tuple[str, int, int]] = set()   # (index, got, want)


def index_name(curriculum_id: uuid.UUID | str) -> str:
    return f"curriculum-{curriculum_id}"


# ── Text extraction ──────────────────────────────────────────────────────


def extract_text(filename: str, data: bytes, mime_type: str = "") -> str:
    """Extract plain text from a courseware file. Raises ValueError on unsupported types."""
    name = filename.lower()
    if name.endswith(".pdf") or "pdf" in mime_type:
        return _extract_pdf(data)
    if name.endswith(".docx") or "wordprocessingml" in mime_type:
        return _extract_docx(data)
    if name.endswith(".pptx") or "presentationml" in mime_type:
        return _extract_pptx(data)
    if name.endswith((".md", ".markdown", ".txt", ".rst")) or mime_type.startswith("text/"):
        return data.decode("utf-8", errors="replace")
    if name.endswith((".html", ".htm")) or "html" in mime_type:
        return _html_to_text(data.decode("utf-8", errors="replace"))
    raise ValueError(f"Unsupported file type: {filename} ({mime_type or 'unknown mime'})")


def _extract_pdf(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def _extract_docx(data: bytes) -> str:
    import io

    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    import io

    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        slide_text = [f"[Slide {i}]"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        slide_text.append(text)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                slide_text.append(f"(Notes: {notes})")
        parts.append("\n".join(slide_text))
    return "\n\n".join(parts)


def _html_to_text(html: str) -> str:
    import html2text

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0
    return converter.handle(html)


async def fetch_url_text(url: str) -> tuple[str, str]:
    """Fetch a web page and return (title-ish name, extracted text)."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type:
            return url.rsplit("/", 1)[-1] or url, _extract_pdf(resp.content)
        text = _html_to_text(resp.text)
        match = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
        title = match.group(1).strip()[:200] if match else url
        return title, text


# ── Chunking ─────────────────────────────────────────────────────────────


def chunk_text(text: str) -> list[str]:
    """Split on paragraph boundaries into ~CHUNK_CHARS windows with overlap."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + CHUNK_CHARS
        if end < len(text):
            # Prefer to break on a paragraph, then sentence, then word.
            window = text[start:end]
            for sep in ("\n\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > CHUNK_CHARS // 2:
                    end = start + cut + len(sep)
                    break
        chunks.append(text[start:end].strip())
        start = max(end - CHUNK_OVERLAP, start + 1)
        if end >= len(text):
            break
    return [c for c in chunks if c]


# ── Embedding (best-effort) ──────────────────────────────────────────────


async def embed_text(text: str) -> tuple[list[float] | None, str]:
    """Returns (vector, model) or (None, '') when no embedding node is available."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{AI_ORCHESTRATOR_URL}/ai/embedding",
                json={"text": text[:8000]},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding"), data.get("model_used", "")
    except httpx.HTTPStatusError as exc:
        # A reachable endpoint that refuses the call is a configuration fault,
        # not the "no embedding node in dev" case below. 401 here almost always
        # means OPENAI_API_KEY is still the placeholder rather than a live key.
        logger.warning(
            "Embedding endpoint returned %s; indexing text-only and retrieval will use BM25",
            exc.response.status_code,
        )
        return None, ""
    except Exception as exc:
        logger.debug("Embedding unavailable (%s); indexing text-only", exc)
        return None, ""


async def _probe_embedding_dim() -> int | None:
    """Ask the served model how wide its vectors are. Probed at most once."""
    global _probed_dim, _probe_done
    if _probe_done:
        return _probed_dim
    _probe_done = True

    vector, model = await embed_text("embedding dimension probe")
    if not vector:
        logger.warning(
            "Could not probe the embedding model; assuming %d dimensions. "
            "Semantic search stays unavailable until embeddings are reachable.",
            _EMBED_DIM_PIN or EMBED_DIM_FALLBACK,
        )
        return None

    _probed_dim = len(vector)
    logger.info("Embedding model %s returns %d-dimensional vectors", model or "unknown", _probed_dim)
    if _EMBED_DIM_PIN is not None and _probed_dim != _EMBED_DIM_PIN:
        logger.warning(
            "EMBED_DIM=%d contradicts the served model (%s returns %d). Using %d — "
            "correct EMBED_DIM, or new indexes will be built at a width the model cannot fill.",
            _EMBED_DIM_PIN, model or "unknown", _probed_dim, _probed_dim,
        )
    return _probed_dim


async def _existing_index_dim(client: httpx.AsyncClient, index: str) -> int | None:
    """Vector width already fixed in a live index, or None if it does not exist."""
    try:
        resp = await client.get(f"{OPENSEARCH_URL}/{index}/_mapping")
        if resp.status_code != 200:
            return None
        props = resp.json()[index]["mappings"]["properties"]
        return int(props["embedding"]["dimension"])
    except Exception as exc:
        logger.debug("Could not read the mapping for %s (%s)", index, exc)
        return None


def _warn_mismatch(index: str, got: int, want: int) -> None:
    """Warn once per (index, got, want) that vectors are being discarded."""
    key = (index, got, want)
    if key in _warned_mismatch:
        return
    _warned_mismatch.add(key)
    logger.warning(
        "Embedding width mismatch on %s: the model returned %d, the index expects %d. "
        "Chunks are indexed text-only and retrieval has fallen back to BM25. Rebuild the "
        "index (delete it and re-ingest) to restore semantic search at %d dimensions.",
        index, got, want, got,
    )


# ── OpenSearch indexing / retrieval ──────────────────────────────────────


async def ensure_index(curriculum_id: uuid.UUID | str) -> int:
    """Create the per-curriculum index if absent. Returns its vector width."""
    index = index_name(curriculum_id)
    cached = _dim_cache.get(index)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=15) as client:
        existing = await _existing_index_dim(client, index)
        if existing is not None:
            # The index wins: its width cannot be altered in place. Say so if the
            # model has since changed, because every vector will now be dropped.
            probed = await _probe_embedding_dim()
            if probed is not None and probed != existing:
                _warn_mismatch(index, probed, existing)
            _dim_cache[index] = existing
            return existing

        dim = await _probe_embedding_dim() or _EMBED_DIM_PIN or EMBED_DIM_FALLBACK
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "curriculum_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "filename": {"type": "keyword"},
                    "chunk_ordinal": {"type": "integer"},
                    "text": {"type": "text"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": dim,
                        "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
                    },
                }
            },
        }
        resp = await client.put(f"{OPENSEARCH_URL}/{index}", json=mapping)
        if resp.status_code >= 300:
            # Lost a race with a concurrent create — adopt whatever won.
            existing = await _existing_index_dim(client, index)
            if existing is None:
                resp.raise_for_status()
            _dim_cache[index] = existing
            return existing

    logger.info("Created index %s for %d-dimensional embeddings", index, dim)
    _dim_cache[index] = dim
    return dim


async def index_chunks(
    curriculum_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    filename: str,
    chunks: list[str],
) -> tuple[int, str]:
    """Index chunks (embedding when possible). Returns (indexed_count, embed_model)."""
    dim = await ensure_index(curriculum_id)
    index = index_name(curriculum_id)
    embed_model = ""

    bulk_lines: list[str] = []
    import json as _json

    for ordinal, chunk in enumerate(chunks):
        vector, model = await embed_text(chunk)
        if model:
            embed_model = model
        doc = {
            "curriculum_id": str(curriculum_id),
            "document_id": str(document_id),
            "filename": filename,
            "chunk_ordinal": ordinal,
            "text": chunk,
        }
        if vector and len(vector) == dim:
            doc["embedding"] = vector
        elif vector:
            _warn_mismatch(index, len(vector), dim)
        bulk_lines.append(_json.dumps({"index": {"_index": index}}))
        bulk_lines.append(_json.dumps(doc))

    if not bulk_lines:
        return 0, embed_model

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OPENSEARCH_URL}/_bulk",
            content="\n".join(bulk_lines) + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        resp.raise_for_status()
        result = resp.json()
        indexed = sum(
            1 for item in result.get("items", [])
            if item.get("index", {}).get("status", 500) < 300
        )
    return indexed, embed_model


async def rag_search(curriculum_id: uuid.UUID | str, query: str, k: int = 8) -> list[dict]:
    """Top-k chunks: kNN when the query embeds to the index's width, BM25 otherwise."""
    index = index_name(curriculum_id)
    vector, _ = await embed_text(query)

    dim = _dim_cache.get(index)
    if dim is None:
        # Read, never create: a search must not conjure an empty index.
        async with httpx.AsyncClient(timeout=15) as client:
            dim = await _existing_index_dim(client, index)
        if dim is not None:
            _dim_cache[index] = dim
    if vector and dim is not None and len(vector) != dim:
        _warn_mismatch(index, len(vector), dim)

    if vector and dim is not None and len(vector) == dim:
        body: dict = {
            "size": k,
            "query": {"knn": {"embedding": {"vector": vector, "k": k}}},
            "_source": ["text", "filename", "chunk_ordinal"],
        }
    else:
        body = {
            "size": k,
            "query": {"match": {"text": {"query": query}}},
            "_source": ["text", "filename", "chunk_ordinal"],
        }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{OPENSEARCH_URL}/{index}/_search", json=body)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    return [
        {
            "text": h["_source"]["text"],
            "filename": h["_source"].get("filename", ""),
            "chunk_ordinal": h["_source"].get("chunk_ordinal", 0),
            "score": h.get("_score", 0),
        }
        for h in hits
    ]


async def delete_index(curriculum_id: uuid.UUID | str) -> None:
    index = index_name(curriculum_id)
    # Drop the cached width and its warnings so a rebuild re-resolves cleanly.
    _dim_cache.pop(index, None)
    _warned_mismatch.difference_update([k for k in _warned_mismatch if k[0] == index])
    async with httpx.AsyncClient(timeout=15) as client:
        await client.delete(f"{OPENSEARCH_URL}/{index}")
