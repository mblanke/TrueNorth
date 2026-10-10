"""TrueNorth Range - the post-deploy configure stage (docs/greyspace-plan.md, slice 0).

``configure_range`` runs after ``provision_range`` has built a range and marked it ready,
for what can only be set up once its VMs are running. Its one configurator today is the
range's Greyspace (worker/greyspace.py ``configure``: gs-core's corpus mount, stack start
and health check through the backend's guest channel); ``after_provision`` queues it only
for ranges that have one on a backend that builds gs-core.

Not retried automatically: a configure step that failed is recorded with its reason on
the range's Greyspace (``failed``), and repeating it blindly would not change that.
A separate module, not tasks.py (ADR 0003: tasks.py's line count is ratcheted).
"""

from __future__ import annotations

import logging

from . import greyspace
from .celery_app import app

logger = logging.getLogger("truenorth.worker.configure")

# Configurators, in order: each takes the range id and returns a short outcome.
CONFIGURATORS = (("greyspace", greyspace.configure),)


@app.task(bind=True, name="worker.tasks.configure_range")
def configure_range(self, range_id: str) -> dict:
    """Run every configurator for a built range; never raises (each records its own outcome)."""
    outcomes = {}
    for name, configurator in CONFIGURATORS:
        try:
            outcomes[name] = configurator(range_id)
        except Exception:  # noqa: BLE001 — one configurator must not stop the others
            logger.exception("[configure] range %s: %s failed", range_id, name)
            outcomes[name] = "failed"
    logger.info("[configure] range %s: %s", range_id, outcomes)
    return {"range_id": range_id, **outcomes}
