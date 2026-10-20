"""Put a demo live class on a real TrueNorth deployment, and take it off again exactly.

Runs INSIDE the deployed api container, with the app's own models and services against
the real database. ``scripts/ops/demo_live_class.sh`` (on the host) copies it in and keeps
the manifest at /srv/truenorth/state/demo-live-class.json (root, 600):

    sudo scripts/ops/demo_live_class.sh dry-run          # the default: nothing committed
    sudo scripts/ops/demo_live_class.sh apply            # create, or refresh every time
    sudo scripts/ops/demo_live_class.sh remove-dry-run
    sudo scripts/ops/demo_live_class.sh remove

A dry run does all the work in one transaction, reads the class back through
``live_class.service.build`` and rolls back.

What it uses, and never creates or changes: the tenant (the only one, or
``PLATFORM_TENANT_ID``), the staff (the instructor and the admin must already have signed
in; matched by email or display-name prefix ``tn.instructor`` / ``tn.admin``), a published
course (``--course``, default the threat-hunting one) and a range template (``--template``,
default "Medium Enterprise Network"), which sizes the booking through
``range_topology.template_demand`` exactly as ``POST /scheduler/events`` does.

What it creates, each row with a fixed uuid5 id in its own namespace (``NS``), so re-running
``apply`` merges instead of adding and moves every time to now ("started 40 min ago"):

- a demo scenario, and the exercise range recorded on the ``mock`` backend: its
  ``provisioner_output`` has the shape ``worker/range_alloc.stored_output`` records for a
  build, one ``DEMO-`` VM per template node, so the range pages show the template's VMs.
  Nothing is built; the worker's health sweep logs this range as unhealthy (the mock
  backend keeps its state in memory) and changes nothing;
- the exercise, RUNNING since 40 minutes ago, its four objectives and this run's injects;
- a booking over now, of the course, with the instructor teaching it (state ``active``;
  the scheduler clock completes it ``--hours`` from now plus its teardown grace, and,
  ``auto_provisioned`` being false, tears nothing down);
- 14 Students (``source=demo``, ``@demo.invalid``, no Keycloak identity: they cannot sign
  in), assigned to the course through ``enrollment.ensure_enrollment`` (the service picks
  the enrolment and module-progress ids; the manifest records them), three teams, and
  their detection attempts.

``remove`` deletes exactly the manifest's rows. Rows other people made on the demo since
(an annotation, an inject sent from the Ops Center) are found through the foreign keys and
listed; ``remove`` refuses while there are any, unless ``--with-dependents``. Audit-log
rows are never deleted (signed daily exports, ADR 0011).

Not made, on purpose: lab sessions. A lab session needs a course release
(``LabSession.release_id`` is NOT NULL) and releases are never deleted
(``course_releases.models._refuse_deletes``), so a demo release on a real course could not
be taken off again. The live class's lab lane is empty; the learning and quiet lanes fill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

NS = uuid.UUID("de30c1a5-0b5e-4c1a-9d3e-2026102000a1")  # this demo's rows, and only them
MARK = "DEMO"
EMAIL_DOMAIN = "demo.invalid"
DEFAULT_COURSE = "Advanced Threat Hunting"
DEFAULT_TEMPLATE = "Medium Enterprise Network"
DEFAULT_MANIFEST = "/tmp/demo-live-class.json"
STAFF_PREFIX = {"instructor": "tn.instructor", "admin": "tn.admin"}
RUN_ID = "demo-run-1"


def fid(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NS, "/".join(parts))


TEAMS = [("DEMO Blue Cell 1", "#1E5AA8"), ("DEMO Blue Cell 2", "#2E7D32"), ("DEMO Blue Cell 3", "#8E24AA")]

OBJECTIVES = [
    ("detect_c2", "detection", 40, "Detect the C2 beacon from WS01"),
    ("detect_lateral", "detection", 30, "Detect lateral movement to FS01"),
    ("detect_exfil", "detection", 20, "Detect the data exfiltration"),
    ("write_report", "deliverable", 10, "Submit the incident report"),
]

# Minutes after the start, timeline offset, action, status, detail, MITRE.
INJECTS = [
    (0.5, "0:00", "Phishing email delivered to WS01 via MAIL01", "fired", "user opened invoice.docm", "T1566.001"),
    (6, "6:00", "C2 beacon to northwind-update.example", "fired", "HTTPS beacon every 60 s from WS01", "T1071.001"),
    (14, "14:00", "Credential dump on WS01", "fired", "LSASS read by rundll32.exe", "T1003.001"),
    (22, "22:00", "Lateral movement to FS01 over SMB", "fired", "service created remotely on FS01", "T1021.002"),
    (29, "29:00", "Stage file-share archive on FS01", "failed", "agent unreachable on FS01 (retry scheduled)",
     "T1074.001"),
    (35, None, "Instructor hint: review the proxy logs for WS01", "fired", "sent to all teams", None),
]

# 14 Students: first, last, team, attempts (objective, verdict, minutes ago, matched, on target, query).
STUDENTS = [
    ("Amélie", "Tremblay", 0, [
        ("detect_c2", "missed", 24, 120, 8, "url.domain:*northwind*"),
        ("detect_c2", "achieved", 17, 14, 14, "url.domain:northwind-update.example AND event_type:http"),
        ("detect_lateral", "achieved", 4, 9, 8, "event_type:smb AND destination.host:FS01")]),
    ("Jordan", "Okafor", 0, [
        ("detect_c2", "achieved", 18, 16, 15, "dns.question.name:northwind-update.example"),
        ("detect_lateral", "pending", 1, 0, 0, "process.name:services.exe AND host.name:FS01")]),
    ("Priya", "Raman", 0, [
        ("detect_c2", "achieved", 26, 15, 15, "url.domain:northwind-update.example"),
        ("detect_lateral", "achieved", 15, 7, 7, "event_type:smb AND destination.host:FS01"),
        ("detect_exfil", "achieved", 6, 5, 4, "file.extension:7z AND host.name:FS01")]),
    ("Liam", "MacDonald", 0, [("detect_c2", "missed", 30, 300, 6, "event_type:http")]),
    ("Sofia", "Haddad", 0, []),
    ("Ethan", "Nguyen", 1, [
        ("detect_c2", "missed", 28, 0, 0, "url.domain:northwind.com"),
        ("detect_c2", "missed", 19, 412, 9, "event_type:http AND status:200"),
        ("detect_c2", "missed", 9, 88, 12, "user_agent:*curl*")]),
    ("Chloé", "Gagnon", 1, [
        ("detect_c2", "achieved", 26, 15, 14, "url.domain:northwind-update.example"),
        ("detect_lateral", "missed", 21, 0, 0, "event_type:rdp"),
        ("detect_lateral", "missed", 16, 240, 3, "destination.port:445"),
        ("detect_lateral", "missed", 11, 61, 2, "event_type:smb"),
        ("detect_lateral", "missed", 5, 30, 4, "event_type:smb AND source.host:WS01")]),
    ("Marcus", "Bell", 1, [("detect_c2", "missed", 33, 950, 10, "event_type:dns")]),
    ("Hana", "Kowalski", 1, []),
    ("Daniel", "Osei", 1, [
        ("detect_c2", "achieved", 10, 13, 13, "url.domain:northwind-update.example AND event_type:http")]),
    ("Rebecca", "Lindqvist", 2, [
        ("detect_c2", "achieved", 22, 15, 15, "url.domain:northwind-update.example"),
        ("detect_lateral", "missed", 7, 44, 3, "destination.host:FS01")]),
    ("Tyler", "Brennan", 2, [("detect_c2", "missed", 14, 610, 11, "event_type:http AND host.name:WS01")]),
    ("Noor", "Siddiqui", 2, [
        ("detect_c2", "achieved", 30, 15, 15, "url.domain:northwind-update.example"),
        ("detect_lateral", "achieved", 20, 8, 7, "event_type:smb AND destination.host:FS01"),
        ("detect_exfil", "missed", 8, 70, 2, "file.size > 1000000")]),
    ("Gabriel", "Roy", 2, []),
]

SCENARIO_YAML = """name: demo-vigilant-maple
version: "1"
description: DEMO only (scripts/ops/demo_live_class.py). Nothing runs it.
timeline:
  - {t: "0:00", action: "Phishing email delivered to WS01 via MAIL01"}
  - {t: "6:00", action: "C2 beacon to northwind-update.example"}
  - {t: "14:00", action: "Credential dump on WS01"}
  - {t: "22:00", action: "Lateral movement to FS01 over SMB"}
  - {t: "29:00", action: "Stage file-share archive on FS01"}
