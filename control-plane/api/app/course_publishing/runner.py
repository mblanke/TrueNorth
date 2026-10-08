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
        # tenant-safe: an id this process queued after creating it (service.request).
        pub = db.get(CoursePublication, publication_id)
        if pub is not None:
            service.run(db, pub)
    except service.PublishRefusedError as exc:
        logger.info("publication %s not run: %s", publication_id, exc)
    except service.LeaseLostError as exc:  # its Moodle writes may have landed: say so
        logger.warning("publication %s lost its lease mid-run: %s", publication_id, exc)
    except Exception:  # noqa: BLE001 — a background job must record, never crash the server
        logger.exception("publication %s crashed", publication_id)
    finally:
        db.close()
    run_waiting(publication_id)


def run_waiting(publication_id: uuid.UUID) -> None:
    """After a run ends, run the publication of the same course and Moodle that waited for
    it (claim() lets one run per course and Moodle at a time)."""
    from ..db import SessionLocal
    from . import service
    from .models import CoursePublication

    for _ in range(10):  # each pass publishes or supersedes one waiting job
        db = SessionLocal()
        try:
            # tenant-safe: the publication that just ran, an id this process queued.
            done = db.get(CoursePublication, publication_id)
            nxt = service.waiting(db, done.course_id, done.platform_id) if done is not None else None
            if nxt is None:
                return
            publication_id = nxt.id
            service.run(db, nxt)
        except (service.PublishRefusedError, service.LeaseLostError) as exc:
            logger.info("waiting publication %s not run: %s", publication_id, exc)
            return
        except Exception:  # noqa: BLE001
            logger.exception("waiting publication %s crashed", publication_id)
            return
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
