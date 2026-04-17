"""Manual acknowledgement validator — checks API for objective ack."""

from __future__ import annotations

import logging
import os

import httpx

from . import ValidationResult, Validator, register_validator

logger = logging.getLogger(__name__)


@register_validator
class ManualAckValidator(Validator):
    validator_name = "manual_ack"

    def check(self, params: dict, range_id: str, tenant_id: str) -> ValidationResult:
        """
        Checks if an objective has been manually acknowledged via the API.
        This validator is designed to be polled periodically or triggered by a webhook.
        """
        exercise_id = params.get("exercise_id", "")
        objective_ref = params.get("objective_ref", "")
        api_url = os.getenv("API_URL", "http://api:8080")

        if not exercise_id or not objective_ref:
            return ValidationResult(
                passed=False,
                validator_name="manual_ack",
                evidence="Missing exercise_id or objective_ref in params",
            )

        try:
            resp = httpx.get(f"{api_url}/exercises/{exercise_id}/objectives")
            resp.raise_for_status()
            objectives = resp.json()
            for obj in objectives:
                if obj.get("ref_id") == objective_ref and obj.get("achieved"):
                    return ValidationResult(
                        passed=True,
                        validator_name="manual_ack",
                        evidence=obj.get("evidence", "Manually acknowledged"),
                    )
            return ValidationResult(passed=False, validator_name="manual_ack", evidence="Not yet acknowledged")
        except Exception as e:
            return ValidationResult(passed=False, validator_name="manual_ack", evidence=f"API error: {e}")
