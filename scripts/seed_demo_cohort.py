#!/usr/bin/env python3
# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""Seed a demo cohort into the local dev stack: 150 Students across four formations, at
varied proficiency, with a class history that fills the readiness, competency and progress
pages.

The people and plan come from ``demo_cohort_roster.py`` (pure, deterministic). This script
turns them into requests against the API **in-process**, through the shared harness
(``_inprocess_api.py``): every write goes through the API's own routes, permission checks
and validation, as the named administrator, with no token or password. It runs inside the
api image, like the installer's content loader:

    make seed-demo-cohort          # after `make dev` and `make import-content`

What it makes:

1. 150 Students (``POST /users``), the formation in ``unit``: Cyber Command x120, Fleet
   Cyber East x10, Fleet Cyber West x10, Cyber Mission Assurance x10. Database rows only:
   no Keycloak account, no password, nobody signs in as them. The dev stack runs with
   ``AUTH_DISABLED=true``, so you browse as the admin.
2. NICE competency assertions for each Student around their band (``POST
   /competency/users/{id}/assertions``) on the NICE work roles (imported first; idempotent),
   which feed the competency heatmap.
3. Six classes of 25 on ALJQ courses (``POST /classes``, roll call by email): three closed
   with attendance and pass / fail / incomplete results, two in progress with some sessions
   held, one scheduled.

**The two backdated steps.** History cannot be made through the API alone today, so for a
class that has already started this script backdates two things, in-process, and nothing
else:

- *Its roll call.* A Student counts for a session only if they were on the roll call
  before it ended (``attendance.expected``, by ``joined_at``). Added today, they would owe
  no past session and no absence could ever be recorded, so their entries' ``joined_at``
  is set to a week before the class starts.
- *Its past sessions' approval.* The approver refuses a session that is already over
  (``scheduler.approval._admit``), and the clock lapses one nobody approved in time. For a
  session in the past, the approval module's clock reads an hour before the session while
  ``POST /schedule/events/{id}/approve`` runs, and is put back straight after. Everything
  else about the approval (conflicts, capacity, class open, audit) is checked as usual.

Then one clock pass (``POST /schedule/tick``) marks the past sessions completed. Present
marks are the lead's corrections (``PUT /classes/{id}/attendance/...``); absences are what
the app records itself: close-out for a closed class, and for an open one the same
``attendance.record_class`` the attendance clock and close-out run.

**Refuses outside the dev stack** (``TN_ENV`` development, the default, and
``AUTH_DISABLED=true``): this is demo data.

**Idempotent.** A Student whose email exists is not made again; a Student with any
competency assertion gets none added; a class whose name exists (and is not cancelled) is
skipped whole. Cancelling an open class (``DELETE /classes/{id}``) makes the next run
build it again; a closed class cannot be cancelled, so a full rebuild means a fresh dev
database (docs/development.md). The open classes' remaining sessions are recorded by the
live attendance clock as they pass, all absent (nobody opens a range), so seed shortly
before a demo.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import demo_cohort_roster as dc  # noqa: E402

# Course per class, by catalogue code (course names are "{code} — {title}"). All carry
# the ALJQ qualification, so a pass on a closed class earns it on the readiness page.
CLASS_COURSES = ("C101", "C103", "C201", "C204", "C206", "C209")
# Each class's sessions at its own hours (UTC), so one lead never double-books.
SESSION_SLOTS = ((13, 16), (17, 20), (13, 16), (13, 16), (17, 20), (13, 16))
LEAD_EMAIL = "instructor.demo@truenorth.test"  # made by `make import-content`; else the admin


