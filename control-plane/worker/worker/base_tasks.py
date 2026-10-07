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
from .provisioners import get_provisioner


def _get_backend(backend: str | None = None):
    """Return an instantiated provisioner for the given backend name.

    Falls back to the ``PROVISIONER_BACKEND`` environment variable, then
    to ``"mock"`` when neither the caller nor the env provides a value.
    """
    resolved = backend or os.getenv("PROVISIONER_BACKEND", "mock")
    return get_provisioner(resolved)


class ReliableTask(Task):
    """Base task with exponential backoff + jitter on retries."""

    autoretry_for = (Exception,)
    dont_autoretry_for = FINAL_ERRORS  # the soft time limit: record failed, do not retry
    max_retries = 3
    retry_backoff = True  # Exponential backoff
    retry_backoff_max = 300  # Max 5 minutes between retries
    retry_jitter = True  # Add randomness to prevent thundering herd
