"""TrueNorth Range - the DB session and API notification, for task modules split out of tasks.py.

``_db_session`` and ``_notify_api`` still live in ``tasks.py``: tests patch them there
(``patch("worker.tasks._db_session")``) and the worker test database repoints
``tasks.DATABASE_URL``. These wrappers look them up on ``worker.tasks`` at call time,
so a task moved into its own module (exercise_run.py, aar_tasks.py) behaves, and is
patched, exactly as it was. The import is deferred to call time because ``celery_app``
imports every task module at the end of its own import; importing ``tasks`` at load
time from a task module would bring back the cycle base_tasks.py was made to break.

When the plumbing itself moves out of tasks.py, change these two functions and the
tests that patch ``worker.tasks`` together.
"""

from __future__ import annotations


def db_session():
    """One DB session for one task (``tasks._db_session``)."""
    from . import tasks

    return tasks._db_session()


def notify_api(channel: str, message: dict):
    """Push a state change over Redis pub/sub (``tasks._notify_api``)."""
    from . import tasks

    return tasks._notify_api(channel, message)
