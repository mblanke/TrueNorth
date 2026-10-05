"""Lab-session lifecycle.

    launch     key (tenant, user, release, activity, attempt) -> the live session, or a new
               one: quota and capacity reserved, isolated networks leased, the lab profile
               rendered into a Range, provision_range dispatched
    advance    one step of whatever the session is waiting on, from the Range's state:
               provisioning -> probes pass -> baselining (snapshot) -> ready; expiry;
               cleaning -> destroyed (networks returned, leftovers reconciled)
    touch      the student is here: idle lease extended, ready -> active
    reset      snapshot restore (profile reset: snapshot) or a rebuild (reset: rebuild)
    end        evidence kept, VMs destroyed

Every step reads the current state and moves it forward at most once, so the runner, a
page poll and a retried request can all call ``advance`` without doubling anything.
Worker tasks are the existing range tasks; this module writes no hypervisor state itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..course_releases import lab_profile as profile_rules
from ..course_releases.models import ACCEPTED, SUPERSEDED, CourseRelease
from ..course_releases.service import load_bundle
from ..models import GoldenImage, Range, RangeSnapshot, RangeState, Template
from . import probes
from .models import (
    ACTIVE,
    BASELINING,
    CLEANING,
    COMPLETED,
    DESTROYED,
    ENDING,
    EXPIRED,
    FAILED,
    HOLDS_RESOURCES,
    LIVE,
    PROVISIONING,
    QUEUED,
    READY,
    RECONCILE,
    RESETTING,
    TERMINAL,
    LabNetworkLease,
    LabSession,
)

logger = logging.getLogger(__name__)
BASELINE = "lab-baseline"


class LabRefusedError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for timezone columns; they were written as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def backend() -> str:
    return os.getenv("PROVISIONER_BACKEND", "mock")


def _dispatch(task: str, *args: Any) -> str | None:
    from ..celery_client import dispatch

    return dispatch(task, *args)


# ── the profile a session runs ───────────────────────────────────────


def release_profile(db: Session, release: CourseRelease, activity_id: str) -> dict[str, Any]:
    acts = json.loads(release.meta).get("activities") or {}
    if acts.get(activity_id) != "range":
        raise LabRefusedError(f"{activity_id} is not a range activity of this release", 404)
    bundle = load_bundle(db, release)
    if not bundle.lab_profile or activity_id not in bundle.lab_profile.get("module_ids", []):
        raise LabRefusedError(f"this release has no lab profile for {activity_id}", 404)
    return bundle.lab_profile


def _catalogue(db: Session, hypervisor: str) -> tuple[set[str], set[str]]:
    rows = db.query(GoldenImage).filter(GoldenImage.hypervisor == hypervisor, GoldenImage.deleted_at.is_(None)).all()
    return {r.catalogue_id for r in rows if r.enabled}, {r.catalogue_id for r in rows if not r.enabled}


def check_profile(db: Session, profile: dict[str, Any], activity_id: str) -> None:
    """The same rules ARC² applied, now against this platform's image catalogue."""
    hypervisor = "proxmox" if "proxmox" in backend() else "vsphere"
    problems = profile_rules.findings(
        profile, range_modules=set(profile["module_ids"]), catalogue=_catalogue(db, hypervisor)
    )
    if problems:
        raise LabRefusedError("this lab cannot be built here: " + "; ".join(m for _, m in problems), 422)


