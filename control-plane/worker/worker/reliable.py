"""Retry policy shared by the worker's task modules.

Kept out of tasks.py so another task module (lab_tasks.py) can use it without importing
tasks.py at load time: celery_app imports every task module at its end, so a module that
imported tasks.py found it half-initialised whenever tasks.py was imported first.
"""

from __future__ import annotations

from celery import Task

from .fencing import FINAL_ERRORS


class ReliableTask(Task):
    """Base task with exponential backoff + jitter on retries."""

    autoretry_for = (Exception,)
    dont_autoretry_for = FINAL_ERRORS  # the soft time limit: record failed, do not retry
    max_retries = 3
    retry_backoff = True  # Exponential backoff
    retry_backoff_max = 300  # Max 5 minutes between retries
    retry_jitter = True  # Add randomness to prevent thundering herd
