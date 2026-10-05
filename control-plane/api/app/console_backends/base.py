"""The console seam: a short-lived URL that opens one VM's screen in a browser."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class ConsoleError(Exception):
    """The console could not be opened; the message is safe to show the student."""


class BaseConsoleBackend(ABC):
    kind: ClassVar[str]

    @abstractmethod
    def open(self, vm: dict[str, Any]) -> dict[str, Any]:
        """Console access for one VM of a range (an entry of its provisioner output:
        ``vm_id``, ``name``, ...). Returns ``{"kind", "url", "expires_in"}``; the URL is
        single-use or short-lived, never a standing credential."""
