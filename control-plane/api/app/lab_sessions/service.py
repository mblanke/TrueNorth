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

Every operation runs under the session's lease (run_locked), so the sweep in every API
process, page polls and relaunches never act on one session at once; worker tasks are
written to the session in the transaction that asks for them and sent only after it
commits, still under the lease. One the broker refuses, or one whose process died before
sending it, stays on the session and the sweep sends it again. Networks return to the pool only
after the VMs are destroyed and leftovers were looked for. Worker tasks are the existing
range tasks; this module writes no hypervisor state itself.
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
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
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
    RECONCILE_WAIT,
    RESETTING,
    RUNS_RESOURCES,
    TERMINAL,
    LabNetworkLease,
    LabSession,
)

logger = logging.getLogger(__name__)
BASELINE = "lab-baseline"
OUTBOX = "lab_outbox"  # sessions (under our lease) whose pending tasks go out after commit
RESENT = "lab_resent"  # of those, sessions sending tasks a refusal or a crash left behind
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_EVIDENCE_ITEMS = 200
LEASE_SECONDS = 120


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
    # A profile's own limits are the author's; the platform sets the ceiling.
    if len(profile["nodes"]) > _int_env("LAB_MAX_VMS_PER_LAB", 20):
        problems.append(("lab.size", f"{len(profile['nodes'])} VMs is above this platform's limit per lab"))
    for node in profile["nodes"]:
        if node["ram_mb"] > _int_env("LAB_MAX_VM_RAM_MB", 16384) or node["disk_gb"] > _int_env(
            "LAB_MAX_VM_DISK_GB", 200
        ):
            problems.append(("lab.size", f"node {node['name']} asks for more RAM or disk than this platform allows"))
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


# ── dispatch: after commit, never lost ────────────────────────────────
#
# A task is written to the session (``pending``) in the transaction that asks for it, so
# it commits with the state change or not at all. flush_outbox() sends it after the
# commit, while the caller still holds the session's lease, and only then removes it: a
# worker never looks for rows the API has not committed, and a process that dies between
# the commit and the send leaves the task on the session for the sweep, once the dead
# process's lease runs out. A task the broker refuses stays too. Delivery is at least
# once: a crash after the send and before its removal sends it again.
#
# A session with tasks left behind does nothing else until they are sent, and its step
# clock restarts at the send: a grace period (e.g. for leftover VMs to be found before
# the networks go back) counts from when the task really went out, not from the crash.
# Known limit (CR1-11): the lease has no owner, so a process stalled past its lease can
# still release one another process took since.


def _hold(db: Session, session: LabSession) -> None:
    """This transaction holds the session's lease and sends its pending tasks after commit."""
    held = db.info.setdefault(OUTBOX, [])
    if session.id not in held:
        held.append(session.id)


def _send(db: Session, session: LabSession, task: str, *args: Any) -> None:
    session.pending = json.dumps(json.loads(session.pending or "[]") + [[task, list(args)]])
    _hold(db, session)


def flush_outbox(db: Session) -> int:
    """Send the pending tasks of the sessions this transaction held, then let their leases
    go. Call after commit. Returns tasks not sent (kept, in order, for the sweep)."""
    unsent = 0
    resent = db.info.pop(RESENT, set())
    try:
        for session_id in db.info.pop(OUTBOX, []):
            session = db.get(LabSession, session_id)
            if session is None:
                continue
            kept: list[list[Any]] = []
            sent = False
            for task, args in json.loads(session.pending or "[]"):
                if kept:  # never send past a refused task
                    kept.append([task, args])
                    continue
                try:
                    if _dispatch(task, *args) is None:
                        kept.append([task, args])
                    else:
                        sent = True
                except Exception as exc:  # noqa: BLE001 — a call the task contract refuses
                    # A bug, not an outage: sending it again can never work, and keeping it
                    # would block every task behind it. Dropped, and said on the session.
                    logger.exception("lab session %s: task %s dropped", session.id, task)
                    session.error = f"internal error: the {task} task was refused ({exc})"[:2000]
            session.pending = json.dumps(kept)
            if sent and session.id in resent:
                session.state_since = _now()
            unsent += len(kept)
            release(session)
    finally:
        db.commit()
    return unsent


def _set_state(session: LabSession, state: str) -> None:
    if session.state != state:
        session.state = state
        session.state_since = _now()
        session.step_attempts = 0


