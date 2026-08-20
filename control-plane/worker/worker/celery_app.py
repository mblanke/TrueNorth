"""TrueNorth Range - Celery application configuration.

Scaled for 70,000 VMs:
  - Multiple queues with priority routing
  - Rate limiting on provisioning to avoid overwhelming Proxmox
  - Late ack + prefetch 1 for fair distribution across workers
  - Separate queues for provision, destroy, scenario, and default tasks
"""

from __future__ import annotations

import os

from celery import Celery
from kombu import Exchange, Queue

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery("truenorth", broker=REDIS_URL, backend=REDIS_URL)

# -- Queue definitions for task isolation --------------------------------
default_exchange = Exchange("truenorth", type="direct")

app.conf.task_queues = (
    Queue("default", default_exchange, routing_key="default"),
    Queue("provision", default_exchange, routing_key="provision"),
    Queue("destroy", default_exchange, routing_key="destroy"),
    Queue("scenario", default_exchange, routing_key="scenario"),
    Queue("telemetry", default_exchange, routing_key="telemetry"),
)
app.conf.task_default_queue = "default"

app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Routing: map task names to queues
    task_routes={
        "worker.tasks.provision_range": {"queue": "provision"},
        "worker.tasks.destroy_range": {"queue": "destroy"},
        "worker.tasks.run_scenario": {"queue": "scenario"},
        "worker.tasks.batch_provision": {"queue": "provision"},
        "worker.tasks.ingest_telemetry_batch": {"queue": "telemetry"},
        "worker.tasks.*": {"queue": "default"},
    },
    # Concurrency control
    task_track_started=True,
    task_acks_late=True,  # Don't ack until task completes
    worker_prefetch_multiplier=1,  # One task at a time per worker thread
    worker_max_tasks_per_child=100,  # Recycle workers to prevent memory leaks
    # Result backend
    result_expires=3600,
    # Rate limiting (applied per worker)
    # Provisioning: max 10 per minute per worker to avoid Proxmox overload
    # With 8 workers: 80 provisions/min = ~4,800/hr
    task_annotations={
        "worker.tasks.provision_range": {"rate_limit": "10/m"},
        "worker.tasks.destroy_range": {"rate_limit": "15/m"},
        "worker.tasks.ingest_telemetry_batch": {"rate_limit": "100/m"},
    },
    # Retry
    broker_connection_retry_on_startup=True,
    broker_connection_retry=False,  # Fail fast on send_task from API if broker is down
    broker_connection_timeout=5,  # 5s max connect attempt
    broker_transport_options={
        "visibility_timeout": 3600,  # 1 hour for long provisions
        "queue_order_strategy": "priority",
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
    },
    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,
)

# -- Beat schedule for periodic tasks -------------------------------------
app.conf.beat_schedule = {
    "cleanup-expired-ranges": {
        "task": "worker.tasks.cleanup_expired_ranges",
        "schedule": 300.0,  # every 5 minutes
    },
    "health-check-ranges": {
        "task": "worker.tasks.health_check_ranges",
        "schedule": 60.0,  # every minute
    },
    "collect-range-metrics": {
        "task": "worker.tasks.collect_range_metrics",
        "schedule": 30.0,  # every 30 seconds
    },
}

# -- Additional routing for new tasks -------------------------------------
app.conf.task_routes.update(
    {
        "worker.tasks.run_scenario_v2": {"queue": "scenario"},
        "worker.tasks.generate_aar": {"queue": "default"},
        "worker.tasks.cleanup_expired_ranges": {"queue": "default"},
        "worker.tasks.snapshot_range": {"queue": "provision"},
        "worker.tasks.restore_snapshot": {"queue": "provision"},
        "worker.tasks.health_check_ranges": {"queue": "default"},
        "worker.tasks.collect_range_metrics": {"queue": "telemetry"},
        "worker.tasks.delete_snapshot": {"queue": "provision"},
        # EPIC 1: Exercise Forge
        "worker.tasks.forge_exercise": {"queue": "default"},
        # EPIC 3: Adaptive Learning
        "worker.tasks.auto_assess_competency": {"queue": "default"},
        "worker.tasks.generate_learning_recommendation": {"queue": "default"},
    }
)

# -- Register task modules -------------------------------------------------
# Importing at the end (after `app` is configured) registers every @app.task
# with this Celery app. Without this the worker starts with an empty task list.
# -- Chaos engineering hooks (disabled by default) -------------------------
# Importing the module registers Celery signals; actual injection is
# controlled by CHAOS_ENABLED env var.
from . import (
    chaos,  # noqa: F401, E402
    tasks,  # noqa: F401, E402
)
