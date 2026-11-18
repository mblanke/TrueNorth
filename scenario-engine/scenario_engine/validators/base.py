# Author: Marc-Andre Blanke
# Created: 2026-03-04T22:24:49Z
from abc import ABC, abstractmethod
from typing import Any


class BaseValidator(ABC):
    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}

    @abstractmethod
    def validate(self, context: dict[str, Any]) -> bool: ...
