"""Who an LTI launch is, after the launch: session hand-offs and exercise learner links.

``lti_handoffs``: a launched Student who has no TrueNorth sign-in of their own (an account
the launch created, ``keycloak_id`` ``lti:<platform>:<sub>``) gets a single-use code in the
redirect's URL fragment. The SPA exchanges it once, within two minutes, from the browser
that launched (``bind_hash``: the hash of an HttpOnly cookie set on that launch), for a
short TrueNorth session (app/lti_identity/session.py). Only hashes are stored.

``exercise_learners``: an exercise launched from an LMS names the Student who launched
it, per run, so the run knows its LMS-launched trainees (gap #4 in
docs/moodle-integration.md). Attribution only: credit still comes from detections
(ADR 0005).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class LTIHandoff(Base):
    __tablename__ = "lti_handoffs"
    __table_args__ = (Index("ix_lti_handoffs_expires", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    bind_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")  # "" = not bound
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False, default="/")  # an SPA path, never a URL
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LTIUserLink(Base):
    """An LMS account (platform + LTI ``sub``) bound to a TrueNorth account, by that
    account's holder, signed in to TrueNorth (app/lti_identity/links.py). This is the only
    way an LTI launch becomes a staff account: never by the email the LMS asserts."""

    __tablename__ = "lti_user_links"
    __table_args__ = (
        UniqueConstraint("platform_id", "lti_sub", name="uq_lti_user_link_sub"),
        UniqueConstraint("platform_id", "user_id", name="uq_lti_user_link_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    lti_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    lms_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # as the LMS named them
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LTILinkRequest(Base):
    """A pending link: an LMS launch whose asserted email is a staff account's. Holds the
    hash of that email, never the email; confirmed once, within ten minutes, by the staff
    member signed in to TrueNorth in the browser that launched (``bind_hash``)."""

    __tablename__ = "lti_link_requests"
    __table_args__ = (Index("ix_lti_link_requests_expires", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    bind_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    lti_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lms_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExerciseLearner(Base):
    __tablename__ = "exercise_learners"
    __table_args__ = (
        UniqueConstraint("exercise_id", "user_id", name="uq_exercise_learner"),
        Index("ix_exercise_learners_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    platform_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=True)
    resource_link_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
