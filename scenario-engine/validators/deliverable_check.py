"""Deliverable validator — checks MinIO for uploaded artifact."""

from __future__ import annotations

import logging
import os

import httpx

from . import ValidationResult, Validator, register_validator

logger = logging.getLogger(__name__)


@register_validator
class DeliverableCheckValidator(Validator):
    validator_name = "deliverable_check"

    def check(self, params: dict, range_id: str, tenant_id: str) -> ValidationResult:
        bucket = params.get("bucket", f"exercise-{range_id}")
        object_key = params.get("key", "")
        minio_url = os.getenv("MINIO_ENDPOINT", "http://minio:9000")

        if not object_key:
            return ValidationResult(
                passed=False,
                validator_name="deliverable_check",
                evidence="No object key specified",
            )

        try:
            resp = httpx.head(f"{minio_url}/{bucket}/{object_key}")
            if resp.status_code == 200:
                return ValidationResult(
                    passed=True,
                    validator_name="deliverable_check",
                    evidence=f"Artifact found: {bucket}/{object_key}",
                )
            return ValidationResult(
                passed=False,
                validator_name="deliverable_check",
                evidence=f"Artifact not found: {bucket}/{object_key} (HTTP {resp.status_code})",
            )
        except Exception as e:
            return ValidationResult(
                passed=False,
                validator_name="deliverable_check",
                evidence=f"MinIO error: {e}",
            )
