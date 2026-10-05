"""Retry policy shared by the worker's task modules.

Kept out of tasks.py so a task module (power_tasks.py) can use it without importing
tasks.py at load time: celery_app imports every task module at its end, so a task
module that imported tasks.py could find it half-initialised when tasks.py was the
first module imported.
"""

from __future__ import annotations

from celery import Task


class ReliableTask(Task):
    """Base task with exponential backoff + jitter on retries."""

    autoretry_for = (Exception,)
    max_retries = 3
    retry_backoff = True  # Exponential backoff
    retry_backoff_max = 300  # Max 5 minutes between retries
    retry_jitter = True  # Add randomness to prevent thundering herd


def _last_attempt(task) -> bool:
    """True when a failure now will not be retried, or the task was called directly."""
    return bool(task.request.called_directly) or task.request.retries >= (task.max_retries or 0)
