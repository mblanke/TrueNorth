"""TrueNorth Range — Curriculum Forge router.

Upload courseware (PDF/DOCX/PPTX/Markdown/text) or register URLs, ingest it
into a per-curriculum RAG index (extract → chunk → embed → OpenSearch), and
query it. Generation endpoints (courses, quizzes, ranges) build on the
`rag_search` retrieval exposed here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import curriculum_ingest, object_store
from ..auth import CurrentUser, get_current_user
from ..db import SessionLocal, get_db
from ..models import (
    Course,
    CourseModule,
    Curriculum,
    CurriculumDocStatus,
    CurriculumDocument,
    CurriculumStatus,
    ModuleContentType,
    Quiz,
)

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:8100")

logger = logging.getLogger("truenorth.api.curriculum")

router = APIRouter(prefix="/curricula", tags=["curriculum"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
ALLOWED_SUFFIXES = (".pdf", ".docx", ".pptx", ".md", ".markdown", ".txt", ".rst", ".html", ".htm")


# ── Schemas ──────────────────────────────────────────────────────────────


class CurriculumIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class CurriculumDocOut(BaseModel):
    id: uuid.UUID
    filename: str
    source_url: str
    mime_type: str
    status: str
    char_count: int
    chunk_count: int
    error: str

    class Config:
        from_attributes = True


class CurriculumOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    status: str
    chunk_count: int
    embedding_model: str
    created_at: datetime | None = None
    documents: list[CurriculumDocOut] = []

    class Config:
        from_attributes = True


class UrlIngestIn(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=25)


class RagSearchIn(BaseModel):
    query: str = Field(..., min_length=2)
    k: int = Field(default=8, ge=1, le=25)


class RagChunkOut(BaseModel):
    text: str
    filename: str
    chunk_ordinal: int
    score: float


# ── CRUD ─────────────────────────────────────────────────────────────────


@router.post("", response_model=CurriculumOut, status_code=status.HTTP_201_CREATED)
def create_curriculum(
    body: CurriculumIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    curriculum = Curriculum(name=body.name, description=body.description, tenant_id=user.tenant_id)
    db.add(curriculum)
    db.commit()
    db.refresh(curriculum)
    return curriculum


@router.get("", response_model=list[CurriculumOut])
def list_curricula(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return (
        db.query(Curriculum)
        .filter(Curriculum.tenant_id == user.tenant_id, Curriculum.deleted_at.is_(None))
        .order_by(Curriculum.created_at.desc())
        .all()
    )


@router.get("/{curriculum_id}", response_model=CurriculumOut)
def get_curriculum(
    curriculum_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return _get_owned(curriculum_id, db, user)


@router.delete("/{curriculum_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_curriculum(
    curriculum_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    curriculum = _get_owned(curriculum_id, db, user)
    curriculum.deleted_at = datetime.now(UTC)
    db.commit()
    try:
        await curriculum_ingest.delete_index(curriculum_id)
    except Exception as exc:  # index removal is best-effort
        logger.warning("Could not delete RAG index for %s: %s", curriculum_id, exc)


# ── Document upload / URL registration ───────────────────────────────────


@router.post(
    "/{curriculum_id}/documents",
    response_model=CurriculumOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_documents(
    curriculum_id: uuid.UUID,
    files: list[UploadFile],
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Upload one or more courseware files; ingestion runs in the background."""
    curriculum = _get_owned(curriculum_id, db, user)

    doc_ids: list[uuid.UUID] = []
    for file in files:
        filename = file.filename or "upload"
        if not filename.lower().endswith(ALLOWED_SUFFIXES):
            raise HTTPException(415, f"Unsupported file type: {filename}")
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{filename} exceeds the 50 MB limit")
        if not data:
            raise HTTPException(422, f"{filename} is empty")

        doc = CurriculumDocument(
            curriculum_id=curriculum.id,
            filename=filename,
            mime_type=file.content_type or "",
            tenant_id=user.tenant_id,
        )
        db.add(doc)
        db.flush()
        doc.minio_key = f"{curriculum.id}/{doc.id}/{filename}"
        object_store.put_object(doc.minio_key, data, doc.mime_type)
        doc_ids.append(doc.id)

    curriculum.status = CurriculumStatus.ingesting
    db.commit()
    db.refresh(curriculum)

    background.add_task(_ingest_documents, curriculum.id, doc_ids)
    return curriculum


