"""TrueNorth Range - Celery application configuration.

Scaled for 70,000 VMs:
  - Multiple queues with priority routing
  - Rate limiting on provisioning to avoid overwhelming Proxmox
  - Late ack + prefetch 1 for fair distribution across workers
  - Separate queues for provision, destroy, scenario, and default tasks
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger
from kombu import Exchange, Queue

from .contracts import QUEUES, route_table
from .fencing import SOFT_TIME_LIMIT, TASK_TIME_LIMIT

# The broker redelivers an unacked task after this long (acks_late). Every hard time limit
# must be below it, or a task still running is delivered a second time alongside itself.
VISIBILITY_TIMEOUT = 3600

RANGE_TASK_LIMITS = {"soft_time_limit": SOFT_TIME_LIMIT, "time_limit": TASK_TIME_LIMIT}


def _env_seconds(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


# Every other task (task_annotations below has the long ones): a hung AI call, AAR render
# or inject no longer holds a worker slot for ever. The soft limit raises
# SoftTimeLimitExceeded in the task (final: ReliableTask does not retry it); the hard limit
# kills the child process. Worker containers get a stop_grace_period above the longest hard
# limit (compose.prod.yml), so a deploy's warm shutdown lets running tasks finish.
DEFAULT_SOFT_TIME_LIMIT = _env_seconds("CELERY_TASK_SOFT_TIME_LIMIT", 1800)
DEFAULT_TIME_LIMIT = _env_seconds("CELERY_TASK_TIME_LIMIT", 1900)
for _soft, _hard in ((DEFAULT_SOFT_TIME_LIMIT, DEFAULT_TIME_LIMIT), (SOFT_TIME_LIMIT, TASK_TIME_LIMIT)):
    if not 0 < _soft < _hard < VISIBILITY_TIMEOUT:
        raise ValueError(
            f"Celery time limits must satisfy 0 < soft ({_soft}) < hard ({_hard}) < "
            f"visibility timeout ({VISIBILITY_TIMEOUT}); check CELERY_TASK_SOFT_TIME_LIMIT / CELERY_TASK_TIME_LIMIT"
        )

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery("truenorth", broker=REDIS_URL, backend=REDIS_URL)

# -- Queue definitions for task isolation --------------------------------
default_exchange = Exchange("truenorth", type="direct")

app.conf.task_queues = tuple(Queue(q, default_exchange, routing_key=q) for q in QUEUES)
app.conf.task_default_queue = "default"

app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Routing: one table, shared with the API's sender (contracts.py). The API used to
    # keep its own copy, and the two disagreed on delete_snapshot.
    task_routes={**route_table(), "worker.tasks.*": {"queue": "default"}},
    # Concurrency control
    task_track_started=True,
    task_acks_late=True,  # Don't ack until task completes
    worker_prefetch_multiplier=1,  # One task at a time per worker thread
    worker_max_tasks_per_child=100,  # Recycle workers to prevent memory leaks
    task_soft_time_limit=DEFAULT_SOFT_TIME_LIMIT,
    task_time_limit=DEFAULT_TIME_LIMIT,
    # Result backend
    result_expires=3600,
    # Rate limiting (applied per worker)
    # Provisioning: max 10 per minute per worker to avoid Proxmox overload
    # With 8 workers: 80 provisions/min = ~4,800/hr
    # Range tasks (worker/fencing.py): the soft limit is raised inside the task (it records
    # `failed` and keeps the range's lease); the hard limit kills the process, below the
    # broker's visibility timeout, so a running task is never redelivered alongside itself.
    task_annotations={
        "worker.tasks.provision_range": {"rate_limit": "10/m", **RANGE_TASK_LIMITS},
        "worker.tasks.destroy_range": {"rate_limit": "15/m", **RANGE_TASK_LIMITS},
        "worker.tasks.stop_range": RANGE_TASK_LIMITS,
        "worker.tasks.start_range": RANGE_TASK_LIMITS,
        "worker.tasks.snapshot_range": RANGE_TASK_LIMITS,
        "worker.tasks.restore_snapshot": RANGE_TASK_LIMITS,
        # Hypervisor or Ansible work that may legitimately run past the default limit
        # (the noise playbook's own timeout, NOISE_DEPLOY_TIMEOUT, is 1800 s).
        "worker.tasks.delete_snapshot": RANGE_TASK_LIMITS,
        "worker.tasks.deploy_noise_agents": RANGE_TASK_LIMITS,
        "worker.tasks.reconcile_lab_vms": RANGE_TASK_LIMITS,
        "worker.tasks.ingest_telemetry_batch": {"rate_limit": "100/m"},
    },
    # Retry
    broker_connection_retry_on_startup=True,
    broker_connection_retry=False,  # Fail fast on send_task from API if broker is down
    broker_connection_timeout=5,  # 5s max connect attempt
    broker_transport_options={
        "visibility_timeout": VISIBILITY_TIMEOUT,  # 1 hour for long provisions
        "queue_order_strategy": "priority",
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
    },
    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,
)

# -- Beat schedule for periodic tasks -------------------------------------
# Sent by the single `beat` service in compose.prod.yml (never run two: each would send
# every entry). `expires`: a run nobody picked up before the next one is due is dropped,
# so a stopped worker does not come back to a broker full of stale health checks.
app.conf.beat_schedule = {
    "health-check-ranges": {
        "task": "worker.tasks.health_check_ranges",
        "schedule": 60.0,  # every minute
        "options": {"expires": 60},
    },
    "collect-range-metrics": {
        "task": "worker.tasks.collect_range_metrics",
        "schedule": 30.0,  # every 30 seconds
        "options": {"expires": 30},
    },
}


# -- LOG_FORMAT=json: one JSON object per line (as control-plane/api/app/log_format.py) --
class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("task_id", "task_name"):  # set on records from Celery's task logger
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


@after_setup_logger.connect
@after_setup_task_logger.connect
def _json_logs(logger: logging.Logger, **_kwargs) -> None:
    if os.getenv("LOG_FORMAT", "").strip().lower() == "json":
        for handler in logger.handlers:
            handler.setFormatter(JsonLogFormatter())

# -- Register task modules -------------------------------------------------
# Importing at the end (after `app` is configured) registers every @app.task
# with this Celery app. Without this the worker starts with an empty task list.
# -- Chaos engineering hooks (disabled by default) -------------------------
# Importing the module registers Celery signals; actual injection is
# controlled by CHAOS_ENABLED env var.
from . import (
    aar_tasks,  # noqa: F401, E402
    chaos,  # noqa: F401, E402
    exercise_run,  # noqa: F401, E402
    lab_tasks,  # noqa: F401, E402
    noise_tasks,  # noqa: F401, E402
    power_tasks,  # noqa: F401, E402
    tasks,  # noqa: F401, E402
    telemetry_tasks,  # noqa: F401, E402
)
