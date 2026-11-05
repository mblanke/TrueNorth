# 04 — Instructor guide: running a class end to end

For instructors (and the assistants and observers on their classes). It follows one class
from creation to close-out. The site must already be set up by an administrator
([03-admin-first-day.md](03-admin-first-day.md)). Your Students' side is in
[05-student-quick-start.md](05-student-quick-start.md).

**Before you start.** You sign in the same way the administrator does in
[03-admin-first-day.md](03-admin-first-day.md) §1: press **Sign in with SSO**, and on the
first sign-in Keycloak asks you to enrol a second factor (**Mobile Authenticator Setup** —
instructors need one by default, like admins, range ops and observers) and to sign the
Rules of use. Guide 03 §1 walks through it; this guide picks up after that.

Checked against `main` at `fd4f79a6` (2026-10-27, v1.2.0); sections 1 to 9 and the exercise
templates part of section 12 were reconciled with `main` at `353339c2` (2026-11-02), and
section 7's console parts with the source at `eec53906` (2026-11-05); sections 10, 11 and the
rest of 12 were not re-read end to end. The release under test is `v1.2.0-rc17`: apart from
section 7's console parts and the reconciliation edits of 2026-11-05 made to this guide, its
text is rc17's verbatim, and those console parts describe the source after rc17 — on rc17
itself a console opens as a one-time ticket from the hypervisor, not relayed through
TrueNorth. Words follow
[GLOSSARY.md](../GLOSSARY.md). UI paths are written as **sidebar section → item → card →
button**.

---

## The flow at a glance

```
Classes → New class ─► Roll call (paste or CSV) ─► Students emailed, sign in, sign Rules of use
        │
Schedule → Book a session (Session of a class) ─► awaiting approval ─► admin / range ops:
        │                                        Approve │ Propose time │ Reject
        ▼
Day before: reminders ─► 30 min before: ranges built ─► session: Students press Join
        │                 2 h after the first session starts: not signed in = Lost seat
        ▼
After each session: attendance recorded ─► last day: Close-out (pass / fail / incomplete)
```

### Who can do what on a class

| | Lead instructor | Assistant | Observer | Admin |
|---|:-:|:-:|:-:|:-:|
| See the class, roll call, sessions and ranges, attendance, close-out | ✓ | ✓ | ✓ (read-only) | ✓ |
| Change the roll call, book sessions, split teams, correct attendance, close | ✓ | | | ✓ |
| Reopen a closed class | | | | ✓ |
| Open class range passwords (range page) | ✓ | ✓ | never | ✓ |
| Open a console from a session's **Live** view (section 7) | ✓ | ✓ | only if the site turns it on | ✓ |
| See the **Unit readiness** page | ✓ | ✓ | ✓ | ✓ |

The menu you see as an instructor:

- **Learn:** Learning, Student progress
- **Create:** Authoring Studio
- **Prepare & Operate:** Exercises, Ops Center, Telemetry, Schedule, Calendar, Classes
- **Review:** Scoring & AAR, Unit readiness
- **Help:** Wiki, Support
- **Admin:** Teams, Infrastructure, AI Orchestrator

**Approvals** is no longer on an instructor's menu: only an administrator approves Request
access. Your Students come in through the roll call.

---

## 1. Create a class

**Prepare & Operate → Classes → New class**:

| Field | Notes |
|---|---|
| **Name or serial** | e.g. `C201-26-03` |
| **Course** | The course the class teaches |
| **Start date**, **End date** | The class's span. Ranges are kept up to the end of the last day. |
| **Lead instructor** | **Me**, or another instructor or admin |
| **Assistant instructors** | Optional |
| **Observers** | Optional: observers, instructors or admins |

Press **Create**. The class opens on its roll call.

A class can't be deleted, only cancelled. While the class is open, its lead (or an admin)
sees three buttons on its row in the Classes list:

