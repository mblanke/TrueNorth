"""The Class record, its staff and its roll call (ADR 0014 §1, §2, §5, §8).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

- ``classes``: one running of a course for a named group ("C201-26-03"), in one tenant,
  with one lead instructor and start and end dates.
- ``class_staff``: its assistant instructors and observers (the lead is on the class row).
- ``class_roll_call``: the Students on it, by email. Being on it is approval to join
  (``onboarding.admit_from_roll_call``).
- ``class_sessions``: its sessions, each one a scheduler booking (``ScheduledEvent``) that
  needs an approver (ADR 0014 §14; ``scheduler/approval.py``), each *individual* (a range per
  Student) or *collective* (one shared range) (ADR 0014 §9; #282).
- ``class_course_range_modes``: a tenant's default mode for a course's class sessions.
- ``class_ranges``: the ranges a class owns (``ranges.py``): one per roll-call seat and
  template for individual sessions, one shared per template for collective ones. Kept from
  the first session that needs them to the last (ADR 0014 §10).
- ``class_session_teams``: a collective session's balanced teams (ADR 0014 §11), which are
  ordinary ``teams`` rows.
- ``class_attendance``: who was present at each session (ADR 0014 §12; ``attendance.py``):
  computed after the session ends, corrected by the lead or an admin.
- ``class_closeouts``: when a class was last closed and by whom, and its last reopening.
- ``class_results``: each Student's close-out result (ADR 0014 §13; ``closeout.py``): pass,
  fail or incomplete with a comment. Never a numeric grade.

Nothing here is ever deleted by the API (ADR 0010): a class is cancelled, a roll-call
entry is removed (``status = removed``), so who was invited and who signed in stays on
record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin


class ClassStatus(str, enum.Enum):
    open = "open"
    cancelled = "cancelled"
    # Closed out by its lead or an admin (ADR 0014 §13, ``closeout.py``): read-only, results
    # set; its ranges are torn down by the clock as for any class that is no longer open.
    # Only an admin reopens it.
    closed = "closed"


class StaffRole(str, enum.Enum):
    assistant = "assistant"
    observer = "observer"


class RollCallStatus(str, enum.Enum):
    invited = "invited"  # on the roll call; no TrueNorth sign-in yet
    signed_in = "signed_in"  # linked to an account that has signed in
    # No "ready": ADR 0014 defines none. A Student's rules of use and range are shown on My
    # classes from their own records (#284), not as a roll-call status (#319).
    # Added after the class's sessions were approved, and they had no room for one more
    # Student (ADR 0014 §17): waits for an approver. Not on the roll call until approved:
    # not linked, enrolled, emailed, invited or admitted.
    pending = "pending"
    removed = "removed"  # taken off by the lead or an admin; kept for the record
    # Not signed in by 2 hours after the class's first approved session started (ADR 0014
    # §4, ``forfeit.py``): off this roll call, seat freed, until the lead reinstates them.
    forfeited = "forfeited"


class RangeMode(str, enum.Enum):
    """How a class session's Students get a range (ADR 0014 §9)."""

    individual = "individual"  # every Student on the roll call gets their own small range
    collective = "collective"  # an exercise: everyone shares one range (today's one booking, one range)


# No course default set: a session is one shared range, as every booking was before #282.
DEFAULT_RANGE_MODE = RangeMode.collective


# Entries that are not (or not yet) on the roll call: no invites, no admission, no count.
OFF_ROLL_CALL = (RollCallStatus.removed.value, RollCallStatus.pending.value, RollCallStatus.forfeited.value)


