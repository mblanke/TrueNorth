"""The scheduled rediscovery loop, started from the API's lifespan (app/main.py).

Every ``INFRA_DISCOVERY_SECONDS`` (default 300; 0 turns it off) one round runs in a
thread: every active connection that is due is rediscovered under its lease
(``service.run_round``). Several API processes may each run the loop; the per-connection
lease and the due check make one of them do each connection once per interval.
"""

from __future__ import annotations

import asyncio
import logging

from . import service

logger = logging.getLogger(__name__)


def _round() -> dict[str, int]:
    from ..db import SessionLocal

    return service.run_round(SessionLocal)


async def loop() -> None:
    every = service.interval()
    if every <= 0:
        logger.info("Scheduled infrastructure discovery is off (INFRA_DISCOVERY_SECONDS=0)")
        return
    logger.info("Scheduled infrastructure discovery every %ss", every)
    while True:
        await asyncio.sleep(every)
        try:
            counts = await asyncio.to_thread(_round)
            if counts["ok"] or counts["failed"]:
                logger.info("Infrastructure discovery round: %s", counts)
        except Exception:  # noqa: BLE001 — the loop outlives any one round
            logger.exception("Infrastructure discovery round failed")