- **Edit** reopens the form above (name, course, dates, staff) and saves with **Save**.
- **Hand over** asks for a **New lead instructor**; press **Hand over** to confirm.
- **Cancel class** asks "Cancel <class>? …" first. **Yes, cancel the class** cancels the
  sessions that have not started, tears the ranges down and sends the Students on the roll
  call a calendar cancellation. The class stays on record and it cannot be undone.

Under **Course**, the form shows "Sessions of this course default to **Individual** or
**Collective**" with the source ("set for your organisation" or "no default set"). If you may
author courses, a **Course default range mode** select there changes it. Each session can
still override it (section 5).

---

## 2. Roll call

**Classes → <class> → Roll call**. Classes is one page: a class has no page of its own to
click through to. Press **Roll call** on the class's card, and the class's **Roll call**,
**Attendance**, **Sessions and ranges** and **Close-out** cards all appear below the list —
they are hidden until a class is selected. (The calendar's **Roll call** link on a session
opens the same page with the class already selected.)

1. Paste the Students' email addresses into **Paste Students' email addresses** (one per line,
   or separated by commas), then press **Add**. Or use **upload a CSV** with an `email`
   column.
2. A summary line says what happened: added, already on it, refused (with the reason), not
   email addresses, not emailed, or awaiting approval.

**Being on the roll call is the approval.** A Student signs in with their AD account, and
their AD email is matched to the roll call. They go straight in as a Student of your tenant,
enrolled on the course. There is no Request access and no waiting.

**Onboarding emails.** A Student with no TrueNorth account yet is emailed "You are on the
roll call for <class>" when you add them. If they still haven't signed in, they get one
reminder the day before the class starts: "Reminder: <class> starts tomorrow".

- "The day before" is the day on the site's calendar (the site time zone: the
  `tn_site_timezone` the administrator set at install, else the host's own zone, else UTC).
  It can still look a day off if you are in another zone.
- You may send at most 100 such emails a day. Anything over that is listed as "not
  emailed".
- A Student you **remove and add again** is treated as new: emailed again, and only a
  notice sent after the re-add can cost them their seat.

> **If email is off** you see: "Email is not configured on this server: no email will be sent
> to the Students you add. Tell them to sign in to TrueNorth themselves." Do that, and get
> them signed in **before** the first session. A Student nobody could notify keeps their seat
> (section 6), but they still have to sign in to attend.

**Statuses:**

| Status | Meaning |
|---|---|
| **Invited** | On the roll call, not signed in yet |
| **Signed in** | Has an account and is admitted |
| **Awaiting approval** | Added after approval when the sessions had no room; an approver decides |
| **Lost seat** | Not signed in by 2 hours after the start (section 6) |
| **Removed** | Taken off. This includes an addition the approver rejected: the row then says "rejected: <reason>". |

A **Lost seat** row says "not signed in by <time>". A row that says "not notified — seat
kept" means the Student was never actually notified (section 6), so the seat was not taken.
There is no **Ready** status.

**Common refusals:**

- the address is not in your organisation's roll-call domains;
- it belongs to a deactivated or staff account ("a roll call holds Students only");
- the Student lost their seat ("reinstate them from the roll call instead").

If every add is refused, no domains are set yet: ask your administrator.

**Remove** takes a Student off at any time. Their own range is torn down and their seat
freed.

---

## 3. Students sign the Rules of use

Everyone signs the Rules of use on first sign-in, after each new version, and yearly. You
did too. Nothing works for a Student until they have signed.

The roll call shows **Signed in** once a Student has signed in. It doesn't show whether they
have signed the rules: that appears only on the Student's own **My classes** page
("Rules of use signed"). A Student stuck at sign-in usually hasn't scrolled to the end of
the rules: the **I agree** button stays disabled until they do.

---

## 4. Book the sessions (approval needed)

A class's **sessions** are its calendar bookings. Each one needs approval by an admin or
range ops. **Until it is approved nothing happens**: no capacity held, no range, no emails, no
invites.

**Prepare & Operate → Schedule → Book a session** (or **Calendar → Events → Book a
session**):

