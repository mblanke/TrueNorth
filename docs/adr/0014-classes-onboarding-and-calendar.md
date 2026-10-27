# ADR 0014: Classes, roll-call onboarding and the two-view calendar

- **Status:** accepted 2026-10-22 (owner decisions, grilling session). **Amends ADR 0004** (see "What this changes in ADR 0004").
- **Date:** 2026-10-22
- **Code:** sliced into GitHub issues, one PR each, listed under "Delivery". Slices that touch auth, tenancy or provisioning get an adversarial review. Built so far: Class and roll call (#278, `app/classes/`), the content clock (#286, `app/content_review/`), booking approval (#281: `app/scheduler/approval.py`, `app/classes/sessions.py`), per-session ranges and teams (#282: `app/classes/ranges.py`, `app/classes/teams.py`), attendance and close-out (#283: `app/classes/attendance.py`, `app/classes/closeout.py`).
- **Design reference:** the calendar mockups "TrueNorth Calendars" (Events and Content views, week and month), approved by the owner on 2026-10-22.

## Context

Onboarding today is one path for everybody:
1. sign in with AD;
2. fill in **Request access**;
3. wait for an instructor or admin to approve;
4. Students only: go through a first-run wizard.

A class of 12–15 Students therefore means 15 approvals plus 15 single enrolments.

There is no class object. A "class" is a scheduler booking (`ScheduledEvent`: course, range, instructor). Students are tied to the **course**, so two sections of one course in the same week cannot be told apart. The registration form's "cohort" field is free text with no link to anything.

Bookings are self-service, and capacity is the only check. Per-Student labs (`lab_sessions`) ignore the calendar entirely.

Content (courses, modules, labs, scenarios, range templates, golden images) has no review clock. Nothing flags it when a new ISO, distro release or vulnerability makes it stale.

## Decision

### Onboarding

1. **Roll call is approval.**
   - The lead instructor adds Students to a class **roll call**.
   - A Student on a roll call signs in with AD and goes straight in, with no Request access and no waiting. Their AD email is matched to the roll-call entry.
   - They become a Student in the class's tenant and are enrolled on its course.
   - Roll-call admission does not apply REGISTRATION_ALLOWED_GROUPS; the auth-zone check still applies.
2. **Onboarding email.** A roll-call Student not yet in TrueNorth gets an email to onboard when they are added. They get **one reminder the day before** the class starts.
3. **Acknowledgement form.** Everyone signs the rules of use before doing anything else: Students, instructors, admins and observers.
   - **When:** on first sign-in, whenever an admin publishes a new version, and **every year**.
   - **Record:** each signature records who signed, which version and when, in the audit log. Signatures are exportable.
   - **Observation notice:** the form says sessions may be observed, including live consoles (see 19).
4. **Seat forfeit.** A roll-call Student who has not signed in by **2 hours after the class starts** loses their seat.
   - It removes them from that class's roll call only.
   - The instructor is notified and can reinstate them with one click.
5. **Request access stays** for people who are on no roll call: new staff, observers, and Students taking self-serve modules. An admin approves them.
   - **A class is visible only to people on its roll call**, plus its instructors, observers and admins.
   - Nobody can browse classes or join one themselves.
6. **AD is read-only to TrueNorth.**
   - Only an AD admin disables or removes an AD account. AD itself disables accounts after 90 days without use.
   - A nightly check deactivates TrueNorth users whose AD account is disabled or gone. It frees their seats, notifies their instructors and is audited. It never deletes anything.
   - Accounts that aren't in AD are disabled after 90 days unused, **except break-glass**.
   - TrueNorth never writes to AD.
7. **Commander = observer + unit readiness page.** There is no new role.
   - Observers get a readiness page for their tenant: classes, enrolment, attendance, results and qualifications earned.
   - It can be filtered by unit and date, and exported.
   - The five roles stay as they are.

### Classes

8. **Class is a first-class record.** A class has:
   - a name or serial (e.g. "C201-26-03") and a course;
   - **one lead instructor**, **assistant instructors** and **observers**;
   - a roll call;
   - start and end dates;
   - its **sessions**, which are its calendar bookings.

   Only the lead changes the roll call and the bookings. The lead can hand the class to another instructor. Assistants see the roll call, Live Class and Students' ranges.
9. **Individual or collective, per session.**
   - In an *individual* session, every Student on the roll call gets their own small range.
   - In a *collective* session (an **exercise**), everyone shares one large range.
   - The course sets the default, and the instructor can override it per session, e.g. individual labs Monday to Thursday and a collective exercise on Friday.
10. **Ranges run for the whole class.** Ranges stay up from the first session to the last, so Students carry on where they left off. They are torn down after the last session or when the class is closed, whichever comes first.
11. **Teams.** For a collective session the roll call is split into balanced teams automatically. The instructor can move people between teams before the session.
12. **Attendance is automatic.**
    - A Student is present for a session if they signed in and opened their range during it.
    - The instructor can correct this (present or excused), and each correction is audited.
    - Attendance appears in results and exports.
13. **Close-out.** The lead instructor closes the class.
    - Results are pre-filled from scores and attendance. The lead sets pass, fail or incomplete for each Student, with a comment.
    - Results go to the Student's record and qualification.
    - A closed class is read-only and exportable. Reopening needs an admin and is audited.

### Booking approval and capacity

14. **Every class booking needs approval** by an admin or range ops.
15. **Nothing happens until approval.** No Student emails, no calendar invites, no capacity held. Capacity is checked when the approver acts.
16. **The approver can approve, reject, or propose a different time.**
    - The approval screen shows whether the booking fits, and the next free slot.
    - The instructor accepts or declines a proposed time.
    - Changing the class's size or its individual/collective mode stays the instructor's decision.
17. **Roll-call changes after approval.**
    - Removing a Student is always allowed. Their range is torn down and their seat freed.
    - Adding a Student is automatic while capacity allows. An addition that doesn't fit goes to the approver.
18. **Self-serve modules.** ARC² designs self-serve modules on cyber topics.
    - Their labs launch on demand through **Start lab**, without a booking.
    - They use only the capacity left over after approved classes. When none is free, the Student sees "labs are busy, next free at HH:MM".
    - Per-Student and per-tenant quotas still apply.
    - An idle lab is torn down after 4 hours.

### People on a class

19. **Observers on a class** see what instructors see, read-only: the schedule, roll call, Live Class, results, the after-action review, and **live screens and consoles**. They never see range login details.
20. **Students get a "My classes" page.** It covers all their classes and sessions across the week or month. It shows their status (onboarding, acknowledgement, range ready) and a **Join** button when their range is up. Email calendar invites continue as today.

### The two-view calendar (admin and instructor)

21. **Events view.**
    - **Layout:** classes and exercises in week and month views. **Today** jumps to the current period, and a Week | Month switch changes the view.
    - **Colours:** **blue = class (individual ranges)**, **orange = exercise (collective, shared range)**. A **dashed outline** means awaiting approval, and **grey stripes** mark a maintenance window.
    - **Capacity:** each day shows its load, plus the extra load if pending bookings were approved.
    - **Side panel:** the legend and filters, the approval queue (Approve / Propose time / Reject), and the selected booking's details.
22. **Content view.**
    - It covers **all** content: courses, modules, labs, scenarios, range templates and golden images, not only what ARC² builds.
    - Each item sits on its review-due date, in week or month view.
    - Each item is coloured by its **theme tag** (e.g. Blue team, Red team, DFIR, Network, ICS/OT, Cloud, Malware, Platform), and themes can be filtered.
23. **The content clock.** An item becomes due or stale in three ways:
    - **Time:** a minimum review every **90–120 days**. The default is 90; an admin can set up to 120 for each content type.
    - **Releases:** a new ISO or distro release, or an end-of-life date, for anything the item is built from.
    - **Vulnerabilities:** a known-exploited vulnerability (CISA KEV) or an NVD entry, either for software in its golden images or for a technique it teaches.
24. **Staleness flows down the dependency chain:** golden image → range templates that use it → courses and modules that use those ranges.
    - A stale item shows a **STALE** flag with its reason, and appears in a "Stale now" list.
    - **Mark reviewed** resets the clock. An item can also be assigned a reviewer, or retired.
25. **Feeds.** Where the site has internet, TrueNorth pulls the release and vulnerability feeds itself on a schedule, through TN-MGMT01 or the depot. Air-gapped sites get the same flags from a **signed feed bundle** an admin imports. Both go through the same pipeline. *(Accepted as recommended, owner "keep cooking", 2026-10-22.)*

## What this changes in ADR 0004

| ADR 0004 says | Now |
|---|---|
| Bookings are self-service, gated only by capacity | Every class booking needs approval; nothing is held or sent before it |
| Recipients are the course's active Students | The class roll call is the attendee list; course enrolment alone no longer makes someone an attendee |
| Attendees and recurrence are named as owned but are not built | Attendees = the roll call; a class's sessions replace recurrence |
| Labs are outside the scheduler | Self-serve labs only use capacity left over after approved classes |
| One booking, one range | A booking is a class session; ranges per Student (individual) or one shared range (collective), built from the roll call |

## Assumptions (not asked; correct them if wrong)

- An admin of the platform tenant edits the acknowledgement text, and it applies to every tenant. A per-tenant addendum can come later.
- Content reviewers are the content's owner (its author or lead instructor) plus admins. Instructors see the Content view read-only, except for items they own.
- Join codes are **not** built. Being added to the roll call by the instructor is the only way into a class.
- The 2-hour forfeit and the day-before reminder are fixed now. They can become admin settings later.

## Consequences

- **New records**, each in its own per-section module (ADR 0003):
  - the Class, its roll-call entries, instructors and observers;
  - class sessions, which link a Class to a `ScheduledEvent`;
  - per-session attendance;
  - acknowledgement versions and signatures;
  - content review state, theme tags and dependency edges;
  - feed snapshots.
- **Registration's free-text "cohort" field** stays for people who are on no roll call. For classes, the roll call replaces it.
- **Issue #272** (roster import, enrol a cohort) is folded into the Class and roll-call slice.
- **Issue #276** (invites reaching other tenants' Students) must be fixed first. The roll call becomes the invite list and is tenant-filtered.
- **Adversarial review** is needed for roll-call auto-approval, the AD status sync, observers watching live consoles, and the self-serve capacity gate. All of them touch auth, tenancy or provisioning.

## Delivery (one issue and one PR each, in this order)

1. **Class and roll call:**
   - the Class record, with lead, assistant and observer roles;
   - roll call by CSV or paste;
   - roll-call auto-approval on first sign-in;
   - the onboarding email plus the day-before reminder.

   This folds in #272 and is blocked by #276.
2. **Acknowledgement form:** versions, signatures, re-signing yearly and on every change, audit, export.
3. **Seat forfeit and reinstate.**
4. **Booking approval** (#281, built):
   - approve, reject, or propose a time;
   - nothing is sent or held before approval;
   - the roll-call change rules.

   As built: a class session is linked to its booking by `class_sessions` (classes section)
   and its approval lives in `scheduler_booking_approvals` (scheduler). New booking states
   `pending_approval` (holds nothing; the clock skips it and cancels it if nobody decides
   before it ends) and `rejected` (final). Approvers hold `schedule:approve` (admins, range
   ops). Approve and accept re-check under the booking lock; under the over-capacity
   policy `warn` an approval that does not fit goes through with audited warnings, as any
   booking does, and under `block` it is 409 with the next free slot. Moving or resizing an
   approved session sends it back for approval. Additions after approval are checked
   against a per-seat demand seam (`sessions.seat_demand`) that is zero until #282 builds
   per-Student ranges. Bookings with no class stay self-service, except that an
   instructor may not book a course plainly while one of its open classes runs (422: book
   it as the class's session); approvers may. Cancelling a class cancels its sessions that
   have not started. A Student's own sessions (`/schedule/mine`, their feed) and invites
   follow the roll call for class sessions; exercise and telemetry rosters followed course
   enrolment until #282, and follow the roll call since.
5. **Per-session ranges:**
   - individual or collective, with the course default and an instructor override;
   - kept up all week and torn down at the end;
   - balanced teams.

   As built (#282): the mode is on `class_sessions.range_mode`, set at booking (the tenant's
   default for the course in `class_course_range_modes`, else collective); changing it is a
   change of size. An individual session needs a range template and names no range; a seat
   costs the template's size (`sessions.seat_demand`), approval sizes the session from the
   roll call then, and an addition that fits grows it with no re-approval (an unchanged edit
   afterwards asks for the size it holds, so it does not re-pend either). A class's ranges
   (`class_ranges`, `app/classes/ranges.py`) are built by a step of the scheduler's clock at
   the provisioning lead before the first session that needs them, through the scheduler's
   own path (`range_lifecycle`, range operations, the worker's tasks), one operation per seat
   so a failed seat holds up nobody and is reported on the class. They are kept until the
   class's last day is over (drafts and day-by-day booking do not wipe the Students' work),
   and capacity holds them as one span per class, kind and template, from the first
   approved session's lead to the class's end (`app/classes/holds.py`): sessions inside it,
   and the ranges it builds, are counted once, and approving a session checks up to the
   class's end. Only the class's end is lease expiry (the `booking_ended` teardown a
   maintenance window never blocks); a Student taken off the roll call, a class cancelled
   or closed, or no session left that uses a range is a normal destroy a window defers.
   An individual session's size is recomputed from the roll call (seat size times
   Students) under the booking lock on every roll-call change. A collective session books its class's shared
   range. Its teams are ordinary `teams` rows linked in `class_session_teams`, split evenly
   from the roll call and moved by the lead before the start; the `/teams` endpoints treat
   them as the class's. Exercise, AAR and telemetry rosters of class sessions follow the
   roll call. A Student reads only their own seat's credentials (or the shared range's
   during a collective session); observers none.
6. **Attendance and close-out.**

   As built (#283): attendance is `class_attendance`, one row per (session, roll-call entry),
   recorded by the API's class clock after each approved session ends (guarded insert under
   the unique key, so replicas and re-runs record each Student once, and an instructor's row
   is never overwritten). The evidence is an audit row of the Student opening their own class
   range during the session, from its start to its end (exclusive): `class_range_open`,
   written when a Student loads that range's credentials (at most every 10 minutes), or
   `range_credential_reveal`. That one request proves both a signed-in session and the range
   opened; class ranges have no console, and a reveal alone misses later days of a week-long
   class. The lead or an admin sets present, absent or excused (audited). Close-out
   (`class_results`, `class_closeouts`) pre-fills attendance, course module progress (where quiz
   results land) and detections credited in the class's exercises, suggests incomplete below
   80% attendance, fail below the 70% pass mark, else pass; the lead sets each result with a
   comment. Pass completes the enrolment through `complete_enrollment`, fail marks it failed,
   incomplete leaves it open. On Unclassified nothing numeric is shown, exported or kept on the
   enrolment. A closed class (`classes.status = closed`) is read-only, its unstarted sessions are
   cancelled and its ranges torn down by the clock (`class_closed`); an admin reopens it with a
   reason. CSV export defuses formula cells.
7. **My classes** (Student).
8. **Events view:** week and month, colours, the approval queue, following the mockups.
9. **Content clock:** review dates, theme tags, dependency edges, and the Content view.
10. **Feeds:**
    - release and end-of-life data, KEV and NVD;
    - online pull plus signed bundle import.
11. **Self-serve lab capacity gate.**
12. **Unit readiness page** (observer).
13. **AD status sync,** plus the 90-day disable for local accounts.
