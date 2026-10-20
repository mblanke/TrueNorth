"""Dev-only: seed a live class for the Ops Center demo (issue #199, ``GET /ops/exercises/{id}/class``).

Never for a deployment. Run by scripts/dev/live_class_demo.sh against the demo's own SQLite
file (``DATABASE_URL``); it refuses any other database URL.

What it makes, in one tenant ("Demo Training Centre"):

- an instructor and an admin, whose generated test passwords go to ``.demo/credentials.txt``
  (mode 600, gitignored) and, hashed, to ``.demo/accounts.json`` for the demo sign-in page;
- a published course with four modules, a release of it, and a calendar booking over now with
  the demo instructor as instructor, tied to a RUNNING exercise (started 40 minutes ago) on a range;
- 14 Students, assigned to the course (``enrollment.ensure_enrollment``), in three teams;
- this run's injects (timeline and one instructor inject) across the last 40 minutes;
- per Student: lab sessions launched from the course release, detection attempts with verdicts,
  heartbeats, so every lane of the live class has someone in it.

Idempotent: every row has a fixed id (uuid5) and is merged, and every time is anchored to now,
so re-running it refreshes the clock ("started 40 min ago") without duplicating anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEMO = pathlib.Path(os.getenv("DEMO_DIR") or ROOT / ".demo")
sys.path.insert(0, str(ROOT / "control-plane/api"))

NS = uuid.UUID("5eed1ce0-7a55-4c1a-9d3e-000000000199")
TENANT = uuid.UUID(os.getenv("PLATFORM_TENANT_ID") or "5eed1ce0-0000-4000-8000-000000000199")


def fid(*parts: str) -> uuid.UUID:
    """A fixed id for a demo row, so a re-run merges instead of adding."""
    return uuid.uuid5(NS, "/".join(parts))


STAFF = {
    # key: (username, role, first, last, rank)
    "instructor": ("ines.laurent", "instructor", "Ines", "Laurent", "Capt"),
    "admin": ("ada.mercer", "admin", "Ada", "Mercer", "Maj"),
}

TEAMS = [("Blue Cell 1", "#1E5AA8"), ("Blue Cell 2", "#2E7D32"), ("Blue Cell 3", "#8E24AA")]

OBJECTIVES = [
    # ref, type, points, description
    ("detect_c2", "detection", 40, "Detect the C2 beacon"),
    ("detect_lateral", "detection", 30, "Detect lateral movement to FS-02"),
    ("detect_exfil", "detection", 20, "Detect the data exfiltration"),
    ("write_report", "deliverable", 10, "Submit the incident report"),
]

MODULES = [
    ("Threat hunting fundamentals", "reading", 45),
    ("Network telemetry and the SIEM", "reading", 60),
    ("Lab: hunting the beacon", "scenario", 90),
    ("Exercise: VIGILANT MAPLE", "scenario", 120),
]

# Injects of this run: minutes after the start, timeline offset, action, status, detail, MITRE.
INJECTS = [
    (0.5, "0:00", "Phishing email delivered to FIN-WS-07", "fired", "user j.chen opened invoice.docm", "T1566.001"),
    (6, "6:00", "C2 beacon to northwind-update.example", "fired", "HTTPS beacon every 60 s from FIN-WS-07", "T1071.001"),
    (14, "14:00", "Credential dump on FIN-WS-07", "fired", "LSASS read by rundll32.exe", "T1003.001"),
    (22, "22:00", "Lateral movement to FS-02 over SMB", "fired", "service created remotely on FS-02", "T1021.002"),
    (29, "29:00", "Stage finance archive on FS-02", "failed", "agent unreachable on FS-02 (retry scheduled)", "T1074.001"),
    (35, None, "Instructor hint: review the proxy logs for FIN-WS-07", "fired", "sent to all teams", None),
]

# 14 Students.
# labs: (activity, state, heartbeat minutes ago or None, error, created minutes ago)
# attempts: (objective, verdict, minutes ago, events matched, on target, query)
STUDENTS = [
    {"first": "Amélie", "last": "Tremblay", "team": 0,
     "labs": [("mod_003", "active", 1, "", 38)],
     "attempts": [("detect_c2", "missed", 24, 120, 8, "url.domain:*northwind*"),
                  ("detect_c2", "achieved", 17, 14, 14, "url.domain:northwind-update.example AND event_type:http"),
                  ("detect_lateral", "achieved", 4, 9, 8, "event_type:smb AND destination.host:FS-02")]},
    {"first": "Jordan", "last": "Okafor", "team": 0,
     "labs": [("mod_003", "active", 2, "", 37), ("mod_004", "ready", None, "", 9)],
     "attempts": [("detect_c2", "achieved", 18, 16, 15, "dns.question.name:northwind-update.example"),
                  ("detect_lateral", "pending", 1, None, 0, "process.name:services.exe AND host.name:FS-02")]},
    {"first": "Priya", "last": "Raman", "team": 0,
     "labs": [("mod_003", "active", 0, "", 39)],
     "attempts": [("detect_c2", "achieved", 26, 15, 15, "url.domain:northwind-update.example"),
                  ("detect_lateral", "achieved", 15, 7, 7, "event_type:smb AND destination.host:FS-02"),
                  ("detect_exfil", "achieved", 6, 5, 4, "file.extension:7z AND host.name:FS-02")]},
    {"first": "Liam", "last": "MacDonald", "team": 0,
     "labs": [("mod_003", "failed", None, "clone timed out on esx-03 (datastore busy)", 36)],
     "attempts": [("detect_c2", "missed", 30, 300, 6, "event_type:http")]},
    {"first": "Sofia", "last": "Haddad", "team": 0,
     "labs": [("mod_003", "provisioning", None, "", 4)],
     "attempts": []},
    {"first": "Ethan", "last": "Nguyen", "team": 1,
     "labs": [("mod_003", "active", 1, "", 38)],
     "attempts": [("detect_c2", "missed", 28, 0, 0, "url.domain:northwind.com"),
                  ("detect_c2", "missed", 19, 412, 9, "event_type:http AND status:200"),
                  ("detect_c2", "missed", 9, 88, 12, "user_agent:*curl*")]},
    {"first": "Chloé", "last": "Gagnon", "team": 1,
     "labs": [("mod_003", "active", 2, "", 38)],
     "attempts": [("detect_c2", "achieved", 26, 15, 14, "url.domain:northwind-update.example"),
                  ("detect_lateral", "missed", 21, 0, 0, "event_type:rdp"),
                  ("detect_lateral", "missed", 16, 240, 3, "destination.port:445"),
                  ("detect_lateral", "missed", 11, 61, 2, "event_type:smb"),
                  ("detect_lateral", "missed", 5, 30, 4, "event_type:smb AND source.host:FIN-WS-07")]},
    {"first": "Marcus", "last": "Bell", "team": 1,
     "labs": [("mod_003", "active", 22, "", 37)],
     "attempts": [("detect_c2", "missed", 33, 950, 10, "event_type:dns")]},
    {"first": "Hana", "last": "Kowalski", "team": 1,
     "labs": [("mod_003", "ready", None, "", 38)],
     "attempts": []},
    {"first": "Daniel", "last": "Osei", "team": 1,
     "labs": [("mod_003", "active", 1, "", 36)],
     "attempts": [("detect_c2", "achieved", 10, 13, 13, "url.domain:northwind-update.example AND event_type:http")]},
    {"first": "Rebecca", "last": "Lindqvist", "team": 2,
     "labs": [("mod_003", "active", 3, "", 37)],
     "attempts": [("detect_c2", "achieved", 22, 15, 15, "url.domain:northwind-update.example"),
                  ("detect_lateral", "missed", 7, 44, 3, "destination.host:FS-02")]},
    {"first": "Tyler", "last": "Brennan", "team": 2,
     "labs": [("mod_003", "active", 0, "", 35)],
     "attempts": [("detect_c2", "missed", 14, 610, 11, "event_type:http AND host.name:FIN-WS-07")]},
    {"first": "Noor", "last": "Siddiqui", "team": 2,
     "labs": [("mod_003", "active", 1, "", 39)],
     "attempts": [("detect_c2", "achieved", 30, 15, 15, "url.domain:northwind-update.example"),
                  ("detect_lateral", "achieved", 20, 8, 7, "event_type:smb AND destination.host:FS-02"),
                  ("detect_exfil", "missed", 8, 70, 2, "file.size > 1000000")]},
    {"first": "Gabriel", "last": "Roy", "team": 2,
     "labs": [("mod_003", "active", 4, "", 34)],
     "attempts": []},
]

SCENARIO_YAML = """name: vigilant-maple
version: "1"
range_template: corp-finance-small
variables:
  c2_domain: northwind-update.example