1. **Name**, **Range template**, **Day**, **From**, **To**. **Scenario** is optional: pick
   one when the session is to have a Live Class — choosing a Scenario here is what creates
   the exercise on the range when it is built (section 7). Without it the session has no
   exercise of its own; an exercise made by hand under **Exercises → New Exercise**
   (section 12) is made on a range you choose there, not on the class session's.
2. **Session of a class** → pick your class. Only the lead (or an admin) sees it. The course
   is filled in and locked, and the form says it needs approval.
3. **Ranges**:
   - **The course's default** (collective, unless an author set otherwise);
   - **Individual: a range per Student, from this template**;
   - **Collective: one shared range (an exercise)**.
4. **Ask for approval** (or **Save as draft**).

You can't book the course plainly while one of its open classes runs: "This course has an
open class then; book its sessions from the class (they need approval)".

**While it waits:** the calendar shows it with a **dashed outline**, and the details say
**Awaiting approval**. A session nobody decides on is cancelled when its end time passes.
**You are told of the decision.** When an approver approves, rejects or proposes another time,
the booking's instructor and the class's lead get a notification in the app (the bell in the
top bar), and an email as well when the site has email on. The approver is not told of their
own decision. The calendar shows the same.

**The approver's choices:**

| Decision | What you see | What you do |
|---|---|---|
| **Approve** | Solid block; Students on the roll call get invites | Nothing |
| **Reject** | "Rejected: <reason>" | Book again at another time, or talk to the approver |
| **Propose time** | "Proposed instead: <when>" | **Accept** (approved at that time) or **Decline** (your time stays, still awaiting approval) |

Find **Accept** and **Decline** on the session in **Schedule** or **Calendar → Events**.

**Changes after approval:**

- **Moving or resizing** an approved session sends it back for approval and withdraws the
  invites. **Calendar → Events → Move** changes only the start; **Schedule → Move or
  resize** also changes the length.
- **Adding a Student** goes through automatically while there is room. If there isn't, the
  Student shows **Awaiting approval** until an approver decides.
- **Removing a Student** is always allowed.
- **Changing individual/collective** counts as a change of size: it goes back for approval.

> A class session follows its class: only the lead (or an admin) sees **Move or resize**,
> **Accept** and **Decline**, on both the Schedule page and **Calendar → Events**. An
> assistant does not. A closed class's sessions change for nobody until an admin reopens it.

---

## 5. Individual or collective ranges, and teams

| | Individual | Collective (exercise) |
|---|---|---|
| Ranges | One small range per Student, from the template | One shared range for the class |
| Calendar colour | **Blue** | **Orange** |
| Size | Template size × Students; recomputed on every roll-call change | One range |
| Teams | None | Balanced teams, set by the lead |
| Live Class | No | Yes, through its exercise (section 7) |

Mix them across the week, e.g. individual labs Monday to Thursday and a collective exercise on
Friday. The course's default mode is set in the class form (section 1) by someone who may
author courses. With no setting it is **collective**.

**Ranges run for the whole class.** They are built **30 minutes (the default; the site can
change it) before the first session that needs them** and kept up between sessions, so Students carry on where they left off.
They are torn down after the class's last day, or earlier:

- when the class is cancelled or closed;
- when no session uses the range any more;
- for a Student's own range, when that Student is removed.

**Classes → <class> → Sessions and ranges** shows each session's mode and each range's state:
**Not built yet**, **Building**, **Ready**, **Failed**, **Tearing down** or **Torn down**.
Each range row also names its owner: the Student's name for their own range, "Shared (the
whole class)" for a collective one. A seat range's own name is "<class> seat <8 characters>"
where the 8 characters are the roll-call entry's id, not the Student's — so match a seat to
its Student by the row's owner here, and find the range in
**Create → Authoring Studio → Ranges** by that name.
**Retry** rebuilds a failed range; a failed seat holds up nobody else.

**Teams (collective sessions):**

