"""TrueNorth Range — Quiz engine router (Curriculum Forge).

Moodle-style assessments: AI generation from ingested curriculum, instructor
review, a student attempt flow with server-side grading, xAPI emission,
module-progress updates, competency auto-assessment, and export to Moodle
GIFT / XML question banks.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import uuid
from datetime import UTC, datetime
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from .. import curriculum_ingest
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    CompetencyAutoAssessment,
    CourseModule,
    Curriculum,
    CurriculumStatus,
    Enrollment,
    ModuleProgress,
    ModuleProgressStatus,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionType,
)
from ..xapi import emit_lifecycle

logger = logging.getLogger("truenorth.api.quizzes")

router = APIRouter(prefix="/quizzes", tags=["quizzes"])

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:8100")


# ── Schemas ──────────────────────────────────────────────────────────────


class QuestionOut(BaseModel):
    """Question as shown to a student — no correct answers, no explanation."""

    id: uuid.UUID
    ordinal: int
    question_type: str
    stem: str
    options: list[str]
    points: int


class QuestionFullOut(QuestionOut):
    """Instructor view — includes the answer key."""

    correct: list[int]
    explanation: str
    competency_code: str
    difficulty: str


class QuestionIn(BaseModel):
    question_type: str = Field(default="mcq", pattern=r"^(mcq|multi|truefalse|scenario)$")
    stem: str = Field(..., min_length=3)
    options: list[str] = Field(..., min_length=2, max_length=8)
    correct: list[int] = Field(..., min_length=1)
    explanation: str = ""
    competency_code: str = ""
    difficulty: str = "intermediate"
    points: int = Field(default=10, ge=1, le=100)


class QuizOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    module_id: uuid.UUID | None
    curriculum_id: uuid.UUID | None
    pass_pct: int
    time_limit_minutes: int
    shuffle_questions: bool
    max_attempts: int
    is_published: bool
    generated_by_model: str
    question_count: int = 0

    class Config:
        from_attributes = True


class QuizUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    pass_pct: int | None = Field(default=None, ge=1, le=100)
    time_limit_minutes: int | None = Field(default=None, ge=0)
    shuffle_questions: bool | None = None
    max_attempts: int | None = Field(default=None, ge=0)
    is_published: bool | None = None


class QuizGenerateIn(BaseModel):
    curriculum_id: uuid.UUID
    topic: str = Field(..., min_length=2, max_length=500)
    question_count: int = Field(default=10, ge=3, le=30)
    difficulty: str = Field(default="intermediate", pattern=r"^(beginner|intermediate|advanced|expert)$")
    competency_codes: list[str] = Field(default_factory=list, max_length=20)
    quiz_id: uuid.UUID | None = None     # fill an existing placeholder quiz
    module_id: uuid.UUID | None = None   # or bind a new quiz to a module


class AttemptStartOut(BaseModel):
    attempt_id: uuid.UUID
    quiz_id: uuid.UUID
    title: str
    time_limit_minutes: int
    pass_pct: int
    questions: list[QuestionOut]


class AttemptSubmitIn(BaseModel):
    answers: dict[str, list[int]] = Field(..., description="question_id -> selected option indices")


class QuestionResultOut(BaseModel):
    question_id: uuid.UUID
    correct: bool
    selected: list[int]
    correct_options: list[int]
    explanation: str
    points_earned: int
    points_possible: int


class AttemptResultOut(BaseModel):
    attempt_id: uuid.UUID
    score: int
    max_score: int
    pct: float
    passed: bool
    results: list[QuestionResultOut]


# ── CRUD ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[QuizOut])
def list_quizzes(
    curriculum_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    q = (
        db.query(Quiz)
        .options(joinedload(Quiz.questions))
        .filter(Quiz.tenant_id == user.tenant_id, Quiz.deleted_at.is_(None))
    )
    if curriculum_id:
        q = q.filter(Quiz.curriculum_id == curriculum_id)
    quizzes = q.order_by(Quiz.created_at.desc()).all()
    return [_quiz_out(quiz) for quiz in quizzes]


@router.get("/{quiz_id}", response_model=QuizOut)
def get_quiz(
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return _quiz_out(_get_owned(quiz_id, db, user))


@router.get("/{quiz_id}/questions", response_model=list[QuestionFullOut])
def get_quiz_questions(
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Instructor view with answer key (any authenticated user with tenant access in v1)."""
    quiz = _get_owned(quiz_id, db, user)
    return [_question_full(q) for q in quiz.questions]


