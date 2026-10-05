"""OS confinement for one Course Studio job.

The engine runs request and feedback text from course authors in any tenant, and its
tools include arbitrary Python. Claude Code's ``--allowedTools`` rules match command
text and are not a security boundary. So the runner puts the whole ``claude`` process
tree for a job in an OS sandbox. The sandbox denies by default and then allows only
what the job needs:

* **Writes**: only the job's run (``<runs>/<slug>/``), its request file
  (``<runs>/<slug>.request.txt``), the job's own home directory (Claude Code's config,
  sessions and temp files; created per job and deleted afterwards) and ``/dev`` nodes.
  It cannot write the repository, other runs, ``_studio/`` (where the API records
  ownership), ``_queue/``, ``_jobs/``, ``_history/``, the runner's home, or any shared
  location a later job or the operator would load (``~/.claude``, ``~/.gitconfig``,
  shell profiles, launch agents, shared temp dirs).
* **Reads**: nothing under the runner account's home except the repository, the
  ``claude`` installation, the job's run and the job's home. That excludes other runs,
  ``_studio``, ``_queue``, ``_jobs``, other jobs' Claude sessions, ``~/.ssh`` and
  ``~/.docker``. ``.env*`` files in the repository are not readable either.
* **Processes**: no signals to processes outside the sandbox, and no inspection of them
  (their arguments carry other tenants' requests).
* **Network**: no Unix-domain sockets (the Docker socket) and no localhost (the API,
  Redis, Postgres), except the DNS resolver socket and, when configured, the local
  model fallback's port. Internet access to the model API stays open.

The kernel checks paths after resolving symlinks, so a link inside the run reaches
nothing. The job's credentials are in its environment, so a job can always use them.
Run the runner under a dedicated account whose token can be revoked (see
docs/arc2-course-studio.md).

Backends: ``seatbelt`` (macOS ``sandbox-exec``) and ``none``. Selection comes from
``ARC2_CONFINE``: ``auto`` (the default) uses Seatbelt where it exists and otherwise
fails closed. ``none`` runs unconfined, is for a single-tenant host only, and must be
set explicitly. There is no Linux (bubblewrap) backend yet.
"""

from __future__ import annotations

import os
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

DNS_SOCKET = "/private/var/run/mDNSResponder"


class ConfinementError(RuntimeError):
    """No usable sandbox for the configured choice."""


@dataclass(frozen=True)
class Jail:
    """What one confined process may reach."""

    repo: Path  # readable, never writable
    runs: Path  # the runs root: unreadable and unwritable except ``writable`` below
    home: Path  # the runner account's home: unreadable except what is listed
    writable: tuple[Path, ...]  # read-write subtrees (the job's run, its home)
    writable_files: tuple[Path, ...] = ()  # read-write single files (the request file)
    readable: tuple[Path, ...] = ()  # extra read-only subtrees (the claude installation)
    local_ports: tuple[int, ...] = ()  # localhost ports the job may connect to
    readable_inner: tuple[Path, ...] = ()  # read-only subtrees inside the runs root


class Confinement(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def wrap(self, cmd: list[str], jail: Jail) -> list[str]:
        """``cmd`` rewritten to run inside ``jail``."""


class Seatbelt(Confinement):
    name = "seatbelt"

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None

    def profile(self, jail: Jail) -> str:
        q = _quote
        rw = [f"(subpath {q(p)})" for p in jail.writable] + [f"(literal {q(p)})" for p in jail.writable_files]
        reads = [f"(subpath {q(p)})" for p in (jail.repo, *jail.readable)]
        # Seatbelt applies the last rule that matches: broad denials first, exceptions after.
        rules = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            '(allow file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty")'
            ' (literal "/dev/dtracehelper") (regex #"^/dev/fd/"))',
            f"(deny file-read-data (subpath {q(jail.home)}))",
            f"(allow file-read-data {' '.join(reads)})",
            f"(deny file-read-data (subpath {q(jail.runs)}))",
            r'(deny file-read-data (regex #"/\.env[^/]*$"))',
            *(
                [f"(allow file-read-data {' '.join(f'(subpath {q(p)})' for p in jail.readable_inner)})"]
                if jail.readable_inner
                else []
            ),
            f"(allow file-read-data file-write* {' '.join(rw)})",
            "(deny signal (target others))",
            "(deny process-info* (target others))",
            "(deny network-outbound (remote unix-socket))",
            f'(allow network-outbound (remote unix-socket (path-literal "{DNS_SOCKET}")))',
            '(deny network-outbound (remote ip "localhost:*"))',
            *(f'(allow network-outbound (remote ip "localhost:{port}"))' for port in jail.local_ports),
            "",
        ]
        return "\n".join(rules)

    def wrap(self, cmd: list[str], jail: Jail) -> list[str]:
        return ["sandbox-exec", "-p", self.profile(jail), *cmd]


class Unconfined(Confinement):
    name = "none"

    def available(self) -> bool:
        return True

    def wrap(self, cmd: list[str], jail: Jail) -> list[str]:
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


def _quote(path: Path) -> str:
    """A resolved path as an SBPL string literal."""
    text = str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