1. **Sessions and ranges → Teams** on the session row.
2. Set **Teams** (2–26), then press **Split the roll call evenly**.
3. Move people with the select next to each name.

Teams are made when you press the button, not automatically. They can be changed only
**before the session starts**. Students with no account yet are listed as "Not placed (no
account yet)": split again once they have signed in.

---

## 6. The day before, and the 2-hour seat forfeit

**Reminders:**

- **24 hours before each approved session**, the instructor ("You are teaching…") and every
  Student on the roll call ("You are attending…") get an email with the calendar entry.
- **The day before the class starts**, Students still **Invited** get one more reminder to
  sign in.

All of this needs email. Without it, nothing is sent.

**Seat forfeit.** A Student still **Invited** (never signed in) **2 hours after the class's
first approved session starts** loses their seat, **but only if they were actually notified**.
The status becomes **Lost seat**, their seat is freed and their invites are withdrawn.

- *Notified* means the mail relay accepted their onboarding or day-before email, or they were
  sent an in-app notification and have signed in since.

- If a session was approved after it started, the 2 hours count from the approval. A
  Student added later counts from the next approved session.
- A Student nobody notified keeps their seat. That covers email off, a send the relay refused
  or failed, and the daily limit. The roll call shows "not notified — seat kept" past the
  deadline, so you know to get them signed in yourself or to **Remove** them.
- The lead and assistants (or the admins, if there are none) get one email per class:
  "<class>: N Student(s) lost their seat".
- The Student sees a banner: "You lost your seat… Ask your instructor to reinstate you."

**Reinstate:** **Roll call → Reinstate** on the row. One click, no confirmation. They are back
(or **Awaiting approval** if there is no room) and never forfeit again.

---

## 7. Running a session

**Before it starts:** check **Sessions and ranges** shows the ranges **Ready**. Tell the
Students to open **My classes** and press **Join** during the session.

**Students' accounts.** Join shows a Student their own range's accounts (or the shared
range's, only while a collective session is under way).

- **Console.** On **My classes** each class has a **Console** button beside **Join**. It
  lists **Your machines**, only the machines the Student is handed accounts on. Pressing a
  machine opens that machine's console on the page: a live picture of its screen, with the
  Student's keyboard and mouse. The picture is relayed through TrueNorth; the Student's
  browser never connects to the range's hosts, so the site needs no firewall route from
  Students to them (guide 01 §4.3). Consoles open **during the session, from shortly before it starts** (the same lead
  time as the range build), not before; a shared range only once the session is under way.
  Opening one is recorded and counts for attendance
  like pressing **Join**. A Student on a collective session reaches the shared range's
  Student machines, the same as every other Student there; teams have no machines of their
  own.
- A Student's range shows **paused** on My classes while its machines are stopped, stopping
  or starting. Staff pages still say **ready**. Join waits until it is running again. The
  machines are powered back on from **Create → Authoring Studio → Ranges** → the **Start**
  icon on the range's row (it appears once the machines are stopped; the row then says
  **starting** until they run, and the Student can Join). **Stop** and **Start** there are
  the API's `POST /ranges/{id}/stop` and `/start`, which instructors may call.

**Your view of a range.** Open it from **Create → Authoring Studio → Ranges** (click the range
name). **Credentials → Show accounts** lists every account; each password stays masked until
you reveal it, and every reveal is audited. The same page has the range's operations,
recovery and snapshots. Observers never see passwords.

**Live Class.** **Prepare & Operate → Ops Center**, or **Calendar → Events → <session> → Live
Class**. It works per **exercise**, so you get it for collective sessions that run one. On
the Calendar the **Live Class** button appears on every class session's details; on a session
with no exercise it opens an Ops Center page asking you to select an exercise instead.

- It shows **Needs attention**, the **Roster** and a Student drawer with **Reset lab…**.
- **Message** and **Extend lab time** are not available yet.
- Individual sessions have no exercise, so no Live Class: use **Sessions and ranges** and the
  range pages.

