"""The nightly AD status sync clock (ADR 0014 decision 6; #290).

Runs in the API beside the scheduler's and the classes' clocks (the same pattern: an
asyncio task from the lifespan, the work in a thread with its own session). Every replica
ticks; the night's run is claimed by inserting its ``nightly:<UTC date>`` slot, a unique
key, so exactly one replica runs it, whatever the number of replicas and whether Redis is
up (a PostgreSQL advisory lock would leak through pgbouncer's transaction pooling,
worker/redis_lock.py). Inside the run each account is claimed with a guarded update, so
even an overlapping "run now" deactivates it once. Off with ``AD_STATUS_SYNC_ENABLED=false``;
idle while Keycloak's Admin API is not configured.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import directory, service

logger = logging.getLogger("truenorth.api.ad_status_sync.clock")

TICK_SECONDS = float(os.getenv("AD_STATUS_SYNC_TICK_SECONDS", "300"))


def _tick_in_own_session() -> service.Outcome | None:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        return service.run_nightly(db, directory.KeycloakDirectoryReader())
    finally:
        db.close()


async def run_forever() -> None:
    """Started from the API lifespan. Sleeps first, so a test client does not tick."""
    logger.info("AD status sync clock started (nightly from %02d:00 UTC, dry run %s)",
                service.settings().hour_utc, service.settings().dry_run)
    from ..classes import service as classes
    from ..scheduler import service as scheduler

    while True:
        await asyncio.sleep(TICK_SECONDS)
        if not directory.configured():
            continue
        try:
            outcome = await asyncio.to_thread(_tick_in_own_session)
            if outcome is not None:
                await classes.send(outcome.mails)
                await scheduler.send_invites(outcome.invites)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AD status sync tick failed; retrying next tick")