def _in_state_for(session: LabSession, now: datetime) -> timedelta:
    return now - _aware(session.state_since or session.created_at or now)


def claim(db: Session, session: LabSession, seconds: int = LEASE_SECONDS) -> bool:
    """Take the session's lease, so one process advances it at a time (sweeps in every API
    process, page polls and launches all meet here). False if another holds it."""
    now = _now()
    taken = db.execute(
        update(LabSession)
        .where(
            LabSession.id == session.id,
            or_(LabSession.lease_until.is_(None), LabSession.lease_until < now),
        )
        .values(lease_until=now + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    ).rowcount
    db.refresh(session)
    return taken == 1


def release(session: LabSession) -> None:
    session.lease_until = None


# ── capacity ──────────────────────────────────────────────────────────


def _pool(db: Session) -> list[str]:
    """Sync the configured port-group pool into lease rows; returns the pool."""
    names = [p.strip() for p in os.getenv("LAB_PORT_GROUPS", "").split(",") if p.strip()]
    known = {row.port_group for row in db.query(LabNetworkLease).all()}
    for name in names:
        if name not in known:
            try:
                with db.begin_nested():
                    db.add(LabNetworkLease(port_group=name))
            except IntegrityError:  # another process added it first
                pass
    return names


def _lease_networks(db: Session, session: LabSession, profile: dict[str, Any]) -> dict[str, str] | None:
    """One free isolated port group per profile network, or None if the pool (or this
    tenant's share of it) is short. The mock backend needs no real networks."""
    if session.backend == "mock":
        return {}
    pool = _pool(db)
    if not pool:
        raise LabRefusedError("no lab networks are configured (LAB_PORT_GROUPS)", 503)
    wanted = [n["name"] for n in profile["networks"]]
    tenant_held = (
        db.query(LabNetworkLease)
        .join(LabSession, LabSession.id == LabNetworkLease.session_id)
        .filter(LabSession.tenant_id == session.tenant_id)
        .count()
    )
    if tenant_held + len(wanted) > _int_env("LAB_MAX_NETWORKS_PER_TENANT", 1000):
        return None
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


def _session_networks(db: Session, session: LabSession) -> dict[str, str]:
    return {
        lease.network_name: lease.port_group
        for lease in db.query(LabNetworkLease).filter(LabNetworkLease.session_id == session.id).all()
    }


def _release_networks(db: Session, session: LabSession) -> None:
    for lease in db.query(LabNetworkLease).filter(LabNetworkLease.session_id == session.id).all():
        lease.session_id, lease.network_name, lease.leased_at = None, "", None


def _quota_problem(db: Session, session: LabSession) -> str | None:
    """Room for this lab among labs that hold machines. Queued labs hold nothing, so they
    never count against each other (counting them deadlocked the queue)."""
    holding = db.query(LabSession).filter(LabSession.state.in_(RUNS_RESOURCES), LabSession.id != session.id)
    if holding.filter(LabSession.user_id == session.user_id).count() >= _int_env("LAB_MAX_SESSIONS_PER_USER", 1):
        return "you already have a lab running; end it before starting another"
    tenant = holding.filter(LabSession.tenant_id == session.tenant_id)
    if tenant.count() >= _int_env("LAB_MAX_SESSIONS_PER_TENANT", 1000):
        return "all lab places are in use; this lab starts when one is free"
    vcpu = tenant.with_entities(func.coalesce(func.sum(LabSession.vcpu), 0)).scalar() or 0
    if vcpu + session.vcpu > _int_env("LAB_MAX_VCPU_PER_TENANT", 20000):
        return "the lab capacity for your organisation is in use; this lab starts when some is free"
    older = (
        db.query(LabSession)
        .filter(
            LabSession.state == QUEUED,
            LabSession.tenant_id == session.tenant_id,
            LabSession.created_at < session.created_at,
            LabSession.id != session.id,
        )
        .count()
    )
    if older:
        return "other labs are waiting; this lab starts in turn"
    return None


# ── launch ────────────────────────────────────────────────────────────


def release_for_student(
    db: Session, *, tenant_id: uuid.UUID, user_id: uuid.UUID, course_id: uuid.UUID, auto_enroll: bool = False
) -> CourseRelease:
    """The release a student's lab comes from: the one their enrollment is pinned to (the
    release they started on), else the course's accepted release. A Moodle lab link names
    the course, so publishing a new release never moves a student's lab under them.

    A Moodle launch (``auto_enroll``) enrols the student, as Moodle membership is the
    control there; a TrueNorth launch needs an existing, current enrollment."""
    from ..course_releases.service import active_release, pin_enrollment
    from ..enrollment import ensure_enrollment
    from ..models import Course, Enrollment, EnrollmentStatus

    course = db.query(Course).filter(Course.id == course_id, Course.tenant_id == tenant_id).one_or_none()
    if course is None:
        raise LabRefusedError("course not found", 404)
    enrollment = db.query(Enrollment).filter_by(user_id=user_id, course_id=course_id).one_or_none()
    if enrollment is None and auto_enroll:
        enrollment = ensure_enrollment(db, user_id=user_id, course_id=course_id, tenant_id=tenant_id)
    if enrollment is None:
        raise LabRefusedError("you are not enrolled in this course", 403)
    if enrollment.status in (EnrollmentStatus.withdrawn, EnrollmentStatus.failed):
        raise LabRefusedError("your enrollment in this course is closed", 403)
    pin = pin_enrollment(db, enrollment)
    release = db.get(CourseRelease, pin.release_id) if pin else active_release(db, course_id)
    if release is None:
        raise LabRefusedError("this course has no accepted release", 404)
    return release


def launch(
    db: Session, *, tenant_id: uuid.UUID, user_id: uuid.UUID, release_id: uuid.UUID, activity_id: str
) -> tuple[LabSession, bool]:
    """The student's session for this activity: the live one if there is one, else a new
    attempt. Never a second range for the same attempt. Tasks go out on flush_outbox()."""
    from ..models import User

    release = (
        db.query(CourseRelease)
        .filter(CourseRelease.id == release_id, CourseRelease.tenant_id == tenant_id)
        .one_or_none()
    )
    if release is None:
        raise LabRefusedError("release not found", 404)
    if release.state not in (ACCEPTED, SUPERSEDED):
        raise LabRefusedError("this release has not been accepted")
    # One launch per student at a time: their own row is the lock.
    db.query(User).filter(User.id == user_id).with_for_update().one_or_none()
    key = (
        LabSession.tenant_id == tenant_id,
        LabSession.user_id == user_id,
        LabSession.release_id == release_id,
        LabSession.activity_id == activity_id,
    )
    latest = db.query(LabSession).filter(*key).order_by(LabSession.attempt.desc()).first()
    if latest is not None and latest.state not in TERMINAL:
        return latest, False
    other = db.query(LabSession).filter(LabSession.user_id == user_id, LabSession.state.in_(HOLDS_RESOURCES)).first()
    if other is not None:
        raise LabRefusedError("you already have a lab running or waiting; end it before starting another")

    profile = release_profile(db, release, activity_id)
    check_profile(db, profile, activity_id)
    now = _now()
    session = LabSession(
        tenant_id=tenant_id,
        user_id=user_id,
        release_id=release_id,
        activity_id=activity_id,
        attempt=(latest.attempt + 1) if latest else 1,
        state=QUEUED,
        state_since=now,
        created_at=now,
        profile_id=profile["id"],
        profile_digest=profile_digest(profile),
        backend=backend(),
        vcpu=sum(n["vcpu"] for n in profile["nodes"]),
        ram_mb=sum(n["ram_mb"] for n in profile["nodes"]),
        # Held by this launch until flush_outbox() has sent its tasks: a sweep must not
        # send them a second time in between.
        lease_until=now + timedelta(seconds=LEASE_SECONDS),
    )
    try:
        with db.begin_nested():
            db.add(session)
            db.flush()
    except IntegrityError:  # a double click got there first
        existing = db.query(LabSession).filter(*key).order_by(LabSession.attempt.desc()).first()
        if existing is None:
            raise
        return existing, False
    _hold(db, session)
    _start(db, session, profile)
    db.flush()
    return session, True


def _start(
    db: Session, session: LabSession, profile: dict[str, Any], *, networks: dict[str, str] | None = None
) -> None:
    """queued -> provisioning when there is room (a rebuild passes the networks it keeps and
    keeps its lifetime); stays queued, with why, otherwise."""
    rebuild = networks is not None
    if not rebuild:
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
    now = _now()
    session.range_id = rng.id
    _set_state(session, PROVISIONING)
    session.error = ""
    session.provisioning_at = now
    if not rebuild:
        lifetime = profile["lifetime"]
        session.max_expires_at = now + timedelta(minutes=lifetime["max_minutes"])
        session.idle_expires_at = now + timedelta(minutes=lifetime["idle_minutes"])
    db.flush()
    _send(db, session, "provision_range", str(rng.id))


# ── advance ───────────────────────────────────────────────────────────


def _profile(db: Session, session: LabSession) -> dict[str, Any]:
    return release_profile(db, db.get(CourseRelease, session.release_id), session.activity_id)


def _vms(rng: Range | None) -> list[dict[str, Any]]:
    try:
        return list(json.loads(rng.provisioner_output or "{}").get("vms") or []) if rng else []
    except ValueError:
        return []


def _step_timeout() -> timedelta:
    return timedelta(seconds=_int_env("LAB_STEP_TIMEOUT", 3600))


def advance(db: Session, session: LabSession) -> LabSession:
    """One step forward from whatever the session is waiting on. Callers hold its lease
    (claim) and send the queued tasks after commit (flush_outbox)."""
    now = _now()
    rng = db.get(Range, session.range_id) if session.range_id else None
    state = session.state
    if session.pending and session.pending != "[]":
        # Left by a refusal or a process that died before sending: send them first, and
        # take no step that assumes they went out (flush_outbox restarts the step clock).
        _hold(db, session)
        db.info.setdefault(RESENT, set()).add(session.id)
        return session

    if state == QUEUED:
        if _in_state_for(session, now) > timedelta(seconds=_int_env("LAB_QUEUE_TIMEOUT", 7200)):
            end(db, session, reason="queue_timeout")
        else:
            _start(db, session, _profile(db, session))
    elif state == PROVISIONING:
        _advance_provisioning(db, session, rng, now)
    elif state == BASELINING:
        _advance_baselining(db, session, rng, now)
    elif state == RESETTING:
        _advance_resetting(db, session, rng, now)
    elif state in (READY, ACTIVE):
        if now >= _aware(session.max_expires_at) or now >= _aware(session.idle_expires_at):
            end(db, session, reason="expired")
    elif state in ENDING:
        _advance_cleaning(db, session, rng, now)
    db.flush()
    return session


def _advance_provisioning(db: Session, session: LabSession, rng: Range | None, now: datetime) -> None:
    if rng is None:
        return _fail(db, session, "the lab's range record is missing")
    if rng.state == RangeState.failed:
        return _fail(db, session, f"the lab could not be built: {rng.error_message or 'provisioning failed'}")
    timeout = timedelta(seconds=_int_env("LAB_PROVISION_TIMEOUT", 3600))
    if now - _aware(session.provisioning_at or now) > timeout:
        return _fail(db, session, f"the lab was not ready within {int(timeout.total_seconds() // 60)} minutes")
    if rng.state != RangeState.ready:
        return None
    results = probes.run(_profile(db, session)["health_checks"], _vms(rng), range_id=rng.id)
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
        _set_state(session, BASELINING)
        _send(db, session, "snapshot_range", str(rng.id), str(snap.id))
    else:
        _set_state(session, READY)
    return None


def _advance_baselining(db: Session, session: LabSession, rng: Range | None, now: datetime) -> None:
    snap = db.get(RangeSnapshot, session.baseline_snapshot_id) if session.baseline_snapshot_id else None
    if snap is not None and snap.snapshot_state == "ready":
        _set_state(session, READY)
    elif snap is None or (rng is not None and rng.state == RangeState.failed):
        _fail(db, session, "the reset point (baseline snapshot) could not be taken")
    elif snap.snapshot_state == "failed" or _in_state_for(session, now) > _step_timeout():
        # A snapshot attempt can fail transiently; try again a few times before giving up.
        if session.step_attempts >= 2:
            _fail(db, session, "the reset point (baseline snapshot) could not be taken")
        else:
            session.step_attempts += 1
            session.state_since = now
            snap.snapshot_state = "creating"
            _send(db, session, "snapshot_range", str(rng.id), str(snap.id))


def _advance_resetting(db: Session, session: LabSession, rng: Range | None, now: datetime) -> None:
    profile = _profile(db, session)
    if profile["reset"]["mode"] == "snapshot":
        snap = db.get(RangeSnapshot, session.baseline_snapshot_id) if session.baseline_snapshot_id else None
        if rng is None or rng.state == RangeState.failed:
            return _fail(db, session, "the reset did not complete; the lab has been shut down")
        if snap is not None and snap.snapshot_state == "ready" and rng.state != RangeState.destroying:
            _set_state(session, ACTIVE)
        elif _in_state_for(session, now) > _step_timeout():
            _fail(db, session, "the reset did not complete in time; the lab has been shut down")
        return None
    # Rebuild: the old range must be gone before a new one is built on the same networks.
    if rng is not None and rng.state == RangeState.destroyed:
        session.retired_ranges = json.dumps(json.loads(session.retired_ranges or "[]") + [str(rng.id)])
        _send(db, session, "reconcile_lab_vms", [str(rng.id)], session.backend)
        _start(db, session, profile, networks=_session_networks(db, session))
    elif rng is None or rng.state == RangeState.failed or _in_state_for(session, now) > _step_timeout():
        _fail(db, session, "the rebuild did not complete; the lab has been shut down")
    return None


def _advance_cleaning(db: Session, session: LabSession, rng: Range | None, now: datetime) -> None:
    """Networks go back to the pool only once the VMs are gone and leftovers were looked
    for, so no other student's lab ever shares a network with this one's machines."""
    if rng is None or (rng.state == RangeState.destroyed and session.state == RECONCILE_WAIT):
        if rng is not None and _in_state_for(session, now) < timedelta(seconds=_int_env("LAB_RECONCILE_GRACE", 120)):
            return
        _release_networks(db, session)
        session.reconciled_at = now
        _set_state(session, DESTROYED)
    elif rng.state == RangeState.destroyed:
        ids = json.loads(session.retired_ranges or "[]") + [str(rng.id)]
        _send(db, session, "reconcile_lab_vms", ids, session.backend)
        _set_state(session, RECONCILE_WAIT)
    elif rng.state == RangeState.failed or (
        rng.state == RangeState.destroying and _in_state_for(session, now) > _step_timeout()
    ):
        if session.step_attempts >= _int_env("LAB_TEARDOWN_ATTEMPTS", 5):
            # Its networks stay reserved: machines may still be on them. An operator clears it.
            session.error = "the lab could not be shut down cleanly; an operator has been asked to clear it"
            session.state = FAILED
            logger.error("lab session %s needs an operator: teardown kept failing", session.id)
            return
        session.state = RECONCILE
        session.state_since = now
        session.step_attempts += 1
        _send(db, session, "reconcile_lab_vms", [str(rng.id)], session.backend)
        rng.state = RangeState.destroying
        _send(db, session, "destroy_range", str(rng.id))
    elif session.state in (COMPLETED, EXPIRED):
        _set_state(session, CLEANING)


def _fail(db: Session, session: LabSession, error: str) -> None:
    session.error = error[:2000]
    session.end_reason = session.end_reason or "failed"
    logger.warning("lab session %s failed: %s", session.id, error)
    rng = db.get(Range, session.range_id) if session.range_id else None
    if rng is not None and rng.state != RangeState.destroyed:
        if rng.state != RangeState.destroying:
            rng.state = RangeState.destroying
            _send(db, session, "destroy_range", str(rng.id))
        _set_state(session, CLEANING)
        return
    _release_networks(db, session)
    _set_state(session, FAILED)


# ── student actions ───────────────────────────────────────────────────


def touch(db: Session, session: LabSession) -> LabSession:
    if session.state not in (READY, ACTIVE):
        return session
    now = _now()
    idle = timedelta(minutes=_profile(db, session)["lifetime"]["idle_minutes"])
    session.last_seen_at = now
    session.idle_expires_at = min(now + idle, _aware(session.max_expires_at))
    _set_state(session, ACTIVE)
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
        _set_state(session, RESETTING)
        db.flush()
        _send(db, session, "restore_snapshot", str(rng.id), str(snap.id))
        return session
    # Rebuild: destroy this range; advance builds the new one on the same networks once the
    # old machines are gone. The lab's end times do not move.
    rng.state = RangeState.destroying
    _set_state(session, RESETTING)
    db.flush()
    _send(db, session, "destroy_range", str(rng.id))
    return session


def add_evidence(db: Session, session: LabSession, item: dict[str, Any]) -> LabSession:
    """Keep a submission or validator result on the session; it outlives the VMs."""
    if session.state in TERMINAL:
        raise LabRefusedError("this lab has ended")
    if len(json.dumps(item)) > MAX_EVIDENCE_BYTES:
        raise LabRefusedError("that submission is too large", 413)
    evidence = json.loads(session.evidence or "[]")
    if len(evidence) >= MAX_EVIDENCE_ITEMS:
        raise LabRefusedError("this lab has too many submissions", 413)
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
    if rng is None:
        _release_networks(db, session)
        _set_state(session, DESTROYED)
    else:
        _set_state(session, EXPIRED if reason == "expired" else COMPLETED)
        if rng.state not in (RangeState.destroyed, RangeState.destroying):
            rng.state = RangeState.destroying
            _send(db, session, "destroy_range", str(rng.id))
    db.flush()
    return session


def run_locked(db: Session, session: LabSession, fn, *args: Any, busy_ok: bool = False, **kwargs: Any) -> Any:
    """Run one operation under the session's lease and send its tasks after commit. If
    another process holds the lease: a read (``busy_ok``) gets the session as it stands,
    an action is refused so the caller can try again."""
    if not claim(db, session):
        if busy_ok:
            return session
        raise LabRefusedError("the lab is busy with another request; try again in a moment")
    try:
        with db.begin_nested():  # a refusal undoes this operation only, never earlier work
            result = fn(db, session, *args, **kwargs)
    except Exception:
        db.info.pop(OUTBOX, None)
        db.info.pop(RESENT, None)
        release(session)
        db.commit()
        raise
    _hold(db, session)
    try:
        db.commit()  # the lease is still ours: no sweep sends these tasks in between
    except Exception:
        db.info.pop(OUTBOX, None)
        db.info.pop(RESENT, None)
        raise
    flush_outbox(db)  # sends, then releases the lease
    return result


# ── the runner's sweep ────────────────────────────────────────────────


def sweep(db: Session) -> int:
    """Advance every session that holds resources, oldest first; returns how many changed
    state. Each session is advanced under its own lease and its own transaction."""
    changed = 0
    for session in (
        db.query(LabSession)
        # An ended session can still have tasks left to send (a cleanup check refused).
        .filter(or_(LabSession.state.in_(LIVE + ENDING), LabSession.pending != "[]"))
        .order_by(LabSession.created_at)
        .all()
    ):
        before = session.state
        try:
            run_locked(db, session, advance, busy_ok=True)
        except Exception:  # noqa: BLE001 — one bad session must not stop the sweep
            logger.exception("lab session %s could not advance", session.id)
            continue
        changed += session.state != before
    return changed


def reconcile_all(db: Session, tenant_id: uuid.UUID) -> int:
    """Ask the worker to look again for VMs this tenant's finished labs might have left."""
    rows = (
        db.query(LabSession.range_id, LabSession.backend, LabSession.retired_ranges)
        .filter(
            LabSession.tenant_id == tenant_id,
            LabSession.state.in_(TERMINAL),
            LabSession.range_id.isnot(None),
        )
        .all()
    )
    by_backend: dict[str, list[str]] = {}
    for range_id, kind, retired in rows:
        by_backend.setdefault(kind, []).extend([str(range_id), *json.loads(retired or "[]")])
    for kind, ids in by_backend.items():
        for i in range(0, len(ids), 200):
            _dispatch("reconcile_lab_vms", ids[i : i + 200], kind)
    return sum(len(v) for v in by_backend.values())


def lab_range_ids(db: Session, range_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these ranges belong to lab sessions (current or rebuilt-away)."""
    if not range_ids:
        return set()
    found = {r for (r,) in db.query(LabSession.range_id).filter(LabSession.range_id.in_(range_ids)).all()}
    for (retired,) in db.query(LabSession.retired_ranges).filter(LabSession.retired_ranges != "[]").all():
        found |= {uuid.UUID(r) for r in json.loads(retired) if uuid.UUID(r) in set(range_ids)}
    return found


def console(db: Session, session: LabSession, node: str | None = None) -> dict[str, Any]:
    from ..console_backends import ConsoleError, get_console_backend

    if session.state not in (READY, ACTIVE):
        raise LabRefusedError(f"the lab is {session.state}; the console opens once it is ready")
    allowed = [a["node"] for a in _profile(db, session)["access"] if a["kind"] == "console"]
    target = node or (allowed[0] if allowed else None)
    if target not in allowed:
        raise LabRefusedError(f"no console on {target}", 403)
    vm = probes.vm_for(target, _vms(db.get(Range, session.range_id)), range_id=session.range_id)
    if vm is None:
        raise LabRefusedError(f"{target} has no VM yet")
    try:
        access = get_console_backend(session.backend).open(vm)
    except (ValueError, ConsoleError) as exc:
        raise LabRefusedError(str(exc), 502) from exc
    touch(db, session)
    return {"node": target, **access}