objectives:
  - {id: detect_c2, type: detection, validator: opensearch_query, points: 40}
  - {id: detect_lateral, type: detection, validator: opensearch_query, points: 30}
  - {id: detect_exfil, type: detection, validator: opensearch_query, points: 20}
  - {id: write_report, type: deliverable, validator: deliverable_check, points: 10}
"""


class RefusedError(Exception):
    pass


def handle_of(first: str, last: str) -> str:
    return f"{first}.{last}".lower().replace("é", "e")


# ----------------------------------------------------------------------------- lookups


def tenant_id(db) -> uuid.UUID:
    from app.models import Tenant

    env = os.getenv("PLATFORM_TENANT_ID")
    if env:
        return uuid.UUID(env)
    tenants = db.query(Tenant).all()
    if len(tenants) != 1:
        raise RefusedError(f"{len(tenants)} tenants and no PLATFORM_TENANT_ID; say which")
    return tenants[0].id


def staff(db, tid, prefixes: dict[str, str]) -> dict:
    """The real instructor and admin. They must have signed in; never created here."""
    from app.models import User
    from sqlalchemy import func, or_

    out = {}
    for key, prefix in prefixes.items():
        prefix = prefix.lower()
        rows = (
            db.query(User)
            .filter(
                User.tenant_id == tid,
                User.deleted_at.is_(None),
                or_(func.lower(User.email).like(f"{prefix}@%"), func.lower(User.display_name).like(f"{prefix}%")),
            )
            .all()
        )
        if len(rows) != 1:
            raise RefusedError(f"{len(rows)} users match {prefix!r}; they must sign in once first")
        out[key] = rows[0]
    return out


def course(db, tid, name: str):
    from app.models import Course

    rows = (
        db.query(Course)
        .filter(Course.tenant_id == tid, Course.is_published.is_(True), Course.name.ilike(f"%{name}%"))
        .order_by(Course.name)
        .all()
    )
    if not rows:
        raise RefusedError(f"no published course matching {name!r}")
    return rows[0]


def template(db, tid, name: str):
    from app.models import Template

    row = (
        db.query(Template)
        .filter(Template.name == name, Template.deleted_at.is_(None),
                (Template.tenant_id == tid) | (Template.is_public.is_(True)))
        .first()
    )
    if row is None:
        raise RefusedError(f"no range template named {name!r}")
    return row


def recorded_output(range_id: uuid.UUID, tmpl_yaml: str) -> dict:
    """The shape worker/range_alloc.stored_output records after a build, for the mock
    backend: one VM per template node (``count`` honoured, as template_demand counts)."""
    from app import safe_yaml
    from app.range_topology import DEFAULT_CORES, DEFAULT_DISK_GB, DEFAULT_MEMORY_MB, _node_specs, _spec

    doc = safe_yaml.load(tmpl_yaml) or {}
    vms = []
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        specs = _node_specs(node)
        count = max(1, int(node.get("count", 1) or 1))
        for n in range(count):
            nid = str(node.get("id") or f"node{len(vms)}") + (f"-{n + 1}" if count > 1 else "")
            vms.append({
                "vm_id": str(fid("vm", nid)), "node_id": nid, "name": f"{MARK}-{nid.upper()}",
                "ip": node.get("ip"), "os": node.get("os"), "status": "running",
                "cpu": _spec(specs, "cores", DEFAULT_CORES),
                "memory_mb": _spec(specs, "memory_mb", DEFAULT_MEMORY_MB),
                "disk_gb": _spec(specs, "disk_gb", DEFAULT_DISK_GB),
            })
    vlans = (doc.get("network") or {}).get("vlans") or []
    networks = [
        {"network_id": str(fid("net", str(v.get("name")))), "name": f"{MARK}-{v.get('name')}",
         "cidr": v.get("cidr"), "vlan_id": v.get("id")}
        for v in vlans if isinstance(v, dict)
    ]
    return {"provider": "mock", "range_id": str(range_id), "vms": vms, "networks": networks,
            "warnings": ["DEMO: recorded by scripts/ops/demo_live_class.py; nothing was built"]}


# ----------------------------------------------------------------------------- apply


def apply(db, args) -> dict:
    """Create or refresh every demo row; return the manifest. Does not commit."""
    from app.detections.models import DetectionSubmission
    from app.enrollment import ensure_enrollment
    from app.models import (
        EnrollmentStatus,
        Exercise,
        ExerciseState,
        ModuleProgress,
        Objective,
        ObjectiveType,
        Range,
        RangeState,
        Scenario,
        Team,
        TeamMembership,
        User,
        UserRole,
    )
    from app.range_topology import template_demand
    from app.scenario_runs.models import InjectRecord
    from app.scheduler.models import EventState, ScheduledEvent

    tid = tenant_id(db)
    people = staff(db, tid, {"instructor": args.instructor, "admin": args.admin})
    crs = course(db, tid, args.course)
    tmpl = template(db, tid, args.template)
    demand = template_demand(tmpl.yaml)
    if demand is None or not demand.vm_count:
        raise RefusedError(f"template {tmpl.name!r} declares no VMs")

    now = datetime.now(UTC)
    started = now - timedelta(minutes=40)

    def ago(minutes: float) -> datetime:
        return now - timedelta(minutes=minutes)

    rows: dict[str, list[str]] = {}

    def keep(table: str, *ids) -> None:
        rows.setdefault(table, []).extend(str(i) for i in ids)

    scenario = db.merge(Scenario(id=fid("scenario"), name=f"{MARK} vigilant-maple", yaml=SCENARIO_YAML,
                                 tenant_id=tid, is_public=False))
    rng_id = fid("range")
    rng = db.merge(Range(
        id=rng_id, name=f"{MARK} VIGILANT MAPLE range", template_id=tmpl.id, tenant_id=tid,
        state=RangeState.running, provisioner_backend="mock", error_message=None,
        provisioner_output=json.dumps(recorded_output(rng_id, tmpl.yaml)),
        description=f"DEMO only, recorded on the mock backend: no VMs exist. Sized from the {tmpl.name} template.",
    ))
    db.flush()
    keep("scenarios", scenario.id)
    keep("ranges", rng.id)

    ex = db.merge(Exercise(
        id=fid("exercise"), name=f"{MARK} VIGILANT MAPLE", kind="assessment", range_id=rng.id,
        scenario_id=scenario.id, state=ExerciseState.running, tenant_id=tid, started_at=started,
        completed_at=None, max_score=sum(p for _, _, p, _ in OBJECTIVES),
    ))
    db.flush()
    keep("exercises", ex.id)

    # Exercise-wide: an objective is achieved once anyone's detection on it was credited.
    first_credit: dict[str, float] = {}
    for *_, attempts in STUDENTS:
        for ref, verdict, minutes, *_ in attempts:
            if verdict == "achieved":
                first_credit[ref] = max(first_credit.get(ref, 0), minutes)
    for n, (ref, kind, points, desc) in enumerate(OBJECTIVES):
        o = db.merge(Objective(
            id=fid("objective", ref), exercise_id=ex.id, ref_id=ref, objective_type=ObjectiveType(kind),
            description=desc, validator="opensearch_query" if kind == "detection" else "deliverable_check",
            points=points, achieved=ref in first_credit,
            achieved_at=ago(first_credit[ref]) if ref in first_credit else None,
            created_at=started + timedelta(seconds=n),
        ))
        keep("objectives", o.id)

    booking = db.merge(ScheduledEvent(
        id=fid("booking"), name=f"{MARK} {crs.name}: VIGILANT MAPLE", tenant_id=tid, course_id=crs.id,
        exercise_id=ex.id, range_id=rng.id, scenario_id=scenario.id, template_id=tmpl.id,
        instructor_id=people["instructor"].id, created_by=people["admin"].id, state=EventState.active,
        auto_provisioned=False, auto_exercise=False,
        start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=args.hours),
        vm_count=demand.vm_count, vcpu_total=demand.vcpu, ram_mb_total=demand.ram_mb, disk_gb_total=demand.disk_gb,
        description="DEMO only (scripts/ops/demo_live_class.py): live-fire hunt, teams of four to five.",
    ))
    keep("scheduled_events", booking.id)

    teams = [
        db.merge(Team(id=fid("team", str(n)), name=name, tenant_id=tid, exercise_id=ex.id, color_hex=color,
                      team_type="blue", is_persistent=False))
        for n, (name, color) in enumerate(TEAMS)
    ]
    db.flush()
    keep("teams", *(t.id for t in teams))

    injects = [fid("inject", str(n)) for n in range(len(INJECTS))]
    seq = 0
    for n, (after, t, action, status, detail, mitre) in enumerate(INJECTS):
        when = started + timedelta(minutes=after)
        db.merge(InjectRecord(
            id=injects[n], exercise_id=ex.id, range_id=rng.id, tenant_id=tid, run_id=RUN_ID,
            source="timeline" if t is not None else "instructor", seq=seq if t is not None else None, t=t,
            action=action, status=status, detail=detail, execution_mode="simulated", mitre_technique=mitre,
            telemetry_count=40 + 7 * n if status == "fired" else 0, telemetry_shipped=status == "fired",
            created_at=when, updated_at=when,
        ))
        seq += 1 if t is not None else 0
    keep("inject_records", *injects)

    for first, last, team, attempts in STUDENTS:
        h = handle_of(first, last)
        uid = fid("student", h)
        user = db.merge(User(
            id=uid, keycloak_id=f"demo-noauth-{uid}", email=f"{h}@{EMAIL_DOMAIN}",
            display_name=f"{first} {last}", first_name=first, last_name=last, rank="Cpl", role=UserRole.student,
            tenant_id=tid, is_active=True, source="demo", unit=f"{MARK} (not a real person)",
            onboarding_state="complete", onboarded_at=now - timedelta(days=10),
        ))
        db.flush()
        keep("users", user.id)
        enr = ensure_enrollment(db, user_id=user.id, course_id=crs.id, tenant_id=tid, source="assigned")
        enr.status = EnrollmentStatus.in_progress
        enr.created_at = enr.enrolled_at = now - timedelta(days=9)
        enr.started_at = now - timedelta(days=8)
        keep("enrollments", enr.id)
        keep("module_progress",
             *(p.id for p in db.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enr.id)))
        tm = db.merge(TeamMembership(id=fid("member", h), user_id=user.id, team_id=teams[team].id,
                                     joined_at=now - timedelta(days=1)))
        keep("team_memberships", tm.id)

        mine = [fid("attempt", h, str(k)) for k in range(len(attempts))]
        for k, (ref, verdict, minutes, matched, on_target, query) in enumerate(attempts):
            reason = None
            if verdict == "missed":
                reason = "no events matched" if not matched else f"precision {on_target / matched:.2f} below 0.50"
            db.merge(DetectionSubmission(
                id=mine[k], tenant_id=tid, exercise_id=ex.id, objective_ref=ref, user_id=user.id, query=query,
                submitted_at=ago(minutes), verdict=verdict, events_matched=matched, on_target=on_target,
                threshold=2, min_precision=0.5, window_start=started, window_end=ago(minutes), reason=reason,
            ))
        keep("detection_submissions", *mine)
    db.flush()

    return {
        "what": "TrueNorth demo live class (scripts/ops/demo_live_class.py)",
        "namespace": str(NS),
        "applied_at": now.isoformat(),
        "tenant_id": str(tid),
        "exercise_id": str(ex.id),
        "course": {"id": str(crs.id), "name": crs.name},
        "template": {"id": str(tmpl.id), "name": tmpl.name},
        "instructor": {"id": str(people["instructor"].id), "email": people["instructor"].email},
        "admin": {"id": str(people["admin"].id), "email": people["admin"].email},
        "booking_totals": {"vm_count": demand.vm_count, "vcpu": demand.vcpu, "ram_mb": demand.ram_mb,
                           "disk_gb": demand.disk_gb},
        "rows": rows,
    }


# ----------------------------------------------------------------------------- remove

# Children first.
ORDER = ["detection_submissions", "inject_records", "team_memberships", "module_progress", "enrollments", "teams",
         "scheduled_events", "objectives", "exercises", "ranges", "scenarios", "users"]
# Removed with the enrolments they belong to (made by the service, keyed by enrollment).
WITH_ENROLLMENT = ("enrollment_release_pins",)


def _tables():
    from app.db import Base

    return Base.metadata.tables


def dependents(db, rows: dict[str, list[str]]) -> dict[str, list[str]]:
    """Rows not in the manifest that point, by foreign key, at a manifest row: what people
    did on the demo since it was applied. ``{table: [primary key, ...]}``."""
    from sqlalchemy import select

    ours = {t: {uuid.UUID(i) for i in ids} for t, ids in rows.items()}
    found: dict[str, set[str]] = {}
    for table in _tables().values():
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or table.name in WITH_ENROLLMENT:
            continue
        mine = ours.get(table.name, set())
        for fk in table.foreign_keys:
            target = ours.get(fk.column.table.name)
            if not target:
                continue
            for (key,) in db.execute(select(pk[0]).where(fk.parent.in_(target))):
                if key not in mine:
                    found.setdefault(table.name, set()).add(str(key))
    return {t: sorted(v) for t, v in found.items()}


def remove(db, manifest: dict, with_dependents: bool) -> dict[str, int]:
    from app.models import User
    from sqlalchemy import delete

    rows = manifest["rows"]
    tables = _tables()
    # Only this demo's own Students are ever deleted.
    for u in db.query(User).filter(User.id.in_([uuid.UUID(i) for i in rows.get("users", [])])):
        if not (u.source == "demo" and u.email.endswith(f"@{EMAIL_DOMAIN}")
                and u.id == fid("student", u.email.split("@")[0])):
            raise RefusedError(f"manifest user {u.id} is not a demo Student")
    extra = dependents(db, rows)
    counts: dict[str, int] = {}
    if extra:
        if not with_dependents:
            raise RefusedError("rows made on the demo since it was applied point at it: "
                          + json.dumps({t: len(v) for t, v in extra.items()})
                          + "; re-run with --with-dependents to delete those too")
        for t, ids in extra.items():  # one level only: a refused delete rolls everything back
            pk = list(tables[t].primary_key.columns)[0]
            counts[f"{t} (dependent)"] = db.execute(
                delete(tables[t]).where(pk.in_([uuid.UUID(i) for i in ids]))).rowcount
    enrollments = [uuid.UUID(i) for i in rows.get("enrollments", [])]
    for name in WITH_ENROLLMENT:
        if name in tables and enrollments:
            t = tables[name]
            counts[name] = db.execute(delete(t).where(t.c.enrollment_id.in_(enrollments))).rowcount
    for name in ORDER:
        ids = rows.get(name)
        if not ids:
            continue
        pk = list(tables[name].primary_key.columns)[0]
        counts[name] = db.execute(delete(tables[name]).where(pk.in_([uuid.UUID(i) for i in ids]))).rowcount
    db.flush()
    return counts


# ----------------------------------------------------------------------------- read back


def class_summary(db, exercise_id: str) -> dict:
    from app.live_class import service
    from app.models import Exercise, Range
    from app.range_ops.service import recorded_vms
    from app.scheduler.models import ScheduledEvent

    ex = db.get(Exercise, uuid.UUID(exercise_id))
    out = service.build(db, ex, sees_key=True)
    kinds: dict[str, int] = {}
    for s in out.students:
        for a in s.attention:
            kinds[a.kind] = kinds.get(a.kind, 0) + 1
    rng = db.get(Range, ex.range_id)
    booking = db.query(ScheduledEvent).filter(ScheduledEvent.exercise_id == ex.id).first()
    return {
        "exercise": f"{ex.name} [{out.state}] started {out.started_at:%Y-%m-%d %H:%M:%S} UTC",
        "students": len(out.students),
        "teams": sorted({s.team.name for s in out.students if s.team}),
        "objectives": [o.ref_id for o in out.objectives],
        "attempts": sum(s.attempts_total for s in out.students),
        "credited": sum(s.attempts_credited for s in out.students),
        "attention_by_kind": kinds,
        "labs": sum(len(s.labs) for s in out.students),
        "range": f"{rng.name} [{rng.state.value}] backend={rng.provisioner_backend} recorded_vms={recorded_vms(rng)}",
        "booking": None if booking is None else (
            f"{booking.name} [{booking.state.value}] {booking.start_time:%H:%M}-{booking.end_time:%H:%M} UTC, "
            f"vm={booking.vm_count} vcpu={booking.vcpu_total} ram_mb={booking.ram_mb_total} "
            f"disk_gb={booking.disk_gb_total}"),
    }


# ----------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="commit (default: a dry run, rolled back)")
    ap.add_argument("--dry-run", action="store_true", help="the default; wins over --apply")
    ap.add_argument("--remove", action="store_true", help="delete exactly the manifest's rows")
    ap.add_argument("--with-dependents", action="store_true", help="with --remove: also rows people made on it")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--course", default=DEFAULT_COURSE, help="name (substring) of a published course")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE, help="name of a range template, for sizing")
    ap.add_argument("--instructor", default=STAFF_PREFIX["instructor"], help="email / display-name prefix")
    ap.add_argument("--admin", default=STAFF_PREFIX["admin"], help="email / display-name prefix")
    ap.add_argument("--hours", type=int, default=3, help="the booking ends this many hours from now")
    args = ap.parse_args(argv)
    commit = args.apply and not args.dry_run
    mode = ("remove" if args.remove else "apply") + ("" if commit else " (dry run, rolled back)")

    sys.path.insert(0, "/app")
    import app.main  # noqa: F401 -- registers every model on Base
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        if args.remove:
            try:
                with open(args.manifest) as f:
                    manifest = json.load(f)
            except FileNotFoundError:
                raise RefusedError(f"no manifest at {args.manifest}") from None
            if manifest.get("namespace") != str(NS):
                raise RefusedError("that manifest is not this demo's")
            report = {"mode": mode, "deleted": remove(db, manifest, args.with_dependents)}
        else:
            manifest = apply(db, args)
            report = {
                "mode": mode,
                "rows": {t: len(v) for t, v in manifest["rows"].items()},
                "course": manifest["course"], "template": manifest["template"],
                "instructor": manifest["instructor"]["email"], "admin": manifest["admin"]["email"],
                "booking_totals": manifest["booking_totals"],
                "class": class_summary(db, manifest["exercise_id"]),
                "class_url_path": f"/ops-center/{manifest['exercise_id']}",
            }
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        if commit:
            db.commit()
            if not args.remove:
                fd = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(manifest, f, indent=2, ensure_ascii=False)
        else:
            db.rollback()
    except RefusedError as e:
        db.rollback()
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
