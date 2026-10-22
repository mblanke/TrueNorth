"""Response shapes of the certification endpoints (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Verdict = Literal["certified", "ready_not_certified", "not_safe"]
SectionStatus = Literal["pass", "fail", "incomplete", "info", "missing"]


class CertificationEntryOut(BaseModel):
    seq: int
    section: str
    status: Literal["pass", "fail", "incomplete", "info"]
    summary: str
    detail: dict[str, Any]
    source: str = Field(description="What wrote it: api.capture, correction, api.<action>, a slice's worker step")
    actor: str = Field(description="The person behind it, or 'system' for evidence the platform recorded")
    actor_id: uuid.UUID | None = None
    reason: str | None = None
    corrects_seq: int | None = Field(None, description="The entry this one corrects (it is kept)")
    recorded_at: datetime


class CertificationSectionOut(BaseModel):
    name: str
    title: str
    required: bool = Field(description="Needed for 'certified'; an optional section can still fail the record")
    provided_by: str = Field(description="The slice or issue that records it")
    status: SectionStatus = Field(description="Its latest entry's status; missing when it has none")
    summary: str
    updated_at: datetime | None = None
    history_count: int = Field(description="How many entries the section has, superseded ones included")
    entries: list[CertificationEntryOut] = Field(
        description="The latest entry only, unless history was asked for (?history=1; exports always)")


class CertificationRecordOut(BaseModel):
    id: uuid.UUID
    deployment_id: uuid.UUID = Field(description="The provision operation (GET /ranges/{id}/operations) it certifies")
    generation: int
    range_id: uuid.UUID
    range_name: str
    template_id: uuid.UUID | None = None
    template_name: str
    template_version: str
    plan_hash: str
    plan_hash_basis: str = Field(description="What plan_hash is a SHA-256 of")
    opened_by: str
    opened_by_id: uuid.UUID | None = None
    opened_at: datetime
    tenant_id: uuid.UUID
    deployment_status: str = Field(
        description="The provision operation's status: pending, dispatched (building), held (by a maintenance "
        "window, re-sent when it ends), succeeded, failed, superseded")
    verdict: Verdict
    verdict_label: str = Field(description="Certified for Students / Ready, not certified / Not safe")
    reasons: list[str] = Field(description="Why it is not certified: failures, or what is missing")
    history: bool = Field(description="True: every entry of each section; False: the latest only")
    sections: list[CertificationSectionOut]


class ArchivedCertificationOut(BaseModel):
    deployment_id: uuid.UUID
    generation: int
    opened_at: datetime
    verdict: Verdict
    verdict_label: str
    purge_after: datetime | None = Field(
        description="When retention may delete it (RANGE_CERT_RETENTION_MONTHS); export it before. None: kept forever")


class RangeCertificationOut(BaseModel):
    range_id: uuid.UUID
    record: CertificationRecordOut | None = Field(
        description="The current record: the newest build's (or the one asked for). None: no build has one yet")
    archived: list[ArchivedCertificationOut] = Field(description="Earlier builds' records, newest first")
    retention_months: int = Field(description="Archived records older than this are purged; 0: kept forever")


class ExportSignatureOut(BaseModel):
    alg: Literal["Ed25519"]
    key_id: str
    public_key: str = Field(description="Base64 raw public key; trust it only if key_id matches one known out of band")
    signed_sha256: str = Field(description="SHA-256 of the signed bytes: canonical JSON of the export minus signature")
    value: str


class CertificationExportOut(BaseModel):
    format: Literal["truenorth.range-certification.v1"]
    record_sha256: str = Field(description="SHA-256 of the canonical JSON of `record` alone: the same for every "
                                           "export (and the PDF) of the record in this state")
    exported_at: datetime
    exported_by: str
    record: CertificationRecordOut
    signature: ExportSignatureOut


class CertificationCaptureOut(BaseModel):
    deployment_id: uuid.UUID
    written: int = Field(description="Entries appended; 0 when the evidence had not changed")


class IsolationCheckOut(BaseModel):
    range_id: uuid.UUID
    dispatched: bool = Field(description="False: the task queue was not reachable; ask again later. The new "
                                         "evidence (with the worker's mode) shows on the certification record")


class SigningKeyOut(BaseModel):
    key_id: str = Field(description="First 16 hex characters of the SHA-256 of the raw public key")
    alg: Literal["Ed25519"]
    public_key: str = Field(description="Base64 raw Ed25519 public key")
    first_used_at: datetime | None = Field(description="When it first signed an export; None: not yet")
    current: bool = Field(description="The key that signs exports now")
