"""TrueNorth Range - what every task module shares: the retrying base task and the backend lookup.

Kept out of ``tasks.py`` so that ``lab_tasks.py`` can use them without importing
``tasks``: ``celery_app`` imports ``lab_tasks`` at the end of its own import, and
``tasks`` imports ``celery_app``, so ``lab_tasks`` importing ``tasks`` was a cycle
that made a bare ``import worker.tasks`` fail.
"""

from __future__ import annotations

import os

from celery import Task

from . import connection_binding
from .fencing import FINAL_ERRORS
from .provisioners import get_provisioner


def _get_backend(backend: str | None = None, range_id: str | None = None, *, output: dict | None = None,
                 teardown: bool = False, build: bool = False):
    """Return an instantiated provisioner for the given backend name.

    Falls back to the ``PROVISIONER_BACKEND`` environment variable, then
    to ``"mock"`` when neither the caller nor the env provides a value.

    With ``range_id``, a backend that takes credentials (vsphere_api) reaches the endpoint
    the range was built on, recorded in its provisioner_output (worker/connection_binding.py),
    and refuses (ConnectionBindingError) when that connection is gone or now names another
    host. A range with nothing recorded gets its own tenant's active connection (H6), else a
    shared one, else the environment (VSPHERE_*); ``build`` then records it. ``output``: the
    range's provisioner_output when the caller has read it already.

    ``teardown`` (destroy, leak cleanup) is allowed on an experimental backend that has
    been switched off since the range was built.
    """
    resolved = backend or os.getenv("PROVISIONER_BACKEND", "mock")
    creds, rec = connection_binding.resolve(resolved, range_id, output)
    provisioner = get_provisioner(resolved, creds, teardown=teardown)
    connection_binding.verify(provisioner, rec)
    if build and rec is None and range_id:
        connection_binding.record_build(range_id, provisioner)
    elif rec is None:  # built before records: backfill once one of its VMs is found here
        connection_binding.hook(range_id, provisioner)
    return provisioner


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
