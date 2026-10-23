"""Acknowledgement request and response shapes (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .visible import has_visible_text

# Long enough for any rules of use; short enough that one request cannot fill the table.
MAX_TEXT = 100_000


class AcknowledgementVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int | None = Field(None, description="None while a draft; 1, 2, 3... once published")
    text: str = Field(..., description="Markdown")
    published_at: datetime | None = None
    published_by: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AcknowledgementCurrentOut(BaseModel):
    """What the caller must sign, if anything (app/acknowledgement/rule.py)."""

    version: AcknowledgementVersionOut | None = Field(None, description="The current published version; None before the first")
    required: bool = Field(..., description="The caller must sign it before doing anything else")
    reason: str = Field(..., description="none_published | never_signed | new_version | expired | signed")
    signed_at: datetime | None = Field(None, description="The caller's latest signature of this version")
    expires_at: datetime | None = Field(None, description="When that signature stops counting (365 days)")


class AcknowledgementSignIn(BaseModel):
    version_id: uuid.UUID = Field(..., description="The version the caller read; must be the current one")


class AcknowledgementDraftIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT, description="Markdown, with some visible text")

    @field_validator("text")
    @classmethod
    def _visible(cls, value: str) -> str:
        # A text that renders blank would lock everyone out behind an empty form (visible.py).
        if not has_visible_text(value):
            raise ValueError("the text shows nothing to read once rendered; write the rules of use")
        return value
