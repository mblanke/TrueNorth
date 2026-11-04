"""External identity seam for Unclassified pseudonymous learners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class PseudonymousIdentityError(RuntimeError):
    """The identity provider refused an operation; messages contain no credentials."""


@dataclass(frozen=True)
class PseudonymousIdentity:
    subject: str
    username: str


@dataclass(frozen=True)
class OneTimeLogin:
    subject: str
    username: str
    password: str = field(repr=False)


class BasePseudonymousIdentityBackend(ABC):
    kind: str

    @abstractmethod
    def create_disabled_account(self) -> PseudonymousIdentity:
        """Create an identity-free account that cannot sign in yet."""

    @abstractmethod
    def prepare_one_time_login(self, subject: str) -> OneTimeLogin:
        """Keep the account disabled and replace its password with a temporary one."""

    @abstractmethod
    def enable_account(self, subject: str) -> None:
        """Enable a marker-verified pseudonymous account after credential delivery."""

    @abstractmethod
    def disable_account(self, subject: str) -> None:
        """Disable a marker-verified pseudonymous account."""

    @abstractmethod
    def delete_account(self, subject: str) -> None:
        """Delete a marker-verified, unbound account during creation compensation."""
