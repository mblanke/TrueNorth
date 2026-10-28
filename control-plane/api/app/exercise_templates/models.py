"""The exercise template's tables (ADR 0016).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

* ``exercise_templates``: the reusable part of an exercise: objectives, the MESL, the
  scenario and range template it runs on, the teams it needs, the instructor's notes.
  ``tenant_id`` is the owner; ``is_public`` shares it with every tenant, as scenarios and
  range templates are shared. ``revision`` rises on every edit. The lists are JSON, validated
  by ``schemas`` (the same shapes the content port carries).
* ``exercise_template_uses``: one row per exercise created from a template: the template and
  the revision it came from. The exercise itself keeps its own copy of the objectives and the
  MESL, so editing the template never changes it, and ``exercises`` (``app.models``) is not
  changed: an exercise made by hand has no row here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, SoftDeleteMixin, TimestampMixin

__all__ = ["ExerciseTemplate", "ExerciseTemplateUse"]


class ExerciseTemplate(SoftDeleteMixin, TimestampMixin, Base):
    __tablename__ = "exercise_templates"
    __table_args__ = (Index("ix_exercise_templates_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("scenarios.id"), nullable=True)
    range_template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("templates.id"), nullable=True)
    teams: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Participant roles (min/max/default, VM profile per participant) and the rules by which
    # shared infrastructure scales; the sizing itself is the Exercise Studio's (ADR 0016).
    roles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scaling_rules: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    objectives: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    mesl: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    instructor_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class ExerciseTemplateUse(TimestampMixin, Base):
    __tablename__ = "exercise_template_uses"
    __table_args__ = (Index("ix_exercise_template_uses_template", "template_id"),)

    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), primary_key=True)
    template_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercise_templates.id"), nullable=False)
    template_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