class Seeder:
    def __init__(self, api, admin_email: str, today: date, seed: int, out=print) -> None:
        self.api = api
        self.admin_email = admin_email
        self.today = today
        self.seed = seed
        self.out = out
        self.failures = 0
        self.made = Counter()

    # ── helpers ──────────────────────────────────────────────────────────────

    def ok(self, status, what: str, detail=None) -> bool:
        good = isinstance(status, int) and 200 <= status < 300
        if not good:
            self.failures += 1
            self.out(f"  FAIL {what}  {str(detail)[:300]}")
        return good

    def _list(self, path: str) -> list:
        status, body = self.api.get(path)
        if status != 200:
            return []
        if isinstance(body, dict):
            body = body.get("items") or []
        return body or []

    # ── steps ────────────────────────────────────────────────────────────────

    def roll_call_domain(self) -> None:
        """Let roll calls take truenorth.test addresses: the tenant's allow-list (ADR 0014)."""
        status, body = self.api.get("/classes/roll-call-domains")
        if status != 200:
            self.ok(status, "read roll-call domains", body)
            return
        domains = body.get("email_domains") or []
        if body.get("source") == "setting" and not domains:
            return  # an empty setting allows any domain
        if dc.EMAIL_DOMAIN in domains:
            return
        status, body = self.api.put("/classes/roll-call-domains", json={"email_domains": [*domains, dc.EMAIL_DOMAIN]})
        if self.ok(status, "allow truenorth.test on roll calls", body):
            self.out(f"  roll-call domains now: {', '.join(body.get('email_domains') or [])}")

    def students(self, roster: list[dc.Student]) -> dict[str, str]:
        """email -> user id, making the Students that do not exist."""
        have = {u["email"].lower(): u["id"] for u in self._list("/users")}
        for s in roster:
            if s.email in have:
                continue
            status, body = self.api.post(
                "/users",
                json={
                    "email": s.email,
                    "display_name": s.display_name,
                    "role": "student",
                    "first_name": s.first_name,
                    "last_name": s.last_name,
                    "rank": s.rank,
                    "service_branch": s.service_branch,
                    "unit": s.unit,
                    "callsign": s.callsign,
                },
            )
            if self.ok(status, f"Student {s.email}", body):
                have[s.email] = body["id"]
                self.made["students"] += 1
        return have

    def competencies(self, roster: list[dc.Student], ids: dict[str, str]) -> None:
        # The NICE work roles (idempotent import). `make import-content` loads NICE tasks too,
        # which belong to no category; the assertions go on the roles the heatmap can place.
        from app.routers.competency import nice_category  # in-process: the API's own rule

        status, body = self.api.post("/competency/frameworks/import-nice")
        self.ok(status, "import NICE work roles", body)
        nice = self._list("/competency/frameworks?framework=nice&limit=500")
        by_code = {c["code"]: c["id"] for c in nice if nice_category(c["code"], c.get("category"))}
        if not by_code:
            self.ok(500, "NICE competencies", "none after import; skipping assertions")
            return
        for s in roster:
            uid = ids.get(s.email)
            if not uid:
                continue
            status, body = self.api.get(f"/competency/users/{uid}/profile")
            if status == 200 and body.get("total_competencies"):
                continue
            for code, level in dc.assertion_levels(s, list(by_code), self.seed).items():
                status, body = self.api.post(
                    f"/competency/users/{uid}/assertions",
                    params={"competency_id": by_code[code], "proficiency": level, "source": "demo-seed"},
                )
                if self.ok(status, f"assertion {s.email} {code}", body):
                    self.made["assertions"] += 1

    def lead_id(self, ids: dict[str, str]) -> str | None:
        users = {u["email"].lower(): u for u in self._list("/users")}
        lead = users.get(LEAD_EMAIL) or users.get(self.admin_email.lower())
        return lead["id"] if lead else None

    def courses(self) -> dict[str, str]:
        """catalogue code -> course id."""
        out = {}
        for c in self._list("/courses?limit=200"):
            code = str(c.get("name") or "").split(" — ", 1)[0].strip()
            out.setdefault(code, c["id"])
        return out

    # ── classes ──────────────────────────────────────────────────────────────

    def classes(self, plans: list[dc.ClassPlan], lead: str, courses: dict[str, str], backdate) -> None:
        # Cancelled ones are not listed, so a class cancelled after a failed run is made again.
        existing = {c["name"] for c in self._list("/classes")}
        for i, plan in enumerate(plans):
            name = plan.spec.name
            if name in existing:
                self.out(f"  skip class {name} (exists)")
                continue
            course = courses.get(CLASS_COURSES[i])
            if not course:
                self.ok(404, f"class {name}", f"course {CLASS_COURSES[i]} not loaded; run `make import-content`")
                continue
            self.one_class(plan, i, lead, course, backdate)

    def one_class(self, plan: dc.ClassPlan, i: int, lead: str, course: str, backdate) -> None:
        name = plan.spec.name
        status, cls = self.api.post(
            "/classes",
            json={
                "name": name,
                "course_id": course,
                "start_date": plan.start.isoformat(),
                "end_date": plan.end.isoformat(),
                "lead_instructor_id": lead,
            },
        )
        if not self.ok(status, f"class {name}", cls):
            return
        cid = cls["id"]
        self.made["classes"] += 1
        status, body = self.api.post(f"/classes/{cid}/roll-call", json={"emails": [s.email for s in plan.members]})
        if not self.ok(status, f"{name} roll call", body):
            return
        entries = {e["email"].lower(): e["id"] for e in body.get("added", [])}
        if plan.spec.phase != "scheduled":
            backdate_roll_call(cid, plan.start - timedelta(days=7))
        if len(entries) != len(plan.members):
            self.ok(500, f"{name} roll call", f"{len(entries)} of {len(plan.members)} added: {body.get('refused')}")

        # Sessions: book, then approve. A past one is approved as of an hour before it.
        start_h, end_h = SESSION_SLOTS[i]
        now = datetime.now(UTC)
        booked: list[tuple[str, datetime]] = []
        for n, day in enumerate(dc.session_days(plan), 1):
            start = datetime.combine(day, time(start_h), tzinfo=UTC)
            end = datetime.combine(day, time(end_h), tzinfo=UTC)
            if start <= now < end:
                continue  # never book one under way: the clock would build for it mid-seed
            status, evt = self.api.post(
                "/schedule/events",
                json={
                    "name": f"{name} — day {n}",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "class_id": cid,
                },
            )
            if not self.ok(status, f"{name} session {n}", evt):
                continue
            with backdate(start - timedelta(hours=1)) if end <= now else _nothing():
                status, body = self.api.post(f"/schedule/events/{evt['id']}/approve")
            if self.ok(status, f"{name} approve session {n}", body):
                booked.append((evt["id"], end))
                self.made["sessions"] += 1

        if plan.spec.phase == "scheduled":
            return
        # Past sessions: the clock completes them; then attendance.
        status, body = self.api.post("/schedule/tick")
        self.ok(status, "scheduler clock pass", body)
        status, grid = self.api.get(f"/classes/{cid}/attendance")
        if not self.ok(status, f"{name} attendance grid", grid):
            return
        held = [s for s in grid.get("sessions", []) if s.get("ended")]
        held.sort(key=lambda s: str(s.get("start_time")))
        rng_order = sorted(plan.members, key=lambda s: s.email)
        for s in rng_order:
            entry = entries.get(s.email)
            if not entry:
                continue
            for sess in _attended_sessions(held, plan.attended.get(s.email, 0), s.email, self.seed):
                status, body = self.api.put(
                    f"/classes/{cid}/attendance/{sess['session_id']}/{entry}",
                    json={"status": "present", "note": "demo seed"},
                )
                if self.ok(status, f"{name} attendance {s.email}", body):
                    self.made["present"] += 1

        if plan.spec.phase == "closed":
            results = [
                {
                    "entry_id": entries[s.email],
                    "outcome": plan.outcomes[s.email],
                    "comment": _comment(plan.outcomes[s.email], s.level),
                }
                for s in plan.members
                if s.email in entries
            ]
            status, body = self.api.post(f"/classes/{cid}/close", json={"results": results})
            if self.ok(status, f"{name} close-out", body):
                self.made["closed"] += 1
        else:
            self.made["absences"] += record_ended_sessions(cid)

    # ── all ──────────────────────────────────────────────────────────────────

    def run(self, backdate) -> int:
        roster = dc.build_roster(self.seed)
        plans = dc.plan_classes(roster, self.today, self.seed)
        self.out("Roll-call domain")
        self.roll_call_domain()
        self.out("Students")
        ids = self.students(roster)
        self.out("Competency assertions")
        self.competencies(roster, ids)
        self.out("Classes")
        lead = self.lead_id(ids)
        if not lead:
            self.ok(404, "lead instructor", f"neither {LEAD_EMAIL} nor {self.admin_email} exists")
        else:
            self.classes(plans, lead, self.courses(), backdate)
        self.print_summary(roster)
        return 1 if self.failures else 0

    def print_summary(self, roster: list[dc.Student], dry_run: bool = False) -> None:
        self.out("\nCohort (Students by formation and proficiency)")
        header = f"  {'unit':<26}" + "".join(f"{lvl:>13}" for lvl in dc.LEVELS) + f"{'total':>8}"
        self.out(header)
        for unit, counts in dc.summary(roster).items():
            self.out(
                f"  {unit:<26}" + "".join(f"{counts[lvl]:>13}" for lvl in dc.LEVELS) + f"{sum(counts.values()):>8}"
            )
        if dry_run:
            return
        made = ", ".join(f"{k} {v}" for k, v in sorted(self.made.items())) or "nothing (all there already)"
        self.out(f"\nMade this run: {made}")
        if self.failures:
            self.out(f"{self.failures} step(s) failed; see FAIL lines above.")
        else:
            self.out(
                "Browse with the no-login dev web build (docs/development.md, 'A demo cohort'):\n"
                "  cd control-plane/web && npx ng serve --port 4300 --proxy-config proxy.conf.json\n"
                "  http://localhost:4300: Readiness (filter by unit), Learning > Competency > Heatmap, Classes."
            )


