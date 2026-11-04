#!/usr/bin/env python3
"""Print a stable fingerprint of every Git-visible working-tree change.

``git diff HEAD`` omits untracked files. That made the DoD marker blind to new source,
migrations and tests until somebody staged them. This combines the binary tracked diff with
the path, type, mode and content of every non-ignored untracked entry without following
symlinks. The output is intentionally platform-neutral so dod.sh and dod.ps1 agree.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Protocol


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def _field(digest: _Digest, name: bytes, value: bytes) -> None:
    digest.update(name)
    digest.update(b"\0")
    digest.update(str(len(value)).encode("ascii"))
    digest.update(b"\0")
    digest.update(value)


def fingerprint(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    _field(digest, b"tracked-diff", _git(root, "diff", "--binary", "--no-ext-diff", "HEAD"))

    raw_paths = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_path in sorted(p for p in raw_paths.split(b"\0") if p):
        path = root / os.fsdecode(raw_path)
        info = path.lstat()
        _field(digest, b"path", raw_path)
        _field(digest, b"mode", f"{stat.S_IFMT(info.st_mode):o}:{stat.S_IMODE(info.st_mode):o}".encode())
        if stat.S_ISLNK(info.st_mode):
            _field(digest, b"symlink", os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            _field(digest, b"file", path.read_bytes())
        else:
            _field(digest, b"special", b"")
    return digest.hexdigest()[:16]


if __name__ == "__main__":
    repo = Path(sys.argv[1]) if len(sys.argv) == 2 else Path.cwd()
    print(fingerprint(repo))
