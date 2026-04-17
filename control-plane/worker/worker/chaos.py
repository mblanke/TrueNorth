"""TrueNorth Range — Chaos engineering hooks for resilience testing.

Provides configurable failure injection for Celery tasks:
- Random task failure injection (configurable probability)
- Artificial latency injection
- Task timeout simulation
- Circuit breaker verification

Configuration (environment variables):
    CHAOS_ENABLED           Enable chaos hooks (default: false)
    CHAOS_FAILURE_RATE      Probability of failure injection 0.0-1.0 (default: 0.0)
    CHAOS_LATENCY_MS        Max artificial latency in ms (default: 0)
    CHAOS_AFFECTED_TASKS    Comma-separated task names to target (default: all)
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

from celery import Task
from celery.signals import task_postrun, task_prerun

logger = logging.getLogger("truenorth.chaos")

CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
CHAOS_FAILURE_RATE = float(os.getenv("CHAOS_FAILURE_RATE", "0.0"))
CHAOS_LATENCY_MS = int(os.getenv("CHAOS_LATENCY_MS", "0"))
CHAOS_AFFECTED_TASKS: set[str] | None = None

_raw_tasks = os.getenv("CHAOS_AFFECTED_TASKS", "")
if _raw_tasks:
    CHAOS_AFFECTED_TASKS = {t.strip() for t in _raw_tasks.split(",") if t.strip()}


class ChaosError(RuntimeError):
    """Injected failure from chaos engineering hooks."""

    pass


def _should_apply(task_name: str) -> bool:
    """Check if chaos should apply to this task."""
    if not CHAOS_ENABLED:
        return False
    return CHAOS_AFFECTED_TASKS is None or task_name in CHAOS_AFFECTED_TASKS


@task_prerun.connect
def chaos_prerun_hook(
    sender: Task | None = None,
    task_id: str | None = None,
    task: Task | None = None,
    args: Any = None,
    kwargs: Any = None,
    **kw: Any,
) -> None:
    """Inject failures/latency before task execution."""
    if sender is None:
        return
    task_name = sender.name or ""
    if not _should_apply(task_name):
        return

    # Inject latency
    if CHAOS_LATENCY_MS > 0:
        delay_ms = random.randint(0, CHAOS_LATENCY_MS)
        if delay_ms > 0:
            logger.warning(
                "[CHAOS] Injecting %dms latency into task %s (%s)",
                delay_ms,
                task_name,
                task_id,
            )
            time.sleep(delay_ms / 1000.0)

    # Inject failure
    if CHAOS_FAILURE_RATE > 0 and random.random() < CHAOS_FAILURE_RATE:
        logger.warning(
            "[CHAOS] Injecting failure into task %s (%s)",
            task_name,
            task_id,
        )
        raise ChaosError(f"Chaos failure injected for task {task_name} (failure_rate={CHAOS_FAILURE_RATE})")


@task_postrun.connect
def chaos_postrun_hook(
    sender: Task | None = None,
    task_id: str | None = None,
    retval: Any = None,
    state: str | None = None,
    **kw: Any,
) -> None:
    """Log chaos-affected task completion for observability."""
    if sender is None or not CHAOS_ENABLED:
        return
    task_name = sender.name or ""
    if _should_apply(task_name):
        logger.info(
            "[CHAOS] Task %s (%s) completed with state=%s",
            task_name,
            task_id,
            state,
        )


def get_chaos_status() -> dict[str, Any]:
    """Return current chaos configuration for the health endpoint."""
    return {
        "enabled": CHAOS_ENABLED,
        "failure_rate": CHAOS_FAILURE_RATE,
        "latency_ms": CHAOS_LATENCY_MS,
        "affected_tasks": list(CHAOS_AFFECTED_TASKS) if CHAOS_AFFECTED_TASKS else "all",
    }
