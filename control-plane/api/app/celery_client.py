"""Celery client for the API to dispatch worker tasks.

The API has no Celery app of its own, so `celery.current_app.send_task` targets a
default (non‑Redis) broker and silently no‑ops. This module defines a client on the
worker's broker, with queues and routes taken from the shared task contract
(`task_contracts.py`, generated from `worker/worker/contracts.py`).

Every API -> worker call goes through `dispatch`. Never import the `worker` package
here: it is not in the API image.
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from kombu import Exchange, Queue

from .task_contracts import QUEUES, TaskContractError, route_table, validate_args

__all__ = ["TaskContractError", "celery_app", "dispatch"]

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery("truenorth-api", broker=REDIS_URL, backend=REDIS_URL)

_exchange = Exchange("truenorth", type="direct")
celery_app.conf.task_queues = tuple(Queue(q, _exchange, routing_key=q) for q in QUEUES)
celery_app.conf.task_default_queue = "default"
celery_app.conf.task_default_exchange = "truenorth"
celery_app.conf.task_default_exchange_type = "direct"
celery_app.conf.task_default_routing_key = "default"
celery_app.conf.task_routes = route_table()


def dispatch(task_name: str, *args: Any) -> str | None:
    """Send a contracted worker task. Returns the task id, or None if the broker is down.

    A call that does not match the contract raises TaskContractError: that is a bug in
    the caller, not an outage, and must not be swallowed with the broker errors.
    """
    contract = validate_args(task_name, args)
    try:
        return celery_app.send_task(contract.qualified_name, args=list(args)).id
    except Exception:
        return None