@router.patch("/{quiz_id}", response_model=QuizOut)
def update_quiz(
    quiz_id: uuid.UUID,
    body: QuizUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    quiz = _get_owned(quiz_id, db, user)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(quiz, field, value)
    db.commit()
    db.refresh(quiz)
    return _quiz_out(quiz)


@router.put("/{quiz_id}/questions", response_model=list[QuestionFullOut])
def replace_questions(
    quiz_id: uuid.UUID,
    body: list[QuestionIn],
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Replace the full question set (instructor review/edit save)."""
    quiz = _get_owned(quiz_id, db, user)
    for old in list(quiz.questions):
        db.delete(old)
    db.flush()
    for i, q in enumerate(body):
        _validate_question(q)
        db.add(
            QuizQuestion(
                quiz_id=quiz.id,
                ordinal=i,
                question_type=QuizQuestionType(q.question_type),
                stem=q.stem,
                options=json.dumps(q.options),
                correct=json.dumps(sorted(set(q.correct))),
                explanation=q.explanation,
                competency_code=q.competency_code,
                difficulty=q.difficulty,
                points=q.points,
            )
        )
    db.commit()
    db.refresh(quiz)
    return [_question_full(q) for q in quiz.questions]


@router.delete("/{quiz_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quiz(
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    quiz = _get_owned(quiz_id, db, user)
    quiz.deleted_at = datetime.now(UTC)
    db.commit()


# ── AI generation ────────────────────────────────────────────────────────


@router.post("/generate", response_model=QuizOut, status_code=status.HTTP_201_CREATED)
async def generate_quiz(
    body: QuizGenerateIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Generate draft questions from the curriculum RAG index."""
    curriculum = (
        db.query(Curriculum)
        .filter(
            Curriculum.id == body.curriculum_id,
            Curriculum.tenant_id == user.tenant_id,
            Curriculum.deleted_at.is_(None),
        )
        .first()
    )
    if not curriculum:
        raise HTTPException(404, "Curriculum not found")
    if curriculum.status != CurriculumStatus.ready:
        raise HTTPException(409, "Curriculum is not ready — ingest documents first.")

    chunks = [h["text"] for h in await curriculum_ingest.rag_search(body.curriculum_id, body.topic, k=12)]
    if not chunks:
        raise HTTPException(409, "No relevant curriculum content found for this topic.")

    payload = {
        "topic": body.topic,
        "context_chunks": chunks,
        "question_count": body.question_count,
        "difficulty": body.difficulty,
        "competency_codes": body.competency_codes,
    }
    async with httpx.AsyncClient(timeout=330) as client:
        try:
            resp = await client.post(f"{AI_ORCHESTRATOR_URL}/ai/quiz-generate", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, "AI orchestrator failed to generate the quiz.") from e
        except httpx.ConnectError as e:
            raise HTTPException(503, "AI orchestrator is unavailable.") from e
        data = resp.json()

    questions = _parse_questions(data.get("output", ""))
    if not questions:
        raise HTTPException(502, "AI returned no usable questions.")

    if body.quiz_id:
        quiz = _get_owned(body.quiz_id, db, user)
        for old in list(quiz.questions):
            db.delete(old)
        db.flush()
    else:
        quiz = Quiz(
            title=f"{body.topic} — Quiz",
            curriculum_id=curriculum.id,
            module_id=body.module_id,
            tenant_id=user.tenant_id,
        )
        db.add(quiz)
        db.flush()

    quiz.generated_by_model = data.get("model_used", "")
    for i, q in enumerate(questions):
        db.add(
            QuizQuestion(
                quiz_id=quiz.id,
                ordinal=i,
                question_type=q["question_type"],
                stem=q["stem"],
                options=json.dumps(q["options"]),
                correct=json.dumps(q["correct"]),
                explanation=q.get("explanation", ""),
                competency_code=q.get("competency_code", ""),
                difficulty=q.get("difficulty", body.difficulty),
                points=int(q.get("points", 10)),
            )
        )
    db.commit()
    db.refresh(quiz)
    return _quiz_out(quiz)


# ── Attempts ─────────────────────────────────────────────────────────────


@router.post("/{quiz_id}/attempts", response_model=AttemptStartOut, status_code=status.HTTP_201_CREATED)
def start_attempt(
    quiz_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    quiz = _get_owned(quiz_id, db, user)
    if not quiz.is_published:
        raise HTTPException(409, "Quiz is not published.")
    if not quiz.questions:
        raise HTTPException(409, "Quiz has no questions.")

    if quiz.max_attempts:
        prior = (
            db.query(QuizAttempt)
            .filter(
                QuizAttempt.quiz_id == quiz.id,
                QuizAttempt.user_id == uuid.UUID(user.id),
                QuizAttempt.submitted_at.isnot(None),
            )
            .count()
        )
        if prior >= quiz.max_attempts:
            raise HTTPException(409, f"Attempt limit reached ({quiz.max_attempts}).")

    ordered = list(quiz.questions)
    if quiz.shuffle_questions:
        random.shuffle(ordered)

    attempt = QuizAttempt(
        quiz_id=quiz.id,
        user_id=uuid.UUID(user.id),
        question_order=json.dumps([str(q.id) for q in ordered]),
        max_score=sum(q.points for q in quiz.questions),
        tenant_id=user.tenant_id,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return AttemptStartOut(
        attempt_id=attempt.id,
        quiz_id=quiz.id,
        title=quiz.title,
        time_limit_minutes=quiz.time_limit_minutes,
        pass_pct=quiz.pass_pct,
        questions=[_question_student(q) for q in ordered],
    )


@router.post("/attempts/{attempt_id}/submit", response_model=AttemptResultOut)
def submit_attempt(
    attempt_id: uuid.UUID,
    body: AttemptSubmitIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    attempt = (
        db.query(QuizAttempt)
        .filter(QuizAttempt.id == attempt_id, QuizAttempt.user_id == uuid.UUID(user.id))
        .first()
    )
    if not attempt:
        raise HTTPException(404, "Attempt not found")
    if attempt.submitted_at:
        raise HTTPException(409, "Attempt already submitted.")

    quiz = db.get(Quiz, attempt.quiz_id)
    questions = {str(q.id): q for q in quiz.questions}

    score = 0
    results: list[QuestionResultOut] = []
    competency_points: dict[str, list[int]] = {}  # code -> [earned, possible]

    for qid, question in questions.items():
        selected = sorted(set(body.answers.get(qid, [])))
        correct = sorted(json.loads(question.correct))
        is_correct = selected == correct
        earned = question.points if is_correct else 0
        score += earned
        results.append(
            QuestionResultOut(
                question_id=question.id,
                correct=is_correct,
                selected=selected,
                correct_options=correct,
                explanation=question.explanation,
                points_earned=earned,
                points_possible=question.points,
            )
        )
        if question.competency_code:
            bucket = competency_points.setdefault(question.competency_code, [0, 0])
            bucket[0] += earned
            bucket[1] += question.points

        # Per-question xAPI "answered" statement
        emit_lifecycle(
            background_tasks,
            "answered",
            user.email,
            user.display_name,
            "quiz-question",
            str(question.id),
            question.stem[:200],
            result={"success": is_correct, "score": {"raw": earned, "max": question.points}},
            context_extensions={"quiz_id": str(quiz.id), "attempt_id": str(attempt.id)},
        )

    max_score = attempt.max_score or sum(q.points for q in quiz.questions)
    pct = (score / max_score * 100) if max_score else 0.0
    passed = pct >= quiz.pass_pct

    attempt.answers = json.dumps(body.answers)
    attempt.score = score
    attempt.passed = passed
    attempt.submitted_at = datetime.now(UTC)

    # Attempt-level xAPI: scored + passed/failed
    emit_lifecycle(
        background_tasks,
        "scored",
        user.email,
        user.display_name,
        "quiz",
        str(quiz.id),
        quiz.title,
        result={
            "score": {"raw": score, "max": max_score, "scaled": score / max(max_score, 1)},
            "completion": True,
            "success": passed,
        },
    )
    emit_lifecycle(
        background_tasks,
        "passed" if passed else "failed",
        user.email,
        user.display_name,
        "quiz",
        str(quiz.id),
        quiz.title,
        result={"success": passed},
    )

    # Module progress (if quiz is bound to a course module the user is enrolled in)
    if quiz.module_id:
        _update_module_progress(db, quiz, uuid.UUID(user.id), int(pct), passed)

    # Competency auto-assessment from per-question competency mappings
    if competency_points:
        mappings = [
            {
                "competency_code": code,
                "delta": round((earned / possible) if possible else 0, 3),
                "reason": f"Quiz '{quiz.title}': {earned}/{possible} points",
            }
            for code, (earned, possible) in competency_points.items()
        ]
        db.add(
            CompetencyAutoAssessment(
                user_id=uuid.UUID(user.id),
                quiz_attempt_id=attempt.id,
                competency_mappings=json.dumps(mappings),
                raw_score=score,
                max_score=max_score,
            )
        )

    db.commit()

    # Moodle/LTI grade pass-back (no-op unless this quiz was LTI-launched)
    background_tasks.add_task(_push_lti_grade, uuid.UUID(user.id), quiz.id, score, max_score)

    return AttemptResultOut(
        attempt_id=attempt.id,
        score=score,
        max_score=max_score,
        pct=round(pct, 1),
        passed=passed,
        results=results,
    )


async def _push_lti_grade(user_id: uuid.UUID, quiz_id: uuid.UUID, score: int, max_score: int) -> None:
    from .. import lti13
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        await lti13.push_score_for_resource(db, user_id, "quiz", str(quiz_id), score, max_score)
    except Exception as exc:  # advisory — never fail the submission path
        logger.debug("LTI grade push skipped: %s", exc)
    finally:
        db.close()


# ── Export (Moodle) ──────────────────────────────────────────────────────


@router.get("/{quiz_id}/export")
def export_quiz(
    quiz_id: uuid.UUID,
    format: str = Query(default="gift", pattern=r"^(gift|moodlexml)$"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Export the question bank in Moodle GIFT or Moodle XML format."""
    quiz = _get_owned(quiz_id, db, user)
    if not quiz.questions:
        raise HTTPException(409, "Quiz has no questions.")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", quiz.title).strip("-") or "quiz"

    if format == "gift":
        content = _to_gift(quiz)
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.gift.txt"'},
        )
    content = _to_moodle_xml(quiz)
    return Response(
        content,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.xml"'},
    )


def _gift_escape(text: str) -> str:
    for ch in ("~", "=", "#", "{", "}", ":"):
        text = text.replace(ch, "\\" + ch)
    return text


def _to_gift(quiz: Quiz) -> str:
    lines = [f"// {quiz.title} — exported from TrueNorth Range", f"$CATEGORY: {quiz.title}", ""]
    for i, q in enumerate(quiz.questions, 1):
        options: list[str] = json.loads(q.options)
        correct: set[int] = set(json.loads(q.correct))
        stem = _gift_escape(q.stem)
        feedback = f"#### {_gift_escape(q.explanation)}" if q.explanation else ""
        lines.append(f"::Q{i}:: {stem} {{")
        if q.question_type == QuizQuestionType.truefalse:
            lines.append(f"  {'TRUE' if 0 in correct else 'FALSE'}{feedback}")
        elif q.question_type == QuizQuestionType.multi:
            pct = round(100 / max(len(correct), 1), 5)
            for idx, opt in enumerate(options):
                weight = pct if idx in correct else -pct
                lines.append(f"  ~%{weight}%{_gift_escape(opt)}")
            if feedback:
                lines.append(f"  {feedback}")
        else:  # mcq / scenario
            for idx, opt in enumerate(options):
                prefix = "=" if idx in correct else "~"
                lines.append(f"  {prefix}{_gift_escape(opt)}")
            if feedback:
                lines.append(f"  {feedback}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def _to_moodle_xml(quiz: Quiz) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<quiz>",
        "  <question type=\"category\">",
        "    <category><text>$course$/top/" + xml_escape(quiz.title) + "</text></category>",
        "  </question>",
    ]
    for i, q in enumerate(quiz.questions, 1):
        options: list[str] = json.loads(q.options)
        correct: set[int] = set(json.loads(q.correct))
        if q.question_type == QuizQuestionType.truefalse:
            qtype = "truefalse"
        else:
            qtype = "multichoice"
        parts.append(f'  <question type="{qtype}">')
        parts.append(f"    <name><text>Q{i}</text></name>")
        parts.append(
            '    <questiontext format="html"><text><![CDATA['
            + q.stem.replace("]]>", "]]&gt;")
            + "]]></text></questiontext>"
        )
        parts.append(f"    <defaultgrade>{q.points}</defaultgrade>")
        if q.explanation:
            parts.append(
                '    <generalfeedback format="html"><text><![CDATA['
                + q.explanation.replace("]]>", "]]&gt;")
                + "]]></text></generalfeedback>"
            )
        if qtype == "multichoice":
            single = "true" if q.question_type != QuizQuestionType.multi else "false"
            parts.append(f"    <single>{single}</single>")
            parts.append("    <shuffleanswers>true</shuffleanswers>")
            n_correct = max(len(correct), 1)
            for idx, opt in enumerate(options):
                if idx in correct:
                    fraction = round(100 / n_correct, 5) if q.question_type == QuizQuestionType.multi else 100
                else:
                    fraction = 0
                parts.append(f'    <answer fraction="{fraction}" format="html">')
                parts.append("      <text><![CDATA[" + opt.replace("]]>", "]]&gt;") + "]]></text>")
                parts.append("    </answer>")
        else:
            is_true = 0 in correct
            for label, frac in (("true", 100 if is_true else 0), ("false", 0 if is_true else 100)):
                parts.append(f'    <answer fraction="{frac}"><text>{label}</text></answer>')
        parts.append("  </question>")
    parts.append("</quiz>")
    return "\n".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_owned(quiz_id: uuid.UUID, db: Session, user: CurrentUser) -> Quiz:
    quiz = (
        db.query(Quiz)
        .options(joinedload(Quiz.questions))
        .filter(Quiz.id == quiz_id, Quiz.tenant_id == user.tenant_id, Quiz.deleted_at.is_(None))
        .first()
    )
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    return quiz


def _quiz_out(quiz: Quiz) -> QuizOut:
    out = QuizOut.model_validate(quiz)
    out.question_count = len(quiz.questions)
    return out


def _question_student(q: QuizQuestion) -> QuestionOut:
    return QuestionOut(
        id=q.id,
        ordinal=q.ordinal,
        question_type=q.question_type.value,
        stem=q.stem,
        options=json.loads(q.options),
        points=q.points,
    )


def _question_full(q: QuizQuestion) -> QuestionFullOut:
    return QuestionFullOut(
        id=q.id,
        ordinal=q.ordinal,
        question_type=q.question_type.value,
        stem=q.stem,
        options=json.loads(q.options),
        points=q.points,
        correct=json.loads(q.correct),
        explanation=q.explanation,
        competency_code=q.competency_code,
        difficulty=q.difficulty,
    )


def _validate_question(q: QuestionIn) -> None:
    if any(i < 0 or i >= len(q.options) for i in q.correct):
        raise HTTPException(422, "Correct indices out of range for the provided options.")
    if q.question_type == "truefalse" and len(q.options) != 2:
        raise HTTPException(422, "truefalse questions need exactly 2 options.")
    if q.question_type == "mcq" and len(q.correct) != 1:
        raise HTTPException(422, "mcq questions need exactly one correct option.")


def _parse_questions(raw: str) -> list[dict]:
    """Validate AI quiz output into well-formed question dicts."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []

    valid: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            qtype = QuizQuestionType(str(item.get("question_type", "mcq")))
        except ValueError:
            qtype = QuizQuestionType.mcq
        options = item.get("options") or []
        correct = sorted({int(i) for i in (item.get("correct") or []) if isinstance(i, (int, float))})
        stem = str(item.get("stem") or "").strip()
        if not stem or len(options) < 2 or not correct:
            continue
        if any(i < 0 or i >= len(options) for i in correct):
            continue
        if qtype == QuizQuestionType.mcq and len(correct) != 1:
            qtype = QuizQuestionType.multi
        valid.append(
            {
                "question_type": qtype,
                "stem": stem,
                "options": [str(o) for o in options],
                "correct": correct,
                "explanation": str(item.get("explanation") or ""),
                "competency_code": str(item.get("competency_code") or ""),
                "difficulty": str(item.get("difficulty") or "intermediate"),
                "points": max(1, min(100, int(item.get("points") or 10))),
            }
        )
    return valid


def _update_module_progress(db: Session, quiz: Quiz, user_id: uuid.UUID, pct: int, passed: bool) -> None:
    module = db.get(CourseModule, quiz.module_id)
    if not module:
        return
    enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user_id, Enrollment.course_id == module.course_id)
        .first()
    )
    if not enrollment:
        return
    progress = (
        db.query(ModuleProgress)
        .filter(ModuleProgress.enrollment_id == enrollment.id, ModuleProgress.module_id == module.id)
        .first()
    )
    if not progress:
        progress = ModuleProgress(enrollment_id=enrollment.id, module_id=module.id)
        db.add(progress)
    progress.attempts += 1
    progress.score = max(progress.score, pct)
    progress.max_score = 100
    progress.last_accessed_at = datetime.now(UTC)
    if passed and pct >= module.pass_threshold:
        progress.status = ModuleProgressStatus.completed
        progress.completed_at = datetime.now(UTC)
    elif progress.status == ModuleProgressStatus.not_started:
        progress.status = ModuleProgressStatus.in_progress