timeline:
  - {t: "0:00", action: "Phishing email delivered to FIN-WS-07"}
  - {t: "6:00", action: "C2 beacon to northwind-update.example"}
  - {t: "14:00", action: "Credential dump on FIN-WS-07"}
  - {t: "22:00", action: "Lateral movement to FS-02 over SMB"}
  - {t: "29:00", action: "Stage finance archive on FS-02"}
  - {t: "45:00", action: "Exfiltrate the archive over HTTPS"}
objectives:
  - {id: detect_c2, type: detection, validator: opensearch_query, points: 40,
     params: {query: 'event_type:http AND url.domain:*northwind-update.example*', min_hits: 2}}
  - {id: detect_lateral, type: detection, validator: opensearch_query, points: 30,
     params: {query: 'event_type:smb AND destination.host:FS-02', min_hits: 1}}
  - {id: detect_exfil, type: detection, validator: opensearch_query, points: 20,
     params: {query: 'file.extension:7z AND host.name:FS-02', min_hits: 1}}
  - {id: write_report, type: deliverable, validator: deliverable_check, points: 10}
"""

TEMPLATE_YAML = "name: corp-finance-small\nenvironment: enterprise\n"


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _write_private(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _accounts() -> dict:
    """The demo sign-in accounts: kept across re-runs, so passwords do not change."""
    path = DEMO / "accounts.json"
    accounts = json.loads(path.read_text()) if path.is_file() else {}
    plain: dict[str, str] = {}
    for key, (username, role, first, last, _rank) in STAFF.items():
        if key in accounts:
            continue
        password = secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        accounts[key] = {
            "username": username, "email": f"{username}@demo.truenorth.test", "role": role,
            "display_name": f"{first} {last}", "keycloak_id": f"demo-{username}",
            "salt": salt, "pbkdf2_sha256": password_hash(password, salt),
        }
        plain[key] = password
    if plain:
        _write_private(path, json.dumps(accounts, indent=2) + "\n")
        creds = DEMO / "credentials.txt"
        old = creds.read_text() if creds.is_file() else ""
        lines = [old.rstrip("\n")] if old else [
            "TrueNorth live-class demo: test accounts (dev only, local SQLite, never a deployment).",
            "Sign in at the demo URL with the username (or email) and password below.",
        ]
        for key, password in plain.items():
            a = accounts[key]
            lines += ["", f"[{key}] {a['display_name']}", f"  username: {a['username']}",
                      f"  email:    {a['email']}", f"  password: {password}"]
        _write_private(creds, "\n".join(lines) + "\n")
    return accounts


def main() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///") or "/.demo/" not in url:
        sys.exit(f"refusing: DATABASE_URL must be the demo's own SQLite file under .demo/, not {url!r}")

    import app.main  # noqa: F401 -- registers every model on Base before create_all
    from app.course_releases.models import ACCEPTED, CourseRelease, CourseReleaseBlob
    from app.db import Base
    from app.detections.models import DetectionSubmission
    from app.enrollment import ensure_enrollment
    from app.lab_sessions import models as lab
    from app.models import (
        Course,
        CourseModule,
        EnrollmentStatus,
        Exercise,
        ExerciseState,
        ModuleContentType,
        Objective,
        ObjectiveType,
        Range,
        RangeState,
        Scenario,
        Team,
        TeamMembership,
        Template,
        Tenant,
        User,
        UserRole,
    )
    from app.scenario_runs.models import InjectRecord
    from app.scheduler.models import EventState, ScheduledEvent
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    now = datetime.now(UTC)
    started = now - timedelta(minutes=40)

    def ago(minutes: float | None) -> datetime | None:
        return None if minutes is None else now - timedelta(minutes=minutes)

    db.merge(Tenant(id=TENANT, name="Demo Training Centre", slug="demo-training-centre", is_active=True))
    db.flush()

    # Staff, from the sign-in accounts.
    accounts = _accounts()
    staff: dict[str, User] = {}
    for key, (username, role, first, last, rank) in STAFF.items():
        a = accounts[key]
        staff[key] = db.merge(User(
            id=fid("user", username), keycloak_id=a["keycloak_id"], email=a["email"], display_name=a["display_name"],
            first_name=first, last_name=last, rank=rank, role=UserRole(role), tenant_id=TENANT, is_active=True,
            unit="Demo Training Centre", onboarding_state="complete", onboarded_at=now - timedelta(days=30),
        ))
    db.flush()
    instructor, admin = staff["instructor"], staff["admin"]

    # Template, range, scenario, exercise (running, started 40 min ago) and its objectives.
    template = db.merge(Template(id=fid("template"), name="corp-finance-small", yaml=TEMPLATE_YAML, tenant_id=TENANT))
    db.flush()
    rng = db.merge(Range(id=fid("range"), name="VIGILANT MAPLE range", template_id=template.id, tenant_id=TENANT,
                         state=RangeState.running, provisioner_backend="mock",
                         description="Small finance enterprise: FIN-WS-01..12, FS-02, DC-01, proxy, SIEM."))
    scenario = db.merge(Scenario(id=fid("scenario"), name="vigilant-maple", yaml=SCENARIO_YAML, tenant_id=TENANT))
    db.flush()
    ex = db.merge(Exercise(
        id=fid("exercise"), name="VIGILANT MAPLE", kind="assessment", range_id=rng.id, scenario_id=scenario.id,
        state=ExerciseState.running, tenant_id=TENANT, started_at=started, completed_at=None,
        max_score=sum(p for _, _, p, _ in OBJECTIVES),
    ))
    db.flush()
    # Exercise-wide: an objective is achieved once anyone's detection on it was credited.
    first_credit = {}
    for st in STUDENTS:
        for ref, verdict, minutes, *_ in st["attempts"]:
            if verdict == "achieved":
                first_credit[ref] = max(first_credit.get(ref, 0), minutes)
    for n, (ref, kind, points, desc) in enumerate(OBJECTIVES):
        db.merge(Objective(achieved=ref in first_credit, achieved_at=ago(first_credit.get(ref)),id=fid("objective", ref), exercise_id=ex.id, ref_id=ref, objective_type=ObjectiveType(kind),
                           description=desc, validator="opensearch_query" if kind == "detection" else "deliverable_check",
                           points=points, created_at=started + timedelta(seconds=n)))

    # The course, its modules and an accepted release (what lab sessions are launched from).
    course = db.merge(Course(
        id=fid("course"), name="Cyber Threat Hunting (Intermediate)", tenant_id=TENANT, is_published=True,
        description="Hunt an intrusion end to end in a small finance enterprise: beacon, lateral movement, exfiltration.",
        difficulty="intermediate", duration_hours=24,
        course_meta=json.dumps({"learning_objectives": [d for _, _, _, d in OBJECTIVES]}),
    ))
    db.flush()
    for n, (title, kind, minutes) in enumerate(MODULES):
        db.merge(CourseModule(id=fid("module", str(n)), course_id=course.id, ordinal=n, title=title,
                              content_type=ModuleContentType(kind), duration_minutes=minutes, is_required=True))
    sha = hashlib.sha256(b"truenorth-live-class-demo-release").hexdigest()
    db.merge(CourseReleaseBlob(sha256=sha, size=1, data=b"x"))
    db.flush()
    # A release is immutable once accepted (app/course_releases/models.py): made once, then reused.
    release = db.get(CourseRelease, fid("release")) or db.merge(CourseRelease(
        id=fid("release"), tenant_id=TENANT, course_id=course.id, catalogue_code="CTH-201", arc2_code="ARC2-CTH",
        run_id="demo", slug="cyber-threat-hunting", title=course.name, version=1, release_digest=sha,
        learner_digest=sha, platform_digest=sha, instructor_digest=sha, blob_sha256=sha, meta="{}", state=ACCEPTED,
        created_at=now - timedelta(days=7), accepted_by=admin.id, accepted_at=now - timedelta(days=6),
    ))

    # The booking over now: the course, the exercise, the range, the demo instructor.
    db.merge(ScheduledEvent(
        id=fid("booking"), name="CTH-201 serial 3: VIGILANT MAPLE", tenant_id=TENANT, course_id=course.id,
        exercise_id=ex.id, range_id=rng.id, scenario_id=scenario.id, template_id=template.id,
        instructor_id=instructor.id, created_by=admin.id, state=EventState.active,
        start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=3),
        description="Live-fire hunt, teams of four to five.",
    ))

    teams = [
        db.merge(Team(id=fid("team", str(n)), name=name, tenant_id=TENANT, exercise_id=ex.id, color_hex=color,
                      team_type="blue", is_persistent=False))
        for n, (name, color) in enumerate(TEAMS)
    ]
    db.flush()

    # This run's injects (and nothing left over from an older seed).
    keep = [fid("inject", str(n)) for n in range(len(INJECTS))]
    db.query(InjectRecord).filter(InjectRecord.exercise_id == ex.id, InjectRecord.id.notin_(keep)).delete(
        synchronize_session=False)
    seq = 0
    for n, (after, t, action, status, detail, mitre) in enumerate(INJECTS):
        timeline = t is not None
        when = started + timedelta(minutes=after)
        db.merge(InjectRecord(
            id=keep[n], exercise_id=ex.id, range_id=rng.id, tenant_id=TENANT, run_id="demo-run-1",
            source="timeline" if timeline else "instructor", seq=seq if timeline else None, t=t, action=action,
            status=status, detail=detail, execution_mode="simulated", mitre_technique=mitre,
            telemetry_count=40 + 7 * n if status == "fired" else 0, telemetry_shipped=status == "fired",
            created_at=when, updated_at=when,
        ))
        seq += 1 if timeline else 0

    # The Students: assigned to the course, on a team, with labs and attempts.
    for s in STUDENTS:
        handle = f"{s['first']}.{s['last']}".lower().replace("é", "e")
        user = db.merge(User(
            id=fid("student", handle), keycloak_id=f"demo-student-{handle}", email=f"{handle}@demo.truenorth.test",
            display_name=f"{s['first']} {s['last']}", first_name=s["first"], last_name=s["last"], rank="Cpl",
            role=UserRole.student, tenant_id=TENANT, is_active=True, onboarding_state="complete",
            onboarded_at=now - timedelta(days=10),
        ))
        db.flush()
        enrollment = ensure_enrollment(db, user_id=user.id, course_id=course.id, tenant_id=TENANT, source="assigned")
        enrollment.status = EnrollmentStatus.in_progress
        enrollment.created_at = enrollment.enrolled_at = now - timedelta(days=9)
        enrollment.started_at = now - timedelta(days=8)
        db.merge(TeamMembership(id=fid("member", handle), user_id=user.id, team_id=teams[s["team"]].id,
                                joined_at=now - timedelta(days=1)))

        for activity, state, seen, error, created in s["labs"]:
            range_state = {"failed": RangeState.failed, "provisioning": RangeState.provisioning}.get(
                state, RangeState.running)
            lab_range = db.merge(Range(id=fid("lab-range", handle, activity), name=f"lab {handle} {activity}",
                                       template_id=template.id, tenant_id=TENANT, provisioner_backend="mock",
                                       state=range_state, error_message=error or None))
            db.flush()
            made = ago(created)
            ready = None if state in ("failed", "provisioning") else made + timedelta(minutes=3)
            since = {"failed": made + timedelta(minutes=6), "provisioning": made + timedelta(minutes=1)}.get(state, ready)
            heartbeat = ago(seen)
            db.merge(lab.LabSession(
                id=fid("lab", handle, activity), tenant_id=TENANT, user_id=user.id, release_id=release.id,
                activity_id=activity, attempt=1, state=state, error=error, profile_id="cth-201-hunt",
                profile_digest="0" * 64, backend="mock", range_id=lab_range.id, vcpu=8, ram_mb=16384,
                created_at=made, updated_at=since, provisioning_at=made, ready_at=ready, state_since=since,
                last_seen_at=heartbeat,
                idle_expires_at=(heartbeat or now) + timedelta(minutes=60) if ready else None,
                max_expires_at=made + timedelta(hours=4) if ready else None,
            ))

        mine = [fid("attempt", handle, str(k)) for k in range(len(s["attempts"]))]
        db.query(DetectionSubmission).filter(
            DetectionSubmission.exercise_id == ex.id, DetectionSubmission.user_id == user.id,
            DetectionSubmission.id.notin_(mine)).delete(synchronize_session=False)
        for k, (ref, verdict, minutes, matched, on_target, query) in enumerate(s["attempts"]):
            reason = None
            if verdict == "missed":
                reason = "no events matched" if not matched else f"precision {on_target / matched:.2f} below 0.50"
            db.merge(DetectionSubmission(
                id=mine[k], tenant_id=TENANT, exercise_id=ex.id, objective_ref=ref, user_id=user.id, query=query,
                submitted_at=ago(minutes), verdict=verdict, events_matched=matched or 0, on_target=on_target,
                threshold=2, min_precision=0.5, window_start=started, window_end=ago(minutes), reason=reason,
            ))

    db.commit()
    print(f"seeded: exercise {ex.id} (VIGILANT MAPLE, running since {started:%H:%M:%S} UTC), "
          f"{len(STUDENTS)} Students, {len(INJECTS)} injects")
    print(f"accounts: {DEMO / 'credentials.txt'}")


if __name__ == "__main__":
    main()
