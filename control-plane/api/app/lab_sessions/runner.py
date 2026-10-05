"""The lab sweep: advance every session that holds resources, once a minute.

Readiness, baselines, resets and teardown complete in worker tasks; expiry happens with
no request at all. The sweep is what notices both. Each pass takes a lease on the sweep
itself (one API process sweeps at a time) and gives every session its own transaction.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)
INTERVAL = float(os.getenv("LAB_SWEEP_SECONDS", "60"))


def sweep_once() -> int:
    from ..db import SessionLocal
    from . import service

    db = SessionLocal()
    try:
        return service.sweep(db)
    except Exception:  # noqa: BLE001 — a failed pass is logged; the next one tries again
        logger.exception("lab sweep failed")
        return 0
    finally:
        db.close()


async def loop() -> None:
    while True:
        await asyncio.sleep(INTERVAL)
        await asyncio.get_running_loop().run_in_executor(None, sweep_once)
