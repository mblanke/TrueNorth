"""Pull results from every active Moodle on a schedule, in the API (as the lab sweep and the
exercise clock run): the Moodle seam, the signing key and the learning records it writes
are all the API's, and the worker image holds none of them (ADR 0001, ADR 0003).

``MOODLE_RESULTS_PULL_SECONDS`` sets the interval (default 600; 0 turns it off). Several
API processes may run the loop: each Moodle's cursor is claimed by one at a time, and a
process that finds it claimed skips that Moodle until the next round.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def interval() -> float:
    try:
        return float(os.getenv("MOODLE_RESULTS_PULL_SECONDS", "600"))
    except ValueError:
        return 600.0


def pull_all() -> int:
    """One round over every active Moodle that can report results. Returns how many pulled."""
    from ..db import SessionLocal
    from ..models import ExternalPlatform
    from ..moodle_backends import MoodleError, supported_moodle_types
    from . import service

    db = SessionLocal()
    pulled = 0
    try:
        platforms = (
            db.query(ExternalPlatform)
            .filter(
                ExternalPlatform.is_active.is_(True),
                ExternalPlatform.platform_type.in_(supported_moodle_types()),
                ExternalPlatform.lti_issuer.isnot(None),
            )
            .all()
        )
        for platform in platforms:
            try:
                service.pull(db, platform)
                pulled += 1
            except (service.PullBusyError, service.LeaseLostError, MoodleError) as exc:
                logger.info("results pull of platform %s skipped: %s", platform.id, exc)
            except Exception:  # noqa: BLE001 — one Moodle must not stop the others
                logger.exception("results pull of platform %s crashed", platform.id)
                db.rollback()
    finally:
        db.close()
    return pulled


async def loop() -> None:
    every = interval()
    if every <= 0:
        return
    while True:
        await asyncio.sleep(every)
        try:
            await asyncio.to_thread(pull_all)
        except Exception:  # noqa: BLE001 — the loop outlives any one round
            logger.exception("Moodle results round failed")
