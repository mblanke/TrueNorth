"""Request and response bodies of the cmi5 routes (kept out of ``app/schemas.py``, ADR 0003)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Cmi5AuOut(BaseModel):
    index: int
    publisher_id: str
    title: str
    description: str
    move_on: str
    mastery_score: float | None
    url: str  # TrueNorth's AU runtime for this AU
    # The caller's own registration, when they have one on this release.
    completed: bool = False
    passed: bool = False
    waived: str | None = None
    satisfied: bool = False


class Cmi5StructureOut(BaseModel):
    release_id: uuid.UUID
    course_id: uuid.UUID
    publisher_id: str
    title: str
    registration: uuid.UUID | None  # the caller's, if any
    course_satisfied: bool
    enrolled: bool  # the caller may launch (enrolled and pinned to this release)
    aus: list[Cmi5AuOut]


class Cmi5PageOut(BaseModel):
    path: str
    html: str


class Cmi5QuestionOut(BaseModel):
    id: str
    stem: str
    options: list[str]


class Cmi5QuizOut(BaseModel):
    title: str
    questions: list[Cmi5QuestionOut]


class Cmi5ContentOut(BaseModel):
    index: int
    title: str
    lang: str | None
    move_on: str
    mastery_score: float | None
    pages: list[Cmi5PageOut]
    quiz: Cmi5QuizOut | None


class Cmi5GradeIn(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict, description="option letter (A, B, ...) per question id")


class Cmi5GradeOut(BaseModel):
    correct: int
    total: int
    scaled: float


class Cmi5LaunchIn(BaseModel):
    launch_mode: Literal["Normal", "Browse", "Review"] | None = None


class Cmi5LaunchOut(BaseModel):
    url: str
    session_id: uuid.UUID
    registration: uuid.UUID
    launch_mode: str
    launch_method: str


class Cmi5FetchOut(BaseModel):
    """The fetch URL's answer (cmi5 8.2): the token, or an error code. Always HTTP 200."""

    model_config = ConfigDict(populate_by_name=True)

    auth_token: str | None = Field(default=None, alias="auth-token")
    error_code: str | None = Field(default=None, alias="error-code")
    error_text: str | None = Field(default=None, alias="error-text")


class Cmi5WaiveIn(BaseModel):
    reason: Literal["Tested Out", "Equivalent AU", "Equivalent Outside Activity", "Administrative"]


class Cmi5SatisfiedOut(BaseModel):
    satisfied: list[str]  # newly satisfied: "block:<n>", "course"
