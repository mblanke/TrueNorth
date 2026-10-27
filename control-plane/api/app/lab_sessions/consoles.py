"""Open consoles of idle-due labs, asked in one batch before the sweep takes any lock
(ADR 0014 §18; #311 round 3).

Console input goes straight to the hypervisor (WebMKS), never through the lab page, so before
an idle close the sweep asks the console adapter of each lab's backend how many console
connections its VMs have open. The answers are data for ``service.advance``:

- a count: more than none is the Student working (the idle clock restarts), none closes it;
- None (unknown: no answer in time, or a vSphere-family backend with no adapter): the lab is
  kept and asked about again later, never past its maximum lifetime;
- a backend of a family with no console concept answers 0: idle on heartbeats alone.

Each adapter reads what it needs from the database here, in the sweep's thread; the
hypervisor calls run on worker threads under ``LAB_CONSOLE_BATCH_SECONDS`` in all, so a
vCenter that does not answer delays no lab's advance beyond that, and every lab of a backend
that did not answer in time is unknown.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Range
from .models import ACTIVE, READY, LabSession

logger = logging.getLogger(__name__)


def _batch_seconds() -> float:
    try:
        return float(os.getenv("LAB_CONSOLE_BATCH_SECONDS", "30"))
    except ValueError:
        return 30.0


def idle_due(db: Session, now: datetime) -> list[LabSession]:
    """Ready or active labs whose idle time has run out before their maximum lifetime."""
    return (
        db.query(LabSession)
        .filter(
            LabSession.state.in_((READY, ACTIVE)),
            LabSession.idle_expires_at <= now,
            LabSession.max_expires_at > now,
            LabSession.range_id.isnot(None),
        )
        .all()
    )


def ask(db: Session, sessions: list[LabSession]) -> dict[uuid.UUID, int | None]:
    """Session id -> open console connections on its VMs (None: unknown)."""
    from ..console_backends import ConsoleTarget, console_backend_for, expects_console

    out: dict[uuid.UUID, int | None] = {}
    by_backend: dict[str, list[ConsoleTarget]] = {}
    for s in sessions:
        # tenant-safe: the session's own range, created by app/lab_sessions in its tenant.
        rng = db.get(Range, s.range_id)
        if rng is None:
            out[s.id] = None
            continue
        try:
            vms = [
                {"vm_id": str(vm.get("vm_id") or ""), "name": str(vm.get("name") or "")}
                for vm in json.loads(rng.provisioner_output or "{}").get("vms") or []
                if isinstance(vm, dict)
            ]
        except ValueError:
            vms = []
        # Plain copies for the readers' threads; the Range itself only for preparing them here.
        by_backend.setdefault(s.backend, []).append(ConsoleTarget(s.id, str(rng.id), vms, rng=rng))
    readers = []
    for backend, targets in by_backend.items():
        adapter = console_backend_for(backend)
        if adapter is None:
            # No adapter: unknown on a console-capable family, heartbeats alone otherwise.
            out.update(dict.fromkeys((t.key for t in targets), None if expects_console(backend) else 0))
            continue
        out.update(dict.fromkeys(t.key for t in targets))  # unknown until it answers
        try:
            readers.append((backend, adapter.console_reader(db, targets)))
        except Exception:  # noqa: BLE001 — unknown, never "no consoles"
            logger.warning("console check: %s could not prepare", backend, exc_info=True)
    if not readers:
        return out
    answers: queue.Queue = queue.Queue()

    def run(backend: str, read) -> None:
        try:
            answers.put((backend, read()))
        except Exception as exc:  # noqa: BLE001 — unknown for its labs
            logger.warning("console check: %s failed: %s", backend, exc)
            answers.put((backend, {}))

    for backend, read in readers:  # daemon threads: a hung one never blocks shutdown
        threading.Thread(target=run, args=(backend, read), name="tn-lab-console", daemon=True).start()
    deadline = time.monotonic() + _batch_seconds()
    for _ in readers:
        try:
            backend, answer = answers.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            logger.warning("console check: a backend did not answer in %ss; its labs are kept", _batch_seconds())
            break
        out.update({k: v for k, v in answer.items() if k in out})
    return out
