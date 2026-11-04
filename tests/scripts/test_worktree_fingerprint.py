from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "worktree_fingerprint.py"


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fingerprint(repo: Path) -> str:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_fingerprint_covers_tracked_and_untracked_state_but_not_ignored_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.test")
    _run(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("ignored\n")
    (repo / "tracked").write_text("original\n")
    _run(repo, "add", ".gitignore", "tracked")
    _run(repo, "commit", "-qm", "initial")
    clean = _fingerprint(repo)

    (repo / "tracked").write_text("changed\n")
    tracked = _fingerprint(repo)
    assert tracked != clean

    (repo / "tracked").write_text("original\n")
    (repo / "new").write_text("one\n")
    untracked = _fingerprint(repo)
    assert untracked != clean
    (repo / "new").write_text("two\n")
    assert _fingerprint(repo) != untracked

    if os.name != "nt":
        before_mode = _fingerprint(repo)
        (repo / "new").chmod(0o755)
        assert _fingerprint(repo) != before_mode

    (repo / "ignored").write_text("not release state\n")
    ignored = _fingerprint(repo)
    (repo / "ignored").write_text("still not release state\n")
    assert _fingerprint(repo) == ignored
