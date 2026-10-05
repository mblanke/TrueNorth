"""OS confinement for one Course Studio job.

The engine runs request and feedback text from course authors in any tenant, and its
tools include arbitrary Python. Claude Code's ``--allowedTools`` rules match command
text and are not a security boundary. A Bash sandbox would still leave the repository
(the working directory) writable. So the runner wraps the whole ``claude`` process tree
in an OS sandbox for the job:

* **write** only inside ``<runs>/<slug>/`` and ``<runs>/<slug>.request.txt``. Nothing
  else in the repository or the runs root, so no other run, ``_studio/`` (where the API
  records ownership), ``_queue/`` or ``_jobs/``;
* **read** none of the runs root except those two paths. The rest of the repository
  (agent definitions, content, tools) stays readable;
* writes outside the repository and runs root (Claude Code's own ``~/.claude``, temp
  files) and the network are left alone. The engine needs them.

Paths are checked by the kernel after symlinks are resolved, so a link inside the run
does not reach out of it.

Backends: ``seatbelt`` (macOS ``sandbox-exec``) and ``none``. Selection comes from
``ARC2_CONFINE``: ``auto`` (the default) uses Seatbelt where it exists and otherwise
fails closed. ``none`` runs unconfined; it is for a single-tenant host and must be set
explicitly. A Linux backend (bubblewrap) is not written yet.
"""

from __future__ import annotations

import os
import shutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path


class ConfinementError(RuntimeError):
    """No usable sandbox for the configured choice."""


class Confinement(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def wrap(self, cmd: list[str], *, repo: Path, runs: Path, slug: str) -> list[str]:
        """``cmd`` rewritten to run confined to ``runs/slug``."""


class Seatbelt(Confinement):
    name = "seatbelt"

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None

    def profile(self, *, repo: Path, runs: Path, slug: str) -> str:
        repo_s, runs_s = _sbpl(repo.resolve()), _sbpl(runs.resolve())
        # Seatbelt applies the last rule that matches, so the narrow allows come last.
        return "\n".join(
            [
                "(version 1)",
                "(allow default)",
                f'(deny file-write* (subpath "{repo_s}"))',
                f'(deny file-write* (subpath "{runs_s}"))',
                f'(deny file-read-data (subpath "{runs_s}"))',
                f'(allow file-write* file-read-data (subpath "{runs_s}/{slug}"))',
                f'(allow file-write* file-read-data (literal "{runs_s}/{slug}.request.txt"))',
                "",
            ]
        )

    def wrap(self, cmd: list[str], *, repo: Path, runs: Path, slug: str) -> list[str]:
        return ["sandbox-exec", "-p", self.profile(repo=repo, runs=runs, slug=slug), *cmd]


class Unconfined(Confinement):
    name = "none"

    def available(self) -> bool:
        return True

    def wrap(self, cmd: list[str], *, repo: Path, runs: Path, slug: str) -> list[str]:
        return cmd


BACKENDS: dict[str, type[Confinement]] = {"seatbelt": Seatbelt, "none": Unconfined}


def select(choice: str | None = None) -> Confinement:
    """The configured backend. Raises ConfinementError rather than silently running unconfined."""
    choice = (choice or os.environ.get("ARC2_CONFINE") or "auto").strip().lower()
    if choice == "auto":
        seatbelt = Seatbelt()
        if seatbelt.available():
            return seatbelt
        raise ConfinementError(
            "no OS sandbox for the ARC² engine on this host (Seatbelt needs macOS). "
            "Set ARC2_CONFINE=none to run jobs unconfined, on a single-tenant host only."
        )
    backend_type = BACKENDS.get(choice)
    if backend_type is None:
        raise ConfinementError(f"unknown ARC2_CONFINE={choice!r}; use auto, {', '.join(BACKENDS)}")
    backend = backend_type()
    if not backend.available():
        raise ConfinementError(f"ARC2_CONFINE={choice} is not available on this host")
    return backend


def _sbpl(path: Path) -> str:
    """A path as an SBPL string literal body."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')
