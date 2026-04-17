"""TrueNorth Range — Validator base class and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of a validation check."""

    passed: bool
    validator_name: str
    evidence: str = ""
    detail: str = ""


class Validator(ABC):
    """Base class for all scenario validators."""

    @property
    @abstractmethod
    def validator_name(self) -> str:
        """Name that maps to this validator in scenario YAML."""
        ...

    @abstractmethod
    def check(self, params: dict, range_id: str, tenant_id: str) -> ValidationResult:
        """Run the validation check."""
        ...


# ── Registry ───────────────────────────────────────────────────────────
_VALIDATOR_REGISTRY: dict[str, type[Validator]] = {}


def register_validator(cls: type[Validator]) -> type[Validator]:
    instance = cls()
    _VALIDATOR_REGISTRY[instance.validator_name] = cls
    logger.info(f"Registered validator: {instance.validator_name}")
    return cls


def get_validator(name: str) -> Validator | None:
    cls = _VALIDATOR_REGISTRY.get(name)
    return cls() if cls else None


def list_validators() -> list[str]:
    return list(_VALIDATOR_REGISTRY.keys())
