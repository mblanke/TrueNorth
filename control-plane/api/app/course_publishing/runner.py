"""Run publications outside a request: after a request returns, and on API start for
jobs a previous process left mid-way. Each run gets its own session."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def run_by_id(publication_id: uuid.UUID) -> None:
    from ..db import SessionLocal
    from . import service
    from .models import CoursePublication

    db = SessionLocal()
    try:
        pub = db.get(CoursePublication, publication_id)
        if pub is not None:
            service.run(db, pub)
    except service.PublishRefusedError as exc:
        logger.info("publication %s not run: %s", publication_id, exc)
    except Exception:  # noqa: BLE001 — a background job must record, never crash the server
        logger.exception("publication %s crashed", publication_id)
    finally:
        db.close()


def resume_on_start() -> None:
    """Resume publications whose process died (state running, lease lapsed)."""
    from ..db import SessionLocal
    from . import service

    db = SessionLocal()
    try:
        resumed = service.resume_stalled(db)
        if resumed:
            logger.info("resumed %d stalled course publication(s)", resumed)
    except Exception:  # noqa: BLE001 — never block startup on a Moodle that is down
        logger.exception("could not resume stalled course publications")
    finally:
        db.close()
