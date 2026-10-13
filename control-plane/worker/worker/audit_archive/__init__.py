"""Audit archive backends, by AUDIT_ARCHIVE_BACKEND (default ``filesystem``). ADR 0001:
a new store (MinIO with object lock, v1.3) is a new module and a registry entry here, and
its SDK is imported only inside that module."""

from __future__ import annotations

import os
from collections.abc import Callable

from .base import AuditArchive, AuditArchiveError
from .filesystem import FilesystemArchive

BACKENDS: dict[str, Callable[[], AuditArchive]] = {
    "filesystem": FilesystemArchive,
}


def get_archive(name: str | None = None) -> AuditArchive:
    key = (name or os.getenv("AUDIT_ARCHIVE_BACKEND", "filesystem")).strip().lower()
    try:
        return BACKENDS[key]()
    except KeyError:
        raise AuditArchiveError(f"AUDIT_ARCHIVE_BACKEND={key!r} is not one of {', '.join(sorted(BACKENDS))}") from None


__all__ = ["BACKENDS", "AuditArchive", "AuditArchiveError", "FilesystemArchive", "get_archive"]
