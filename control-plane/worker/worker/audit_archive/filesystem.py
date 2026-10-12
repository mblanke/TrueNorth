"""The filesystem audit archive: a directory (AUDIT_ARCHIVE_DIR) on the worker's volume.

Each write goes to ``<name>.tmp``, is fsynced, renamed into place and the directory
fsynced, so a crash leaves either the old state or the whole file. Files are 0640. An
existing name is never overwritten with different bytes (identical bytes: no-op), which
is what lets a rerun after a crash finish an interrupted export safely.

The directory is backed up and verified by scripts/backup/backup.sh. It is not WORM
storage: MinIO with object lock is the planned second backend (v1.3).
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import AuditArchive, AuditArchiveError

DEFAULT_DIR = "/var/lib/truenorth/audit-archive"
FILE_MODE = 0o640


class FilesystemArchive(AuditArchive):
    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root or os.getenv("AUDIT_ARCHIVE_DIR", DEFAULT_DIR))

    def _path(self, name: str) -> Path:
        return self.root / self.check_name(name)

    def put(self, name: str, data: bytes) -> None:
        path = self._path(name)
        if path.exists():
            if path.read_bytes() == data:
                return
            raise AuditArchiveError(f"{name} already exists with different content; archive files are never replaced")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def get(self, name: str) -> bytes:
        path = self._path(name)
        if not path.is_file():
            raise AuditArchiveError(f"{name} is not in the archive ({self.root})")
        return path.read_bytes()

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def list(self, prefix: str = "") -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.endswith(".tmp"):
                rel = p.relative_to(self.root).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)
