"""Celery client for the API to dispatch worker tasks.

The API has no Celery app of its own, so `celery.current_app.send_task` targets a
default (non‑Redis) broker and silently no‑ops. This module defines a client that
mirrors the worker's broker + queue/exchange routing (`worker/celery_app.py`) so
`send_task` actually reaches the worker queues (scenario/provision/…).
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from kombu import Exchange, Queue

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery("truenorth-api", broker=REDIS_URL, backend=REDIS_URL)

_exchange = Exchange("truenorth", type="direct")
celery_app.conf.task_queues = (
    Queue("default", _exchange, routing_key="default"),
    Queue("provision", _exchange, routing_key="provision"),
    Queue("destroy", _exchange, routing_key="destroy"),
    Queue("scenario", _exchange, routing_key="scenario"),
    Queue("telemetry", _exchange, routing_key="telemetry"),
)
celery_app.conf.task_default_queue = "default"
celery_app.conf.task_default_exchange = "truenorth"
celery_app.conf.task_default_exchange_type = "direct"
celery_app.conf.task_default_routing_key = "default"
celery_app.conf.task_routes = {
    "worker.tasks.provision_range": {"queue": "provision"},
    "worker.tasks.destroy_range": {"queue": "destroy"},
    "worker.tasks.batch_provision": {"queue": "provision"},
    "worker.tasks.snapshot_range": {"queue": "provision"},
    "worker.tasks.restore_snapshot": {"queue": "provision"},
    "worker.tasks.run_scenario": {"queue": "scenario"},
    "worker.tasks.run_scenario_v2": {"queue": "scenario"},
    "worker.tasks.ingest_telemetry_batch": {"queue": "telemetry"},
    "worker.tasks.generate_aar": {"queue": "default"},
}


def dispatch(task_name: str, *args: Any) -> str | None:
    """Send a worker task via the Redis broker; swallow errors (worker may be down)."""
    try:
        return celery_app.send_task(f"worker.tasks.{task_name}", args=args).id
    except Exception:
        return None