def profile_digest(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def range_template(profile: dict[str, Any], port_groups: dict[str, str], name: str) -> dict[str, Any]:
    """The lab profile as a range template ``render_topology`` reads (nodes on vlans)."""
    for node in profile["nodes"]:
        if len(node["networks"]) != 1:
            raise LabRefusedError(
                f"node {node['name']} has {len(node['networks'])} networks; one per node is supported", 422
            )
    return {
        "name": name,
        "network": {
            "vlans": [
                {
                    "name": n["name"],
                    "cidr": n.get("cidr") or "10.250.0.0/24",
                    "port_group": port_groups.get(n["name"], ""),
                }
                for n in profile["networks"]
            ]
        },
        "nodes": [
            {
                "id": n["name"],
                "role": n.get("role", "lab"),
                "os": n["catalogue_id"],
                "vlan": n["networks"][0],
                "specs": {"cores": n["vcpu"], "memory_mb": n["ram_mb"], "disk_gb": n["disk_gb"]},
            }
            for n in profile["nodes"]
        ],
    }


# ── capacity ──────────────────────────────────────────────────────────


def _pool(db: Session) -> list[str]:
    """Sync the configured port-group pool into lease rows; returns the pool."""
    names = [p.strip() for p in os.getenv("LAB_PORT_GROUPS", "").split(",") if p.strip()]
    known = {row.port_group for row in db.query(LabNetworkLease).all()}
    for name in names:
        if name not in known:
            db.add(LabNetworkLease(port_group=name))
    db.flush()
    return names


def _lease_networks(db: Session, session: LabSession, profile: dict[str, Any]) -> dict[str, str] | None:
    """One free isolated port group per profile network, or None if the pool is short.
    The mock backend needs no real networks and leases nothing."""
    if backend() == "mock":
        return {}
    pool = _pool(db)
    if not pool:
        raise LabRefusedError("no lab networks are configured (LAB_PORT_GROUPS)", 503)
    wanted = [n["name"] for n in profile["networks"]]
    free = (
        db.query(LabNetworkLease)
        .filter(LabNetworkLease.session_id.is_(None), LabNetworkLease.port_group.in_(pool))
        .order_by(LabNetworkLease.port_group)
        .limit(len(wanted))
        .with_for_update(skip_locked=True)
        .all()
    )
    if len(free) < len(wanted):
        return None
    out = {}
    for lease, net in zip(free, wanted, strict=True):
        lease.session_id, lease.network_name, lease.leased_at = session.id, net, _now()
        out[net] = lease.port_group
    db.flush()
    return out


def _release_networks(db: Session, session: LabSession) -> None:
    for lease in db.query(LabNetworkLease).filter(LabNetworkLease.session_id == session.id).all():
        lease.session_id, lease.network_name, lease.leased_at = None, "", None


def _quota_problem(db: Session, session: LabSession) -> str | None:
    holding = db.query(LabSession).filter(LabSession.state.in_(HOLDS_RESOURCES), LabSession.id != session.id)
    mine = holding.filter(LabSession.user_id == session.user_id).count()
    if mine >= _int_env("LAB_MAX_SESSIONS_PER_USER", 1):
        return "you already have a lab running; end it before starting another"
    tenant = holding.filter(LabSession.tenant_id == session.tenant_id)
    if tenant.count() >= _int_env("LAB_MAX_SESSIONS_PER_TENANT", 20):
        return "all lab places are in use; this lab starts when one is free"
    vcpu = tenant.with_entities(func.coalesce(func.sum(LabSession.vcpu), 0)).scalar() or 0
    if vcpu + session.vcpu > _int_env("LAB_MAX_VCPU_PER_TENANT", 64):
        return "the lab capacity for your organisation is in use; this lab starts when some is free"
    return None


# ── launch ────────────────────────────────────────────────────────────


def release_for_student(
    db: Session, *, tenant_id: uuid.UUID, user_id: uuid.UUID, course_id: uuid.UUID
) -> CourseRelease:
    """The release a student's lab comes from: the one their enrollment is pinned to (the
    release they started on), else the course's accepted release. A Moodle lab link names
    the course, so publishing a new release never moves a student's lab under them."""
    from ..course_releases.service import active_release, pin_enrollment
    from ..enrollment import ensure_enrollment
    from ..models import Course

    course = db.query(Course).filter(Course.id == course_id, Course.tenant_id == tenant_id).one_or_none()
    if course is None:
        raise LabRefusedError("course not found", 404)
    enrollment = ensure_enrollment(db, user_id=user_id, course_id=course_id, tenant_id=tenant_id)
    pin = pin_enrollment(db, enrollment)
    release = db.get(CourseRelease, pin.release_id) if pin else active_release(db, course_id)
    if release is None:
        raise LabRefusedError("this course has no accepted release", 404)
    return release


def launch(
    db: Session, *, tenant_id: uuid.UUID, user_id: uuid.UUID, release_id: uuid.UUID, activity_id: str
) -> tuple[LabSession, bool]:
    """The student's session for this activity: the live one if there is one, else a new
    attempt. Never a second range for the same attempt."""
    release = (
        db.query(CourseRelease)
        .filter(CourseRelease.id == release_id, CourseRelease.tenant_id == tenant_id)
        .one_or_none()
    )
    if release is None:
        raise LabRefusedError("release not found", 404)
    if release.state not in (ACCEPTED, SUPERSEDED):
        raise LabRefusedError("this release has not been accepted")
    key = (
        LabSession.tenant_id == tenant_id,
        LabSession.user_id == user_id,
        LabSession.release_id == release_id,
        LabSession.activity_id == activity_id,
    )
    latest = db.query(LabSession).filter(*key).order_by(LabSession.attempt.desc()).first()
    if latest is not None and latest.state not in TERMINAL:
        return latest, False

    profile = release_profile(db, release, activity_id)
    check_profile(db, profile, activity_id)
    session = LabSession(
        tenant_id=tenant_id,
        user_id=user_id,
        release_id=release_id,
        activity_id=activity_id,
        attempt=(latest.attempt + 1) if latest else 1,
        state=QUEUED,
        profile_id=profile["id"],
        profile_digest=profile_digest(profile),
        backend=backend(),
        vcpu=sum(n["vcpu"] for n in profile["nodes"]),
        ram_mb=sum(n["ram_mb"] for n in profile["nodes"]),
    )
    db.add(session)
    db.flush()
    _start(db, session, profile)
    db.flush()
    return session, True


def _start(db: Session, session: LabSession, profile: dict[str, Any]) -> None:
    """queued -> provisioning when there is room; stays queued (with why) otherwise."""
    problem = _quota_problem(db, session)
    if problem:
        session.error = problem
        return
    networks = _lease_networks(db, session, profile)
    if networks is None:
        session.error = "all isolated lab networks are in use; this lab starts when one is free"
        return
    name = f"lab-{profile['id']}-{str(session.id)[:8]}"
    template = Template(
        name=name,
        version=str(profile["version"]),
        yaml=yaml.safe_dump(range_template(profile, networks, name), sort_keys=False),
        tenant_id=session.tenant_id,
        is_public=False,
    )
    db.add(template)
    db.flush()
    rng = Range(
        name=name,
        template_id=template.id,
        tenant_id=session.tenant_id,
        provisioner_backend=session.backend,
        state=RangeState.provisioning,
        description=f"Lab session {session.id} ({profile['id']} v{profile['version']}); managed by app.lab_sessions.",
    )
    db.add(rng)
    db.flush()
    lifetime = profile["lifetime"]
    now = _now()
    session.range_id = rng.id
    session.state = PROVISIONING
    session.error = ""
    session.provisioning_at = now
    session.max_expires_at = now + timedelta(minutes=lifetime["max_minutes"])
    session.idle_expires_at = now + timedelta(minutes=lifetime["idle_minutes"])
    db.flush()
    if _dispatch("provision_range", str(rng.id)) is None:
        # Broker down: the Range is waiting in `provisioning`; the runner re-sends.
        session.error = "the provisioning service did not take the request; retrying"


# ── advance ───────────────────────────────────────────────────────────


def _profile(db: Session, session: LabSession) -> dict[str, Any]:
    return release_profile(db, db.get(CourseRelease, session.release_id), session.activity_id)


def _vms(rng: Range | None) -> list[dict[str, Any]]:
    try:
        return list(json.loads(rng.provisioner_output or "{}").get("vms") or []) if rng else []
    except ValueError:
        return []


def advance(db: Session, session: LabSession) -> LabSession:
    now = _now()
    rng = db.get(Range, session.range_id) if session.range_id else None
    state = session.state

    if state == QUEUED:
        _start(db, session, _profile(db, session))
    elif state == PROVISIONING:
        _advance_provisioning(db, session, rng, now)
    elif state == BASELINING:
        snap = db.get(RangeSnapshot, session.baseline_snapshot_id) if session.baseline_snapshot_id else None
        if snap is not None and snap.snapshot_state == "ready":
            session.state = READY
        elif snap is None or snap.snapshot_state == "failed":
            _fail(db, session, "the reset point (baseline snapshot) could not be taken")
    elif state == RESETTING:
        snap = db.get(RangeSnapshot, session.baseline_snapshot_id) if session.baseline_snapshot_id else None
        if snap is not None and snap.snapshot_state == "ready":
            session.state = ACTIVE
        elif snap is not None and snap.snapshot_state == "failed":
            _fail(db, session, "reset failed")
    elif state in (READY, ACTIVE):
        if now >= _aware(session.max_expires_at) or now >= _aware(session.idle_expires_at):
            end(db, session, reason="expired")
    elif state in (COMPLETED, EXPIRED, CLEANING, RECONCILE):
        _advance_cleaning(db, session, rng)
    db.flush()
    return session


def _advance_provisioning(db: Session, session: LabSession, rng: Range | None, now: datetime) -> None:
    if rng is None:
        return _fail(db, session, "the lab's range record is missing")
    if rng.state == RangeState.failed:
        return _fail(db, session, f"the lab could not be built: {rng.error_message or 'provisioning failed'}")
    timeout = timedelta(seconds=_int_env("LAB_PROVISION_TIMEOUT", 1800))
    if now - _aware(session.provisioning_at or now) > timeout:
        return _fail(db, session, f"the lab was not ready within {int(timeout.total_seconds() // 60)} minutes")
    if rng.state != RangeState.ready:
        if session.error.startswith("the provisioning service") and _dispatch("provision_range", str(rng.id)):
            session.error = ""
        return None
    results = probes.run(_profile(db, session)["health_checks"], _vms(rng))
    session.probes = json.dumps(results)
    if not all(r["ok"] for r in results):
        return None  # built but not usable yet: keep probing until the timeout
    session.readiness_seconds = (now - _aware(session.provisioning_at)).total_seconds()
    session.ready_at = now
    if _profile(db, session)["reset"]["mode"] == "snapshot":
        snap = RangeSnapshot(
            range_id=rng.id,
            name=BASELINE,
            description="Lab reset point, taken when the lab first became ready",
            snapshot_state="creating",
            range_state_at_snapshot=RangeState.ready.value,
            tenant_id=session.tenant_id,
        )
        db.add(snap)
        db.flush()
        session.baseline_snapshot_id = snap.id
        session.state = BASELINING
        _dispatch("snapshot_range", str(rng.id), str(snap.id))
    else:
        session.state = READY
    return None


def _advance_cleaning(db: Session, session: LabSession, rng: Range | None) -> None:
    if rng is None or rng.state == RangeState.destroyed:
        _release_networks(db, session)
        if rng is not None:
            _dispatch("reconcile_lab_vms", [str(rng.id)], session.backend)
            session.reconciled_at = _now()
        session.state = DESTROYED
    elif rng.state == RangeState.failed:
        # The destroy failed part-way: find and remove whatever is left by name, then
        # try the destroy again so the range record ends where its VMs are.
        session.state = RECONCILE
        _dispatch("reconcile_lab_vms", [str(rng.id)], session.backend)
        rng.state = RangeState.destroying
        _dispatch("destroy_range", str(rng.id))
    else:
        session.state = CLEANING


def _fail(db: Session, session: LabSession, error: str) -> None:
    session.error = error[:2000]
    logger.warning("lab session %s failed: %s", session.id, error)
    rng = db.get(Range, session.range_id) if session.range_id else None
    if rng is not None and rng.state not in (RangeState.destroyed, RangeState.destroying):
        rng.state = RangeState.destroying
        _dispatch("destroy_range", str(rng.id))
        session.state = CLEANING
        session.end_reason = "failed"
        return
    _release_networks(db, session)
    session.state = FAILED


# ── student actions ───────────────────────────────────────────────────


def touch(db: Session, session: LabSession) -> LabSession:
    if session.state not in (READY, ACTIVE):
        return session
    now = _now()
    idle = timedelta(minutes=_profile(db, session)["lifetime"]["idle_minutes"])
    session.last_seen_at = now
    session.idle_expires_at = min(now + idle, _aware(session.max_expires_at))
    session.state = ACTIVE
    db.flush()
    return session


def reset(db: Session, session: LabSession) -> LabSession:
    if session.state not in (READY, ACTIVE):
        raise LabRefusedError(f"the lab is {session.state}; it can be reset once it is ready")
    rng = db.get(Range, session.range_id)
    profile = _profile(db, session)
    if profile["reset"]["mode"] == "snapshot":
        snap = db.get(RangeSnapshot, session.baseline_snapshot_id) if session.baseline_snapshot_id else None
        if snap is None or snap.snapshot_state != "ready":
            raise LabRefusedError("this lab has no reset point")
        snap.snapshot_state = "restoring"
        session.state = RESETTING
        db.flush()
        _dispatch("restore_snapshot", str(rng.id), str(snap.id))
        return session
    # rebuild: tear this range down and build a fresh one for the same session.
    rng.state = RangeState.destroying
    _dispatch("destroy_range", str(rng.id))
    _release_networks(db, session)
    session.range_id = None
    session.state = QUEUED
    db.flush()
    _start(db, session, profile)
    db.flush()
    return session


def add_evidence(db: Session, session: LabSession, item: dict[str, Any]) -> LabSession:
    """Keep a submission or validator result on the session; it outlives the VMs."""
    if session.state in TERMINAL:
        raise LabRefusedError("this lab has ended")
    evidence = json.loads(session.evidence or "[]")
    evidence.append({**item, "received_at": _now().isoformat()})
    session.evidence = json.dumps(evidence)
    db.flush()
    return session


def end(db: Session, session: LabSession, *, reason: str = "completed") -> LabSession:
    """Finish the lab: evidence stays on the session, the VMs are destroyed."""
    if session.state in TERMINAL or session.state in ENDING:
        return session
    session.ended_at = _now()
    session.end_reason = reason
    rng = db.get(Range, session.range_id) if session.range_id else None
    if rng is None or session.state == QUEUED:
        _release_networks(db, session)
        session.state = DESTROYED
    else:
        session.state = EXPIRED if reason == "expired" else COMPLETED
        if rng.state not in (RangeState.destroyed, RangeState.destroying):
            rng.state = RangeState.destroying
            _dispatch("destroy_range", str(rng.id))
    db.flush()
    return session


# ── the runner's sweep ────────────────────────────────────────────────


def sweep(db: Session) -> int:
    """Advance every session that holds resources; returns how many changed state."""
    changed = 0
    for session in db.query(LabSession).filter(LabSession.state.in_(LIVE + ENDING)).all():
        before = session.state
        try:
            advance(db, session)
            db.commit()
        except Exception:  # noqa: BLE001 — one bad session must not stop the sweep
            db.rollback()
            logger.exception("lab session %s could not advance", session.id)
            continue
        changed += session.state != before
    return changed


def reconcile_all(db: Session) -> int:
    """Ask the worker to remove anything finished lab ranges left on the hypervisor."""
    rows = (
        db.query(LabSession.range_id, LabSession.backend)
        .filter(LabSession.state.in_(TERMINAL), LabSession.range_id.isnot(None))
        .all()
    )
    by_backend: dict[str, list[str]] = {}
    for range_id, kind in rows:
        by_backend.setdefault(kind, []).append(str(range_id))
    for kind, ids in by_backend.items():
        for i in range(0, len(ids), 100):
            _dispatch("reconcile_lab_vms", ids[i : i + 100], kind)
    return sum(len(v) for v in by_backend.values())


def console(db: Session, session: LabSession, node: str | None = None) -> dict[str, Any]:
    from ..console_backends import ConsoleError, get_console_backend

    if session.state not in (READY, ACTIVE):
        raise LabRefusedError(f"the lab is {session.state}; the console opens once it is ready")
    allowed = [a["node"] for a in _profile(db, session)["access"] if a["kind"] == "console"]
    target = node or (allowed[0] if allowed else None)
    if target not in allowed:
        raise LabRefusedError(f"no console on {target}", 403)
    vm = probes.vm_for(target, _vms(db.get(Range, session.range_id)))
    if vm is None:
        raise LabRefusedError(f"{target} has no VM yet")
    try:
        access = get_console_backend(session.backend).open(vm)
    except (ValueError, ConsoleError) as exc:
        raise LabRefusedError(str(exc), 502) from exc
    touch(db, session)
    return {"node": target, **access}