class Class(TimestampMixin, Base):
    __tablename__ = "classes"
    __table_args__ = (Index("ix_classes_tenant_start", "tenant_id", "start_date"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    lead_instructor_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ClassStatus.open.value)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class ClassStaff(TimestampMixin, Base):
    __tablename__ = "class_staff"
    __table_args__ = (
        UniqueConstraint("class_id", "user_id", name="uq_class_staff_user"),
        Index("ix_class_staff_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class RollCallEntry(TimestampMixin, Base):
    """One Student on a class's roll call. ``email`` is stored lower-cased and trimmed:
    matching is case-insensitive. ``tenant_id`` is the class's, kept here so a match across
    every class is one indexed query."""

    __tablename__ = "class_roll_call"
    __table_args__ = (
        UniqueConstraint("class_id", "email", name="uq_class_roll_call_email"),
        Index("ix_class_roll_call_email", "email"),
        Index("ix_class_roll_call_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RollCallStatus.invited.value)
    added_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    onboarding_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why an addition waits for an approver (status pending): what did not fit.
    pending_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # When the seat was forfeited (ADR 0014 §4). Kept after a reinstatement, which is how the
    # clock knows never to forfeit that entry again; cleared only by a fresh addition.
    forfeited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the entry was last put on the roll call (added, approved, reinstated). Someone added
    # after the class started gets their own 2 hours from here. None (entries older than the
    # rule): the class's first approved session is the only clock.
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the Student was actually told to sign in (#319): the first onboarding or day-before
    # email the SMTP relay accepted. ``onboarding_sent_at`` / ``reminded_at`` only record that
    # one was due and claimed; with email off, or a send that failed, this stays empty.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When an in-app notification about this roll call was issued to the linked account. It
    # counts as telling them only once that account has signed in since (``forfeit.notified``).
    in_app_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RollCallSettings(TimestampMixin, Base):
    """A tenant's roll-call settings. ``email_domains``: the domains a roll call may add
    (lower-case, no @). No row: the domains of the tenant's active accounts' emails
    (``service.allowed_domains``)."""

    __tablename__ = "class_roll_call_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), primary_key=True)
    email_domains: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class ClassSession(TimestampMixin, Base):
    """One session of a class: the class's link to its scheduler booking (ADR 0014 §8, §14).

    A link table rather than a ``class_id`` on ``scheduled_events``: the scheduler stays
    ignorant of classes (ADR 0004 §3: it refers to other sections by id and reads them
    through their services), a booking is at most one class's session (``event_id`` is
    unique), and what belongs to the session as a class matter (individual or collective
    mode and teams, #282; attendance) gets a row to hang on. The booking's approval
    itself is the scheduler's (``scheduler_booking_approvals``)."""

    __tablename__ = "class_sessions"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_class_sessions_event"),
        Index("ix_class_sessions_class", "class_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("scheduled_events.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    # individual | collective (RangeMode), set when the session is booked: the course's
    # default unless the instructor chose otherwise. Changing it is a change of size.
    range_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_RANGE_MODE.value, server_default=DEFAULT_RANGE_MODE.value
    )


class CourseRangeMode(TimestampMixin, Base):
    """A tenant's default range mode for its class sessions of one course (ADR 0014 §9). Per
    tenant, because a shared catalogue course is taught by several. No row: collective."""

    __tablename__ = "class_course_range_modes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class ClassRangeKind(str, enum.Enum):
    seat = "seat"  # one Student's own range, for individual sessions
    shared = "shared"  # the class's one range, for collective sessions


SHARED_SEAT = "shared"  # ``seat_key`` of a shared range; a seat's is its roll-call entry id


class ClassRange(TimestampMixin, Base):
    """A range the class owns (ADR 0014 §9-10; ``ranges.py``). Built at the provisioning lead
    before the first session that needs it, kept across sessions, torn down after the last
    one, when the class is cancelled, or (a seat) when its Student is taken off the roll call.

    One row per (class, template, seat): built once and reused. ``range_id`` is the ordinary
    ``Range`` it is now, built and torn down through ``range_lifecycle`` (range operations,
    the worker's tasks, S5 teardown by recorded ids). A seat put back after its range was
    torn down gets a new range on the same row (``builds`` counts them)."""

    __tablename__ = "class_ranges"
    __table_args__ = (
        UniqueConstraint("class_id", "template_id", "seat_key", name="uq_class_ranges_seat"),
        Index("ix_class_ranges_range", "range_id"),
        Index("ix_class_ranges_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("templates.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("class_roll_call.id"), nullable=True)
    seat_key: Mapped[str] = mapped_column(String(64), nullable=False)
    range_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=True)
    builds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    torn_down_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    teardown_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Why the last build or teardown could not be started (a maintenance window, a refusal).
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ClassSessionTeam(TimestampMixin, Base):
    """A team of a collective session (ADR 0014 §11): an ordinary ``teams`` row, linked here
    to its session. Its exercise is set once the session has one."""

    __tablename__ = "class_session_teams"
    __table_args__ = (
        UniqueConstraint("team_id", name="uq_class_session_teams_team"),
        Index("ix_class_session_teams_session", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("class_sessions.id"), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("teams.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AttendanceStatus(str, enum.Enum):
    present = "present"
    absent = "absent"
    excused = "excused"


class AttendanceSource(str, enum.Enum):
    auto = "auto"  # computed by the clock from the evidence (``attendance.py``)
    instructor = "instructor"  # set by the lead or an admin; the clock never overwrites it


class ClassAttendance(TimestampMixin, Base):
    """One Student's attendance at one class session (ADR 0014 §12; ``attendance.py``).

    Keyed by roll-call entry, not account: a Student who never signed in has no account and
    is still absent. One row per (session, entry): the clock inserts it once, guarded by the
    unique constraint, and never overwrites one an instructor set."""

    __tablename__ = "class_attendance"
    __table_args__ = (
        UniqueConstraint("session_id", "entry_id", name="uq_class_attendance_entry"),
        Index("ix_class_attendance_class", "class_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("class_sessions.id"), nullable=False)
    entry_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("class_roll_call.id"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=AttendanceSource.auto.value)
    # The first evidence the Student opened their range during the session (auto rows).
    evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why the instructor set it (instructor rows).
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResultOutcome(str, enum.Enum):
    passed = "pass"
    failed = "fail"
    incomplete = "incomplete"


class ClassResult(TimestampMixin, Base):
    """A Student's result for a class, set by the lead at close-out (ADR 0014 §13).

    Pass, fail or incomplete and a comment, plus the attendance it was decided on. No score
    or grade column: an Unclassified deployment keeps pass/fail only (the Unclassified data
    inventory, ``result_transfer/dlp.py``), and the numbers stay where they are recorded."""

    __tablename__ = "class_results"
    __table_args__ = (
        UniqueConstraint("class_id", "entry_id", name="uq_class_results_entry"),
        Index("ix_class_results_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    entry_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("class_roll_call.id"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    suggested_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sessions_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sessions_present: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sessions_excused: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What this result did to the Student's enrolment (``completed``, ``failed`` or None:
    # nothing, e.g. one already completed some other way). A later close-out undoes only this.
    enrolment_change: Mapped[str | None] = mapped_column(String(16), nullable=True)


class ClassCloseout(TimestampMixin, Base):
    """When a class was last closed, and by whom; its last reopening (ADR 0014 §13). One row
    per class, kept when an admin reopens it (the audit log has every close and reopen). Its
    own table, so the ``classes`` row the earlier slices' migrations know stays as it was."""

    __tablename__ = "class_closeouts"

    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopened_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    reopen_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
