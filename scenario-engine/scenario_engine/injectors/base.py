from abc import ABC, abstractmethod
from typing import Any


class BaseInjector(ABC):
    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}

    @abstractmethod
    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        ...

    def validate_params(self) -> None:
        pass