"""TrueNorth Range - what every task module shares: the retrying base task and the backend lookup.

Kept out of ``tasks.py`` so that ``lab_tasks.py`` can use them without importing
``tasks``: ``celery_app`` imports ``lab_tasks`` at the end of its own import, and
``tasks`` imports ``celery_app``, so ``lab_tasks`` importing ``tasks`` was a cycle
that made a bare ``import worker.tasks`` fail.
"""

from __future__ import annotations

import os

from celery import Task

from .fencing import FINAL_ERRORS
from .provisioners import CREDENTIALS_HYPERVISOR, get_provisioner


def _get_backend(backend: str | None = None, range_id: str | None = None):
    """Return an instantiated provisioner for the given backend name.

    Falls back to the ``PROVISIONER_BACKEND`` environment variable, then
    to ``"mock"`` when neither the caller nor the env provides a value.

    With ``range_id``, a backend that takes credentials (vsphere_api) logs in with the
    range's own tenant's hypervisor connection (H6), else a shared one; with neither, its
    environment (VSPHERE_*). Every operation on a range (build, destroy, power, snapshots,
    health) resolves them the same way, so a build and its teardown reach the same vCenter.
    """
    resolved = backend or os.getenv("PROVISIONER_BACKEND", "mock")
    return get_provisioner(resolved, range_credentials(resolved, range_id))


def range_credentials(backend: str, range_id: str | None) -> dict:
    """The connection ``backend`` should use for ``range_id`` ({}: its environment)."""
    hypervisor = CREDENTIALS_HYPERVISOR.get(backend)
    if not hypervisor or not range_id:
        return {}
    from . import db_ops
    from .tasks import _db_session  # at call time: tasks imports this module

    with _db_session() as db:
        return db_ops.hypervisor_creds(db, hypervisor, range_id)


def db_connect_args(url: str) -> dict:
    """Driver arguments for a task's database engine: on PostgreSQL a connect timeout
    (``DB_CONNECT_TIMEOUT_SECONDS``, default 10), so an unreachable database fails a
    lease renewal (worker/fencing.py) in seconds instead of the OS's minutes."""
    if url.startswith("postgresql"):
        return {"connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10"))}
    return {}


class ReliableTask(Task):
    """Base task with exponential backoff + jitter on retries."""

    autoretry_for = (Exception,)
    dont_autoretry_for = FINAL_ERRORS  # the soft time limit: record failed, do not retry
    max_retries = 3
    retry_backoff = True  # Exponential backoff
    retry_backoff_max = 300  # Max 5 minutes between retries
    retry_jitter = True  # Add randomness to prevent thundering herd
