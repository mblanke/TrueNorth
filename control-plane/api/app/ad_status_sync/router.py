"""The AD status sync panel: last runs, counts, deactivated accounts, "run now" (#290).

Administrators only, and only their own tenant: a nightly run covers every tenant, but each
tenant's administrators see only their tenant's counts and accounts. "Run now" looks at the
caller's tenant only, and is audited (``ad_status_sync_run``, with the caller).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser, require_role
from ..db import get_db
from ..models import AuditLog, User, UserRole
from ..tenancy import tenant_uuid
from . import directory, last_seen, service
from .models import AdStatusSyncResult, AdStatusSyncRun
from .schemas import AdStatusSyncOverviewOut, AdStatusSyncResultOut, AdStatusSyncRunIn, AdStatusSyncRunOut

router = APIRouter(prefix="/ad-status-sync", tags=["AD Sync"])

ADMIN = require_role(UserRole.admin)
STALE = timedelta(hours=6)  # a run still "running" after this was cut short (a restart)
RECENT = 20


def directory_reader() -> directory.DirectoryReader:
    return directory.KeycloakDirectoryReader()


def _visible(tid: uuid.UUID):
    return (AdStatusSyncRun.tenant_id.is_(None)) | (AdStatusSyncRun.tenant_id == tid)


def _aware(t: datetime | None) -> datetime | None:
    return None if t is None else (t.replace(tzinfo=UTC) if t.tzinfo is None else t)


def _out(db: Session, run: AdStatusSyncRun, tid: uuid.UUID, with_results: bool) -> AdStatusSyncRunOut:
    status = run.status
    started = _aware(run.started_at)
    if status == "running" and started is not None and datetime.now(UTC) - started > STALE:
        status = "interrupted"
    results = []
    if with_results:
        rows = (
            db.query(AdStatusSyncResult, User)
            .join(User, User.id == AdStatusSyncResult.user_id)
            .filter(AdStatusSyncResult.run_id == run.id, AdStatusSyncResult.tenant_id == tid, User.tenant_id == tid)
            .order_by(User.email)
            .all()
        )
        results = [
            AdStatusSyncResultOut(user_id=r.user_id, email=u.email, display_name=u.display_name or u.email,
                                  reason=r.reason, reason_text=service.REASONS.get(r.reason, r.reason),
                                  applied=r.applied, seats_freed=r.seats_freed)
            for r, u in rows
        ]
    counts = ((run.counts or {}).get("by_tenant") or {}).get(str(tid), {})
    error = run.error
    if status == "completed" and counts.get("held"):
        # Held per tenant: this tenant's own numbers only, never another tenant's.
        status = "held"
        error = service.held_message(int(counts.get("held_would_deactivate", 0)), int(counts.get("held_eligible", 0)))
    return AdStatusSyncRunOut(
        id=run.id, trigger=run.trigger, dry_run=run.dry_run, status=status, started_at=started,
        finished_at=_aware(run.finished_at), error=error,
        counts={k: int(v) for k, v in counts.items()}, results=results,
    )


@router.get("", response_model=AdStatusSyncOverviewOut, operation_id="getAdStatusSync",
            summary="AD status sync: settings, last runs and the accounts they deactivated")
def overview(db: Session = Depends(get_db), user: CurrentUser = Depends(ADMIN)) -> AdStatusSyncOverviewOut:
    """**Role: admin.** The caller's tenant's view of the nightly and manual runs."""
    import os

    tid = tenant_uuid(user)
    cfg = service.settings()
    runs = (
        db.query(AdStatusSyncRun).filter(_visible(tid))
        .order_by(AdStatusSyncRun.started_at.desc()).limit(RECENT).all()
    )
    last_bg = (
        db.query(func.max(AuditLog.timestamp))
        .filter(AuditLog.action == "break_glass_sign_in", AuditLog.tenant_id == tid)
        .scalar()
    )
    return AdStatusSyncOverviewOut(
        enabled=os.getenv("AD_STATUS_SYNC_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"),
        keycloak_configured=directory.configured(), dry_run=cfg.dry_run, nightly_hour_utc=cfg.hour_utc,
        break_glass_usernames=sorted(last_seen.break_glass_usernames()),
        last_break_glass_use=_aware(last_bg),
        latest=_out(db, runs[0], tid, True) if runs else None,
        runs=[_out(db, r, tid, False) for r in runs],
    )


@router.get("/runs/{run_id}", response_model=AdStatusSyncRunOut, operation_id="getAdStatusSyncRun",
            summary="One AD status sync run, with this tenant's accounts")
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(ADMIN)) -> AdStatusSyncRunOut:
    """**Role: admin.** 404 for another tenant's manual run."""
    tid = tenant_uuid(user)
    run = (
        db.query(AdStatusSyncRun)
        .filter(AdStatusSyncRun.id == run_id, AdStatusSyncRun.tenant_id.is_(None) | (AdStatusSyncRun.tenant_id == tid))
        .one_or_none()
    )
    if run is None:
        raise HTTPException(404, "Run not found")
    return _out(db, run, tid, True)


@router.post("/runs", response_model=AdStatusSyncRunOut, status_code=201, operation_id="runAdStatusSync",
             summary="Run the AD status sync now, for this tenant")
def run_now(
    background: BackgroundTasks,
    body: AdStatusSyncRunIn | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(ADMIN),
    reader: directory.DirectoryReader = Depends(directory_reader),
) -> AdStatusSyncRunOut:
    """**Role: admin.** Looks at the caller's tenant only; audited. A dry run records who would be
    deactivated and changes nothing. While ``AD_STATUS_SYNC_DRY_RUN`` is on every run is dry:
    asking for ``dry_run: false`` is 409, ``override_limit`` or not."""
    from ..classes import service as classes
    from ..scheduler import service as scheduler

    body = body or AdStatusSyncRunIn()
    if not reader.ready():
        raise HTTPException(503, "Keycloak's Admin API is not configured (KEYCLOAK_ADMIN_CLIENT_ID and its secret)")
    tid = tenant_uuid(user)
    if service.settings().dry_run and body.dry_run is False:
        raise HTTPException(409, "AD_STATUS_SYNC_DRY_RUN is on: runs record and change nothing. Turn the setting "
                                 "off (installer tn_ad_status_sync_dry_run) to let a run deactivate accounts.")
    dry = service.settings().dry_run if body.dry_run is None else body.dry_run
    outcome = service.run_now(db, reader, tenant_id=tid, actor_id=uuid.UUID(user.id), dry_run=dry,
                              override_limit=body.override_limit)
    background.add_task(classes.send, outcome.mails)
    background.add_task(scheduler.send_invites, outcome.invites)
    db.refresh(outcome.run)
    return _out(db, outcome.run, tid, True)
