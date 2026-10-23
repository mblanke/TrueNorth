# ADR 0014: Classes, roll-call onboarding and the two-view calendar

- **Status:** accepted 2026-10-22 (owner decisions, grilling session). **Amends ADR 0004** (see "What this changes in ADR 0004").
- **Date:** 2026-10-22
- **Code:** none yet. The work is sliced into GitHub issues, one PR each, listed under "Delivery". Slices that touch auth, tenancy or provisioning get an adversarial review.
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
4. **Booking approval:**
   - approve, reject, or propose a time;
   - nothing is sent or held before approval;
   - the roll-call change rules.
5. **Per-session ranges:**
   - individual or collective, with the course default and an instructor override;
   - kept up all week and torn down at the end;
   - balanced teams.
6. **Attendance and close-out.**
7. **My classes** (Student).
8. **Events view:** week and month, colours, the approval queue, following the mockups.
9. **Content clock:** review dates, theme tags, dependency edges, and the Content view.
10. **Feeds:**
    - release and end-of-life data, KEV and NVD;
    - online pull plus signed bundle import.
11. **Self-serve lab capacity gate.**
12. **Unit readiness page** (observer).
13. **AD status sync,** plus the 90-day disable for local accounts.
