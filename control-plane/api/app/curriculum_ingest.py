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

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:8100")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://opensearch:9200").rstrip("/")

CHUNK_CHARS = 3200      # ~800 tokens
CHUNK_OVERLAP = 400
EMBED_DIM = 1024        # bge-m3 dimensionality


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
    except Exception as exc:
        logger.debug("Embedding unavailable (%s); indexing text-only", exc)
        return None, ""


# ── OpenSearch indexing / retrieval ──────────────────────────────────────


async def ensure_index(curriculum_id: uuid.UUID | str) -> None:
    index = index_name(curriculum_id)
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
                    "dimension": EMBED_DIM,
                    "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
                },
            }
        },
    }
    async with httpx.AsyncClient(timeout=15) as client:
        exists = await client.head(f"{OPENSEARCH_URL}/{index}")
        if exists.status_code == 404:
            resp = await client.put(f"{OPENSEARCH_URL}/{index}", json=mapping)
            resp.raise_for_status()


async def index_chunks(
    curriculum_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    filename: str,
    chunks: list[str],
) -> tuple[int, str]:
    """Index chunks (embedding when possible). Returns (indexed_count, embed_model)."""
    await ensure_index(curriculum_id)
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
        if vector and len(vector) == EMBED_DIM:
            doc["embedding"] = vector
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
    """Top-k chunks: kNN when the query can be embedded, BM25 otherwise."""
    index = index_name(curriculum_id)
    vector, _ = await embed_text(query)

    if vector and len(vector) == EMBED_DIM:
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
    async with httpx.AsyncClient(timeout=15) as client:
        await client.delete(f"{OPENSEARCH_URL}/{index_name(curriculum_id)}")