@router.post(
    "/{curriculum_id}/urls",
    response_model=CurriculumOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def register_urls(
    curriculum_id: uuid.UUID,
    body: UrlIngestIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Register web pages as curriculum sources; fetched during ingestion."""
    curriculum = _get_owned(curriculum_id, db, user)

    doc_ids: list[uuid.UUID] = []
    for url in body.urls:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, f"Not an http(s) URL: {url}")
        doc = CurriculumDocument(
            curriculum_id=curriculum.id,
            filename=url[:500],
            source_url=url,
            mime_type="text/html",
            tenant_id=user.tenant_id,
        )
        db.add(doc)
        doc_ids.append(doc.id)
    curriculum.status = CurriculumStatus.ingesting
    db.commit()
    db.refresh(curriculum)

    background.add_task(_ingest_documents, curriculum.id, doc_ids)
    return curriculum


@router.post("/{curriculum_id}/ingest", response_model=CurriculumOut, status_code=status.HTTP_202_ACCEPTED)
def reingest(
    curriculum_id: uuid.UUID,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Re-run ingestion for any documents that are pending or errored."""
    curriculum = _get_owned(curriculum_id, db, user)
    pending = [
        d.id
        for d in curriculum.documents
        if d.status in (CurriculumDocStatus.pending, CurriculumDocStatus.error)
    ]
    if not pending:
        raise HTTPException(409, "No pending or errored documents to ingest.")
    curriculum.status = CurriculumStatus.ingesting
    db.commit()
    db.refresh(curriculum)
    background.add_task(_ingest_documents, curriculum.id, pending)
    return curriculum


# ── RAG search ───────────────────────────────────────────────────────────


@router.post("/{curriculum_id}/search", response_model=list[RagChunkOut])
async def search_curriculum(
    curriculum_id: uuid.UUID,
    body: RagSearchIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    _get_owned(curriculum_id, db, user)
    try:
        return await curriculum_ingest.rag_search(curriculum_id, body.query, body.k)
    except Exception as exc:
        raise HTTPException(502, f"RAG search failed: {exc}") from exc


# ── AI course generation ─────────────────────────────────────────────────


class CourseGenerateIn(BaseModel):
    difficulty: str = Field(default="intermediate", pattern=r"^(beginner|intermediate|advanced|expert)$")
    module_count: int = Field(default=6, ge=2, le=16)
    focus: str = Field(default="", max_length=2000)


class CourseGenerateOut(BaseModel):
    course_id: uuid.UUID
    name: str
    module_count: int
    quiz_module_count: int
    model_used: str


@router.post("/{curriculum_id}/generate-course", response_model=CourseGenerateOut)
async def generate_course(
    curriculum_id: uuid.UUID,
    body: CourseGenerateIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Draft a course (with modules and quiz placeholders) from the ingested curriculum."""
    curriculum = _get_owned(curriculum_id, db, user)
    if curriculum.status != CurriculumStatus.ready:
        raise HTTPException(409, "Curriculum is not ready — ingest documents first.")

    # Retrieve a broad context sample: the focus hint plus general coverage.
    queries = [body.focus] if body.focus else []
    queries += [curriculum.name, "learning objectives key concepts", "summary overview"]
    chunks: list[str] = []
    seen: set[str] = set()
    for q in queries:
        for hit in await curriculum_ingest.rag_search(curriculum_id, q, k=10):
            key = hit["text"][:120]
            if key not in seen:
                seen.add(key)
                chunks.append(hit["text"])
    if not chunks:
        raise HTTPException(409, "No indexed curriculum content found.")

    payload = {
        "curriculum_name": curriculum.name,
        "context_chunks": chunks[:30],
        "difficulty": body.difficulty,
        "module_count": body.module_count,
        "focus": body.focus,
    }
    async with httpx.AsyncClient(timeout=330) as client:
        try:
            resp = await client.post(f"{AI_ORCHESTRATOR_URL}/ai/course-generate", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, "AI orchestrator failed to generate the course.") from e
        except httpx.ConnectError as e:
            raise HTTPException(503, "AI orchestrator is unavailable.") from e
        data = resp.json()

    draft = _parse_ai_json(data.get("output", ""))
    if not isinstance(draft, dict) or not draft.get("modules"):
        raise HTTPException(502, "AI returned an unusable course draft.")

    course = Course(
        name=str(draft.get("name") or f"{curriculum.name} (AI draft)")[:255],
        description=str(draft.get("description") or ""),
        difficulty=str(draft.get("difficulty") or body.difficulty),
        duration_hours=int(draft.get("duration_hours") or 0),
        tags=json.dumps(draft.get("tags") or []),
        nice_work_roles=json.dumps(draft.get("nice_work_roles") or []),
        course_meta=json.dumps(
            {
                "source_curriculum_id": str(curriculum.id),
                "generated_by_model": data.get("model_used", ""),
            }
        ),
        is_published=False,
        tenant_id=user.tenant_id,
    )
    db.add(course)
    db.flush()

    quiz_count = 0
    for i, mod in enumerate(draft["modules"]):
        content_type_raw = str(mod.get("content_type") or "reading")
        try:
            content_type = ModuleContentType(content_type_raw)
        except ValueError:
            content_type = ModuleContentType.reading
        module = CourseModule(
            course_id=course.id,
            ordinal=int(mod.get("ordinal", i)),
            title=str(mod.get("title") or f"Module {i + 1}")[:255],
            description=str(mod.get("description") or ""),
            content_type=content_type,
            content_ref=json.dumps(
                {
                    "lesson_markdown": mod.get("lesson_markdown") or "",
                    "learning_objectives": mod.get("learning_objectives") or [],
                    "competency_codes": mod.get("competency_codes") or [],
                }
            ),
            duration_minutes=int(mod.get("duration_minutes") or 0),
        )
        db.add(module)
        db.flush()
        if content_type == ModuleContentType.quiz:
            quiz_count += 1
            db.add(
                Quiz(
                    title=f"{module.title} — Quiz",
                    module_id=module.id,
                    curriculum_id=curriculum.id,
                    is_published=False,
                    tenant_id=user.tenant_id,
                )
            )

    db.commit()
    return CourseGenerateOut(
        course_id=course.id,
        name=course.name,
        module_count=len(draft["modules"]),
        quiz_module_count=quiz_count,
        model_used=data.get("model_used", ""),
    )


def _parse_ai_json(raw: str):
    """Parse model output into JSON, tolerating markdown fences and prose wrap."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost JSON object/array in the text.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
    raise HTTPException(502, "AI returned invalid JSON.")


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_owned(curriculum_id: uuid.UUID, db: Session, user: CurrentUser) -> Curriculum:
    curriculum = (
        db.query(Curriculum)
        .filter(
            Curriculum.id == curriculum_id,
            Curriculum.tenant_id == user.tenant_id,
            Curriculum.deleted_at.is_(None),
        )
        .first()
    )
    if not curriculum:
        raise HTTPException(404, "Curriculum not found")
    return curriculum


async def _ingest_documents(curriculum_id: uuid.UUID, doc_ids: list[uuid.UUID]) -> None:
    """Background pipeline: extract → chunk → embed → index, per document."""
    db = SessionLocal()
    try:
        embed_model = ""
        for doc_id in doc_ids:
            doc = db.get(CurriculumDocument, doc_id)
            if not doc:
                continue
            try:
                doc.status = CurriculumDocStatus.extracting
                db.commit()

                if doc.source_url:
                    title, text = await curriculum_ingest.fetch_url_text(doc.source_url)
                    if title and doc.filename == doc.source_url[:500]:
                        doc.filename = title[:500]
                else:
                    data = object_store.get_object(doc.minio_key)
                    text = curriculum_ingest.extract_text(doc.filename, data, doc.mime_type)

                doc.char_count = len(text)
                chunks = curriculum_ingest.chunk_text(text)
                if not chunks:
                    raise ValueError("No extractable text found")

                doc.status = CurriculumDocStatus.embedding
                db.commit()

                indexed, model = await curriculum_ingest.index_chunks(
                    curriculum_id, doc.id, doc.filename, chunks
                )
                if model:
                    embed_model = model
                doc.chunk_count = indexed
                doc.status = CurriculumDocStatus.indexed
                doc.error = ""
                db.commit()
                logger.info("Ingested %s: %d chunks", doc.filename, indexed)
            except Exception as exc:
                logger.error("Ingestion failed for %s: %s", doc_id, exc)
                doc.status = CurriculumDocStatus.error
                doc.error = str(exc)[:2000]
                db.commit()

        curriculum = db.get(Curriculum, curriculum_id)
        if curriculum:
            docs = curriculum.documents
            curriculum.chunk_count = sum(d.chunk_count for d in docs)
            if embed_model:
                curriculum.embedding_model = embed_model
            if any(d.status == CurriculumDocStatus.indexed for d in docs):
                curriculum.status = CurriculumStatus.ready
            elif all(d.status == CurriculumDocStatus.error for d in docs) and docs:
                curriculum.status = CurriculumStatus.error
            db.commit()
    finally:
        db.close()