@contextmanager
def _nothing():
    yield


def _attended_sessions(held: list[dict], n: int, email: str, seed: int) -> list[dict]:
    """Which ``n`` of the held sessions this Student attended: a stable pick per Student."""
    import random

    if n >= len(held):
        return list(held)
    rng = random.Random(f"{seed}:{email}:attended")
    return sorted(rng.sample(held, n), key=lambda s: str(s.get("start_time")))


def _comment(outcome: str, level: str) -> str:
    return {
        "pass": {"expert": "Outstanding; ready to mentor.", "advanced": "Strong, confident work."}.get(
            level, "Met the standard."
        ),
        "fail": "Did not meet the standard on the practical; retest advised.",
        "incomplete": "Missed too many sessions to assess.",
    }[outcome]


def record_ended_sessions(class_id: str) -> int:
    """Record the open class's ended sessions not recorded yet: what the attendance clock
    does within its look-back, and close-out does for the rest. In-process, as that clock."""
    from app.classes import attendance
    from app.classes.models import Class
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        cls = db.get(Class, uuid.UUID(class_id))  # tenant-safe: a class this script just made
        n = attendance.record_class(db, cls)
        db.commit()
        return n
    finally:
        db.close()


def backdate_roll_call(class_id: str, joined: date) -> None:
    """Put the class's roll call on it from ``joined`` (see the module doc). In-process."""
    from app.classes.models import RollCallEntry
    from app.db import SessionLocal

    at = datetime.combine(joined, time(12), tzinfo=UTC)
    db = SessionLocal()
    try:
        # tenant-safe: the entries of a class this script just made.
        db.query(RollCallEntry).filter(RollCallEntry.class_id == uuid.UUID(class_id)).update(
            {RollCallEntry.joined_at: at}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


@contextmanager
def backdated_approval(at: datetime):
    """The approver's clock reads ``at`` for the length of the block (see the module doc)."""
    from app.scheduler import approval

    real = approval._now
    approval._now = lambda: at
    try:
        yield
    finally:
        approval._now = real


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--admin", default="admin@truenorth.local", help="an existing platform admin to act as")
    ap.add_argument("--seed", type=int, default=dc.DEFAULT_SEED, help="roster seed (same seed, same people)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    args = ap.parse_args()

    today = datetime.now(UTC).date()
    if args.dry_run:
        roster = dc.build_roster(args.seed)
        seeder = Seeder(None, args.admin, today, args.seed)
        for plan in dc.plan_classes(roster, today, args.seed):
            units = Counter(s.unit for s in plan.members)
            print(
                f"  {plan.spec.name} {plan.spec.phase:<12} {plan.start}..{plan.end}  "
                + ", ".join(f"{u} {n}" for u, n in units.most_common())
            )
        seeder.print_summary(roster, dry_run=True)
        return 0

    from app.settings import env_flag, tn_env  # inside the api image: the app is importable

    if tn_env() != "development" or not env_flag("AUTH_DISABLED"):
        print("Refusing: this loads demo people, on the dev stack only (TN_ENV=development, AUTH_DISABLED=true).")
        return 2

    from _inprocess_api import connect  # inside the api image: the app is importable

    # The app logs every request at INFO (middleware, httpx, SMTP stand-in): thousands of
    # lines that bury the summary. Warnings and errors still show.
    logging.disable(logging.INFO)

    connected = connect(args.admin)
    if connected is None:
        return 2
    _, api = connected
    return Seeder(api, args.admin, today, args.seed).run(backdated_approval)


if __name__ == "__main__":
    sys.exit(main())
