"""Durable low-side outbox and high-side authoritative result records.

The outbox contains the signed ciphertext plus the exact public projection; it has no
readable score or person mapping. Subject bindings and imported numeric results are
populated only by the Secret-side service guard in ``service.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class ResultTransferOutbox(Base):
    __tablename__ = "result_transfer_outbox"
    __table_args__ = (
        Index("ix_result_outbox_tenant_state", "tenant_id", "state"),
        Index("ix_result_outbox_attempt", "attempt_id"),
    )

    package_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    opaque_subject_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    course_release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    assessment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    outcome: Mapped[str] = mapped_column(String(4), nullable=False)
    source_enclave: Mapped[str] = mapped_column(String(100), nullable=False)
    destination_enclave: Mapped[str] = mapped_column(String(100), nullable=False)
    package_json: Mapped[str] = mapped_column(Text, nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResultSubjectBinding(Base):
    """Secret-only mapping from a source enclave's random token to a real TN user."""

    __tablename__ = "result_subject_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_enclave", "opaque_subject_id", name="uq_result_subject_source_token"),
        Index("ix_result_subject_user", "tenant_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    source_enclave: Mapped[str] = mapped_column(String(100), nullable=False)
    opaque_subject_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProvisionedSubjectCredential(Base):
    """Unclassified inbox entry containing no person mapping or authentication secret."""

    __tablename__ = "provisioned_subject_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_enclave", "opaque_subject_id", name="uq_provisioned_subject_source_token"
        ),
        Index("ix_provisioned_subject_tenant_state", "tenant_id", "state"),
    )

    credential_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    opaque_subject_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    source_enclave: Mapped[str] = mapped_column(String(100), nullable=False)
    destination_enclave: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_json: Mapped[str] = mapped_column(Text, nullable=False)
    credential_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="available")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ImportedResult(Base):
    """Secret authoritative grade with source and correction provenance."""

    __tablename__ = "imported_results"
    __table_args__ = (
        Index("ix_imported_results_user_time", "tenant_id", "user_id", "occurred_at"),
        Index("ix_imported_results_attempt", "source_enclave", "attempt_id"),
        UniqueConstraint("supersedes_package_id", name="uq_imported_result_supersedes"),
    )

    package_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    subject_binding_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("result_subject_bindings.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    source_enclave: Mapped[str] = mapped_column(String(100), nullable=False)
    course_release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    assessment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    raw_score: Mapped[int] = mapped_column(Integer, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(4), nullable=False)
    scorer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence_sha256_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    supersedes_package_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("imported_results.package_id"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
