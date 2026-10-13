"""Where signed audit exports are kept (ADR 0001 adapter, ADR 0011)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AuditArchiveError(RuntimeError):
    pass


class AuditArchive(ABC):
    """A write-once store of named byte blobs. Names are relative, '/'-separated paths
    (``2026/10/audit-2026-10-11.jsonl.gz``, ``keys/<key_id>.pub.pem``)."""

    @abstractmethod
    def put(self, name: str, data: bytes) -> None:
        """Store ``data`` under ``name`` durably. Refuses to replace different content."""

    @abstractmethod
    def get(self, name: str) -> bytes:
        """The bytes stored under ``name``; AuditArchiveError if absent."""

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """Every stored name starting with ``prefix``, sorted."""

    @staticmethod
    def check_name(name: str) -> str:
        parts = name.split("/")
        if not name or name.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise AuditArchiveError(f"not an archive name: {name!r}")
        return name