**Live view of a session (you, assistants, observers, admins).** On **Classes → <class> →
Sessions and ranges** each session row has a **Live** link, and so does the session's
details on **Calendar → Events**. It lists every Student on the roll call with their
machines (or their team and the shared range), the range's state, and **Last opened**
("Not yet" until they open it). It refreshes itself every 30 seconds. It is a status grid,
not screenshots.

- **Lead, assistants and admins** get a button per machine, for **any** machine of the
  session's ranges. The button opens the machine's console on the page (the same console
  Students get) and works only while the session is under way (or in the lead time before
  it). Every console opened this way is audited (who, which machine, and whether it was
  opened as a Student, observer or staff console). Staff consoles
  never count as the Student's attendance.
- **Observers** (the class's own observer list, not every observer in the tenant) see the
  same grid. **By default they get the live status only**: the page says "Observers see the
  live status only: consoles are off for observers on this site". The owner has not yet
  decided to allow observers to watch Students' screens. (The console relay now drops an
  observer's keyboard, mouse and clipboard input itself, so the setting is safe to turn on.)
  The setting is the environment variable `CLASS_OBSERVER_CONSOLES` on the API container, off
  by default. On an installer-built site it **cannot be turned on** in v1.2.0: the installer
  has no variable for it, and the compose file gives the API an explicit environment list
  that does not include it — setting it in the site's `.env` does not reach the container.
  Turning it on means editing the compose file's api environment by hand, which is a local
  change outside what the installer sets or supports. Where it has been turned on that way,
  an observer may open consoles on the **Students' machines only**; their console is
  view-only: keys, mouse and clipboard are not passed on.
- Observers never see range passwords.

Observers also see the calendar, roll call, sessions and ranges, attendance, close-out and
Live Class, all read-only.

---

## 8. Attendance (automatic)

A Student is **present** for a session if they opened their class range during it: in
practice, pressing **Join** on My classes, which loads their accounts. Opening counts from the
session's start up to its end. Attendance is recorded within a few minutes after each
session ends. A Student who lost their seat is absent.

**Classes → <class> → Attendance** (*"Present: signed in and opened their range during the
session."*):

- The lead or an admin corrects a cell with **P** (present) or **E** (excused). **Absent** is
  only ever recorded by the clock, so there is no **A** button; a cell the clock marked
  absent shows "Absent" until you correct it to **P** or **E**. Each correction is audited
  and marked "Set by an instructor"; the clock never overwrites it.
- **Note for the next correction (optional, audited)** (up to 500 characters) is sent with
  the next cell you correct; a cell with a note shows a note icon.
- Everyone else sees it read-only.

---

## 9. Close-out (pass / fail / incomplete with comments)

**Classes → <class> → Close-out**. Every Student on the roll call, plus those who lost their
seat, has a row:

- **Student**, **Attended**, **Course work**;
- **Detections** (not shown on **Unclassified** sites — whether scores appear at all depends
  on the site's classification level, `tn_classification_level`, set at install: guide 02 §5);
- **Suggested**, **Result**, **Comment** (up to 1000 characters).

**Suggested** is pre-filled:

- **Incomplete:** no session was held, attendance (present + excused) is below 80%, or
  graded course work isn't done;
- **Fail:** a graded module is below its pass mark;
- otherwise **Pass**.

1. Set **Result** for every Student (**Pass**, **Fail** or **Incomplete**). Add a
   **Comment**.
2. **Close the class**. There is no confirmation. It is refused while a session is building
   or under way.

What closing does:

- **Pass** completes the Student's enrolment, and so counts toward their qualification.
- **Fail** marks it failed.
- **Incomplete** leaves it open.
- The class becomes read-only. Unstarted sessions are cancelled and ranges torn down.

**Export CSV** downloads attendance and results. Any staff member on the class can, observers
included.

**Reopening** needs an administrator, with a reason (**Why reopen** → **Reopen**). Results
stay. Cancelled sessions and torn-down ranges do not come back.

On **Unclassified** sites: "Unclassified: pass or fail only, no scores." Comments are still
allowed.

**Unit readiness (for you, observers and admins).** **Review → Unit readiness** shows every
class of your organisation in a date range: the Students' enrolment, recorded attendance
(counts of present, excused and absent, never a percentage), result (Pass, Fail, Incomplete
or "Awaiting result", with your comment) and the qualification a pass earned. Filter with
**From**, **To** and **Unit** (the free-text unit on the Student's user record); export with
**CSV** or **PDF**, or **Print**. It is read-only and shows the caller's own tenant only.
Each export is audited. A qualification counts as earned only for a closed class whose
Student passed and whose enrolment completed; a pass for someone with no account or no
enrolment stays on the class only.

---

## 10. The Events and Content calendars

**Prepare & Operate → Calendar** has two tabs.

**Events:**

- **Today**, ‹ ›, **Week** | **Month**.
- **Blue** = class (a range for each Student), **orange** = exercise (one shared range).
- **Dashed outline** = awaiting approval; **dotted** = draft; struck through = rejected.
- **Grey stripes** = maintenance window.
- Each day has a load bar, e.g. "62% · 80% if approved". The lighter part is what pending
  bookings would add.
- The side panel has **Show** filters (Class, Exercise, Awaiting approval, Maintenance), the
  approval queue (approvers only), and the selected session's details with **Roll call**,
  **Live Class** and **Move**.

**Content:** every course, module, lab, scenario, range template and golden image on its
review-due date, coloured by theme, with the **Stale now** list.

- You can **Mark reviewed** items you own or have been assigned, and **Assign reviewer** on
  items you own.
- Everything else is read-only to you.

**Your own calendar app:** **Schedule → Subscribe in your calendar → Get calendar link**. The
link is shown once. A staff feed holds the whole tenant's approved bookings, not only yours.

---

## 11. Self-serve labs, and "Labs are busy, next free at HH:MM"

Self-serve modules have labs that a Student starts on demand, without a booking. On v1.2.0,
they start from the course's activity on the site's Moodle: one **Start lab: <module title>**
activity per lab module, created when the course is published to Moodle. There is no Start lab
button in TrueNorth itself.

> **Moodle is optional.** It is installed only when the installer's `tn_moodle_enabled` is
> true (false by default), and a course reaches it only when the course's accepted release is
> published to the site's Moodle. Until both, the course page has no **Open in Moodle**
> button and the Student has no Start lab to press. Ask your administrator whether the site
> has Moodle and whether the course has been published to it.

Self-serve labs only use the **capacity left over** after approved classes, your class's
whole span included, plus everything running now. When a lab doesn't fit, the Student sees:

> **Labs are busy, next free at HH:MM**

The time is in the site's time zone (set at install with `tn_site_timezone`; the host's zone
if empty, else UTC). A day is added when it isn't today. Common causes:

- an approved class (yours or another's) holds the capacity the lab would need;
- labs already running are counted until their maximum lifetime;
- the Student already has a lab running (one at a time), or other labs are queued first.

A pending booking holds nothing, so approving your class can make labs busy that weren't
before.

**Labs inside your class.** A lab of your course can draw on a session's capacity instead of
the leftover, and then ends with your class. This needs a **collective session with no range
template and no range** for the class's course. The Schedule form can't create one on v1.2.0
(it requires a template). The API can, and the class's **lead** may send it themselves (an
administrator can too) — it is the same booking the form makes, so it waits for approval like
any class session:

    POST /api/schedule/events
    {
      "name": "C201-26-03 lab window",
      "start_time": "2026-11-10T13:00:00Z",
      "end_time": "2026-11-10T17:00:00Z",
      "class_id": "<the class's id — GET /api/classes lists yours>",
      "range_mode": "collective",
      "vcpu_total": 32,
      "ram_mb_total": 131072,
      "disk_gb_total": 500
    }

No `template_id`, `range_id`, `exercise_id` or `scenario_id`: a collective session needs none
of them (only an individual session needs a template). The typed totals are the capacity the
session holds for the course's labs. The session holds that capacity from when it is approved
(scheduled), but a lab can draw on it only from shortly before the session's window starts —
within the scheduler's provisioning lead (the `SCHEDULER_PROVISION_LEAD_MIN` setting on the
API container, 30 minutes by default) — until the window ends, only within what the session
sized, and only for the class's own course. The endpoint is in the published contract
(`docs/interfaces/openapi.json`).

**Idle labs** close after **4 hours** without use, with an in-app warning 15 minutes before.
Students who only use Moodle don't see that warning. No lab lasts more than 7 days.

---

## 12. Exercises, scoring and the AAR

This is reference for the exercise side of a collective session, moved from the old user
guide.

**Exercise states:** `pending` → `running` ⇄ `paused` → `completed` (or `cancelled` before it
starts). Paused time still counts towards the scenario's duration.

- **Create:** **Exercises → New Exercise**, with **Name**, **Range** and **Scenario**.
- **Run:** **Start**, **Pause**, **Resume** and **Complete** in the Exercises list, or
  **Provision & Run (simulated)** on the exercise's page. It completes on its own when the
  scenario's duration has passed. Achieving every objective doesn't end it.
- **Injects:** every injector except the Greyspace breadcrumb writes **synthetic telemetry**,
  not real activity ([scenario-inject-execution.md](../scenario-inject-execution.md)).

**Objectives:**

- *Detection* objectives are achieved only by a Student's submitted query. The Student uses
  **Submit detection** on the exercise page. The query runs against the range's telemetry
  inside the exercise window and must find the attack (ADR 0005). The scenario's own query
  is the answer key and never earns credit.
- You can **Acknowledge** any objective on a running or paused exercise in **Scoring & AAR**,
  with optional evidence.
- There is one score per exercise: points earned out of points available. There are no
  rating bands and no team scores.

**AAR:**

- **Generate AAR** after completion, from the Exercises list or the exercise page. **Download
  AAR PDF** is in the list. It covers the exercise, scores, objectives with evidence,
  injects, timeline and participants.
- The AI analysis is optional, API only (`POST /exercises/{id}/aar/ai-enhance`), and needs an
  AI engine set up.

**Telemetry:** **Prepare & Operate → Telemetry** (staff and observers; not Students). Pick the
range, then search. Range telemetry comes from the scenario injectors (synthetic), the noise
engine and the ingest API; provisioning installs no sensor on range VMs.

### Search Syntax

The search box takes a small, closed language, not raw Lucene. Every term is ANDed:

| Form | Meaning | Example |
|---|---|---|
| `field:value` | exact value of one field | `event_type:dns_query` |
| `field:"two words"` | quoted value | `hostname:"ws 01"` |
| `field:prefix*` | value starts with (`*` only at the end) | `process_name:power*` |
| `field:*` | the field is present | `mitre_technique:*` |
| free text | words anywhere; `-word` excludes | `powershell -notepad` |

Refused (422): `OR` or `NOT` next to field terms, wildcards anywhere but at the end
(`*.corp.local`, `10.*.1`), regular expressions, fuzzy or proximity operators, and field
names starting with `_`. Common fields: `event_type`, `hostname`, `username`, `source_ip`,
`dest_ip`, `source_port`, `dest_port`, `protocol`, `action`, `severity`, `process_name`,
`mitre_technique`.

### Common Queries

| Purpose | Query |
|---|---|
| Phishing emails | `event_type:email_phish` |
| PowerShell execution | `event_type:process_exec process_name:powershell*` |
| DNS queries from one host | `event_type:dns_query hostname:ws-01` |
| New admin accounts | `event_type:identity_new_admin_user` |
| Brute-force sign-in attempts | `event_type:login_attempt` |
| Lateral movement over RDP | `event_type:lateral_move dest_port:3389` |
| Outbound HTTPS from one host | `event_type:http_request hostname:ws-01 dest_port:443` |
| Events mapped to ATT&CK | `mitre_technique:*` |
| High-severity events, any host | `severity:high` |
| Free text, excluding a word | `powershell -notepad` |

### Exercise templates

An exercise template holds the reusable part of a **collective** exercise: objectives, MESL,
teams, participant roles, sizing rules, your notes, and the scenario and range template it
runs on. Editing a template never changes exercises already made from it.

- **Exercises → Templates** lists them (**Template**, **Contents**, **Proven**), with **Open**
  and **Create exercise** on each row, and **New template**. A shared template shows a
  **Shared** chip.
- **Create exercise** asks for an **Exercise name** and the **Range**, which must be built
  from the template's range template. It makes a normal collective exercise with the
  objectives and MESL copied in.
- To start from an exercise you already ran, use the **Save exercise as template** icon on
  its row in the Exercises list, then **Save as template**. An individual exercise is refused.
- Deleting a template that exercises were made from is refused.
- **Proven** reads "Proven" or "Not proven: <reasons>": a range from the template's range
  template reached Certified as it is now, an exercise made from the template's current
  revision completed with a passing score, and the content clock does not call it stale.
  Only the platform administrator can port a proven template to another site (guide 06).
- Exercise templates are not attached to courses, which are individual training.

### Bookings without a class

A plain booking (no **Session of a class**) is still
self-service and needs no approval. While your own such booking is upcoming or running, you
can manage its Students and teams yourself:

- enrol and withdraw Students in **Learn → Student progress**;
- build the exercise's teams in **Admin → Teams** → **Exercise you run**.

Use a class instead whenever there is a named group of Students.

---

## Specialist pathways and streams (not for Students yet)

ADR 0018 and 0019 add 60 upskilling courses (C501 to C560) in eleven specialist pathways that
lead toward DP 3 to 5, and **Leadership** and **Technical** streams for DP 3, 4 and 5. They
are a **test fixture**: DP 3 to 5 and their qualifications are not defined. They exist as
catalogue rows and learning paths that the content loader creates **unpublished**, with no
qualification claim, so Students are not offered them. No screen built for them was found, and
this guide cannot point you to one. Do not promise Students these routes yet.

---

## Known gaps (tell your administrator if they bite)

- Observer consoles cannot be turned on at an installer-built site in v1.2.0: the installer
  has no variable for `CLASS_OBSERVER_CONSOLES` and the compose file does not pass it to the
  API container (section 7 has the detail). Observers get the live status grid only.
- Teams are split by hand, not automatically.
- A lab-drawing class session can't be booked from the Schedule form — it takes the API call
  shown in section 11.
- With email off, a Student who nobody could notify keeps their seat, so a Student who never
  signs in stays on the roll call until you **Remove** them.

---

## Printable checklist: running a class

```
Class: ____________________  Course: ____________________  Dates: ________ – ________

BEFORE
[ ] Classes → New class (name/serial, course, dates, lead, assistants, observers) → Create
[ ] Roll call: addresses pasted / CSV uploaded → Add; summary line read; refusals fixed
[ ] Email on? If not: Students told to sign in themselves, before the first session
[ ] Sessions booked: Schedule → Book a session → Session of a class → Ranges mode
    → Ask for approval
[ ] Every session approved (solid on Calendar → Events); proposals Accepted/Declined
[ ] Collective sessions: Sessions and ranges → Teams → Split the roll call evenly; moves done
[ ] Students told: My classes → Console opens their machines during the session

DAY BEFORE / DAY OF
[ ] Roll call: who is still Invited? Chase them before 2 h after the first session starts
[ ] Sessions and ranges: all Ready (Retry any Failed)
[ ] Students told: My classes → Join, during every session (that records attendance)
[ ] Lost seat? Roll call → Reinstate

EACH SESSION
[ ] Live Class open (collective sessions with an exercise)
[ ] Attendance checked afterwards; P / A / E corrections made

CLOSE-OUT (after the last session ends)
[ ] Close-out: Result + Comment set for every Student
[ ] Close the class
[ ] Export CSV filed
```
