# ADR 0004 — Scheduler is a self-contained module

- Status: proposed
- Date: 2026-10-04
- MOSA pillar: modular design, designated key interfaces, open standards
- Related: ADR 0001 (adapter registry), ADR 0002 (interface versioning),
  ADR 0003 (MOSA ratchet), ADR 0005 (CapacityService, being built separately)

## Context
Scheduling today is split across two places:

- `control-plane/api/app/routers/scheduling.py` (387 lines). It holds:
  - `ScheduledEvent` in the shared `models.py`;
  - the event lifecycle (create, activate, complete);
  - `/schedule/capacity`, `/schedule/check` and `/schedule/timeline`.
- Celery beat in `control-plane/worker/worker/celery_app.py`. It runs fixed periodic
  jobs (cleanup, health checks, metrics). It isn't user-facing and stays where it is.

Problems with today's capacity check (verified 2026-10-04):

1. **Supply is a guess.** Cluster size comes from env vars (`CLUSTER_TOTAL_VCPU=128`,
   `CLUSTER_TOTAL_RAM_MB=524288`, disk, 15% overhead). The dashboard reads discovered
   hosts instead, but vSphere discovery (vCenter REST) reports no host CPU or memory.
   On vSphere both numbers are therefore wrong.
2. **Demand is typed by hand.** A booking's `vcpu_total` and `ram_mb_total` are typed
   in, even though every range template declares its VM sizes
   (`specs: { cores, memory_mb, disk_gb }`).
3. **Commitments are incomplete.** Only overlapping bookings count. A range started
   without a booking consumes capacity the scheduler can't see. This is how the
   cluster gets overtaxed.

Problems with today's access control (verified 2026-10-04):

4. **The whole router is gated on `exercise:read`.** Students hold `exercise:read`, so
   they can list every event, read capacity and the timeline, and also create, edit,
   delete, activate and complete events. The write handlers carry a comment saying
   "stronger than the router-level read gate", but no stronger gate exists.
5. **Listing and creating ignore the caller's tenant.** `GET /schedule/events` returns
   every tenant's events. `POST /schedule/events` files the event under whichever
   tenant row the database returns first.

What users need:

- **Instructors** book a range or exercise for a class of Students at a time. They
  must be told before committing if it won't fit in vCPU, RAM or disk, and the range
  must be ready when the session starts.
- **Staff** see the calendar. Admins, Instructors, range-ops and observers can view it.
- **Students do not see the calendar** (decided 2026-10-04). They don't see other
  bookings, capacity, the timeline or anyone else's schedule. The one exception: a
  Student may get their *own* sessions, and nothing else, in a personal feed (slice 5).

## Decision
1. **One package:** `control-plane/api/app/scheduler/`. It contains `models.py`,
   `schemas.py`, `service.py` and `router.py`. Later slices add `ics.py` and
   `calendar_backends/`.
   - It **absorbs** `routers/scheduling.py` and `ScheduledEvent`. The old router is
     deleted once the new one serves the same paths.
   - New tables and schemas never go into the shared `models.py` or `schemas.py`,
     whose line counts are ratcheted.
   - Other sections reach scheduler data only through `scheduler.service`. For
     example, range deletion asks `service.reserving_event_count()` and
     `service.detach_range()`; it does not query `scheduled_events` itself.
2. **What it owns:** bookings, their lifecycle, recurrence, attendees (Instructors and
   the class they teach), the calendar feed and invites, and reminders.
3. **What it does not own:**
   - ranges, exercises, templates, enrollment and courses. It refers to these by id
     and reads them only through their services and APIs;
   - capacity. It asks `CapacityService` (ADR 0005) and never reads hypervisor
     tables or computes host totals itself.
4. **Capacity rules come from CapacityService:**
   - Demand is derived from the template's VM specs, not typed in.
   - Committed capacity = overlapping bookings **plus** running ranges that have no
     booking.
   - Supply = discovered vSphere host totals minus headroom, under a configured
     overcommit policy (vCPU 4:1 against physical cores; RAM and disk 1:1). The env
     values are only a fallback.
   - The dashboard gauge and the booking check use the same service, so they never
     disagree.
5. **Time-based actions: the API keeps the clock, the worker does the work** (revised
   2026-10-05; see the decisions log). `scheduler/clock.py` ticks once a minute
   (`SCHEDULER_TICK_SECONDS`) and:
   - provisions the booked range `SCHEDULER_PROVISION_LEAD_MIN` (30) before the start;
   - activates the booking at the start;
   - completes it `SCHEDULER_TEARDOWN_GRACE_MIN` (15) after the end, and tears the
     range down;
   - emails the Instructor `SCHEDULER_REMINDER_LEAD_MIN` (1440) before the start
     (0 turns reminders off).

   The rules for that work:
   - **The worker builds and tears down.** Provision and teardown are the existing
     contracted worker tasks (`provision_range`, `destroy_range`), sent through
     `celery_client.dispatch()` by `app/range_lifecycle.py`. That module is the one
     guarded path the ranges router uses too. The scheduler never imports worker code
     and never writes range tables.
   - **It only tears down what it built.** A range that was already up is used as it
     is and left up. A range still being built when its booking ends or is cancelled is
     torn down by a later tick, once it can be.
   - **The next booking of a range inherits it.** When a booking finishes and another
     live booking of the same range exists (the next class in a series), ownership
     passes to that booking and the range stays up. Only the last booking tears it
     down. Destroyed ranges cannot be built again, so tearing down between sessions
     would strand the next one.
   - **It is safe to run anywhere.** Every step is claimed with a guarded update, so
     the clock can run in every API replica at once. `SCHEDULER_CLOCK_ENABLED=false`
     turns it off, and `POST /schedule/tick` (`schedule:admin`) runs one pass by hand.
   - **No booking-specific worker tasks or `WORKER_TABLES` entries are needed.**
6. **Calendar standard: iCalendar (RFC 5545).** Two delivery paths in v1, neither
   needing any Microsoft-side setup:
   - **Subscription feed:** `GET /api/v1/schedule/feed/{token}.ics`. Read-only and
     per user. Works with Outlook's "Subscribe from web", Apple Calendar,
     Thunderbird and Google.
   - **Emailed invites:** `METHOD:REQUEST` with attendees, sent through the existing
     SMTP notification channel. Outlook shows Accept and Decline. An update keeps the
     same `UID` and raises `SEQUENCE`; a cancellation sends `METHOD:CANCEL`.
7. **External calendar sync** sits behind `scheduler/calendar_backends/`:
   - an ABC, a registry and a `null` backend, selected by `CALENDAR_BACKEND`;
   - in v1 only `null` exists. Planned later: `microsoft_graph`, for two-way sync,
     instant updates and rooms. It needs an Entra app registration in the tenant;
   - the seam is added to `tests/contracts/test_adapter_contracts.py`.
8. **Times are stored in UTC** (`DateTime(timezone=True)`) and emitted in UTC in ICS
   (`DTSTART:20261015T130000Z`). The display time zone is a client concern.

## Interfaces

| Interface | Contract | Check |
|---|---|---|
| HTTP API | `docs/interfaces/openapi.json`, paths `/api/v1/schedule/...` | `scripts/export_openapi.py --check`; `npm run check:api` |
| Capacity | `CapacityService` (ADR 0005): `supply`, `demand_for_template`, `committed`, `fits` | unit tests in the capacity module |
| Worker jobs | `worker/worker/contracts.py`: `provision_for_booking`, `teardown_for_booking`, `send_booking_reminder` (names to confirm) | `scripts/export_task_contracts.py --check`; `tests/contracts/test_task_contracts.py` |
| Calendar sync | `BaseCalendarBackend` | `tests/contracts/test_adapter_contracts.py`, seam `calendar` |
| Calendar format | iCalendar RFC 5545 (`VEVENT`; `METHOD:PUBLISH` for the feed, `REQUEST`/`CANCEL` for invites) | `tests/scheduler/test_ics.py`: parses, stable `UID`, `SEQUENCE` rises on update, UTC times |
| Frontend | generated `core/services/scheduler-api.service.ts` | ratchet `web_features_direct_httpclient` = 0 |

## Booking rules

- **Capacity:** a booking that doesn't fit gets a reason for each resource ("RAM: need
  96 GB, 40 GB free 13:00–16:00"). What happens next is an admin-set policy
  (decided 2026-10-04: "admin can hard block"):
  - `block`: the booking is refused with 409 for everyone. There is no per-booking
    force flag.
  - `warn`: the booking is created, the response carries the warnings, and the
    warning is audit-logged.

  Only admins (`schedule:admin`) can change the policy, through `PUT /schedule/policy`,
  and each change is audit-logged. The default is `block`. The policy is platform-wide,
  not per tenant: every tenant books against the same cluster, so one tenant's `warn`
  would overbook everyone else.
- **Conflicts:** the same range, or the same Instructor, double-booked in an
  overlapping window is refused with 409. Conflicts are always refused; the
  over-capacity policy does not apply to them.
  - A range is held for its lead and grace as well, so two bookings of one range need
    lead + grace (45 minutes by default) between them.
  - An Instructor is held for the session only, so back-to-back sessions are fine.
  - The Instructor is the one named (an active instructor or admin in the tenant),
    otherwise the caller when they are an instructor.
  - A booked range must belong to the caller's tenant.
  - Booking checks and writes run under a Postgres advisory lock, so two concurrent
    bookings cannot both pass.
- **Lead time:** provision `SCHEDULER_PROVISION_LEAD_MIN` before the start
  (default 30); tear down `SCHEDULER_TEARDOWN_GRACE_MIN` after the end (default 15).
  The lead time counts as committed capacity.
- **Tenancy:** every booking belongs to the creator's tenant, and every query is
  tenant-scoped (`get_owned`, and a tenant filter on lists). Capacity is the one
  cross-tenant figure, because tenants share the cluster. It reports totals only,
  never another tenant's bookings.
- **Permissions** (`rbac.py`):

  | Permission | admin | instructor | range_ops | observer | student |
  |---|---|---|---|---|---|
  | `schedule:read`: calendar, events, capacity, timeline | yes | yes | yes | yes | **no** |
  | `schedule:write`: create, edit, cancel, state changes | yes | yes | no | no | no |
  | `schedule:admin`: set the over-capacity policy | yes | no | no | no | no |

  A Student's own-sessions feed (slice 5) is authorised by its feed token. That token
  is issued only for that Student's own bookings, so it never grants `schedule:read`.
- **Lifecycle:** `draft → scheduled → provisioning → active → completed`, with
  `cancelled` reachable from any state before `completed`. A state change is a
  guarded update, so a retry can't move a booking twice.
  - Asking for the current state is a no-op, so retries are safe.
  - Only `draft` and `scheduled` bookings can be edited.
  - A draft holds nothing. It is checked for conflicts and capacity at
    `POST .../schedule`.
  - `DELETE` cancels; it no longer hard-deletes. Bookings are history.
  - Every create, edit and move is audit-logged.

## Outlook and calendar-client notes

- **Who fetches the feed:**
  - Outlook on the web and the new Outlook fetch subscriptions **from Microsoft's
    cloud**, so the feed URL must be reachable from the internet.
  - Classic desktop Outlook fetches from the user's own PC, so internal URLs work
    there.
  - On an air-gapped or protected network, rely on emailed invites, or later on a
    Graph backend running inside that tenant.
- **Refresh:** Outlook re-fetches subscriptions on its own schedule, typically every
  few hours. Changes that must be seen immediately also go out as an updated invite.
- **Feed security:** calendar clients can't log in to Keycloak, so the feed URL
  carries a long, random, per-user token.
  - It can be revoked and regenerated from the user's profile.
  - Only a hash of it is stored.
  - It is never logged:
    - the API request log and the uvicorn access log redact it (`middleware.redact_path`);
    - both nginx configs skip access logging for `/api/(v1/)?schedule/feed/`;
    - OpenTelemetry excludes the path.

    Reverse proxies outside this repo must do the same.
  - The URL contains no personal data.
  - The feed itself contains only what that user may see:
    - staff see their tenant's bookings;
    - a Student sees only their own sessions, with no other Students' names.

    The scope is checked against the token owner's role on every fetch, so a role
    change takes effect at the next refresh.
- **HTTPS only.** `webcal://` links are the same URL with the scheme swapped, offered
  as a one-click subscribe button.

## Migration

- An Alembic migration moves `scheduled_events` into the scheduler's tables (or keeps
  the table name; decide during implementation). It also drops the hand-typed
  `vcpu_total`, `ram_mb_total` and `disk_gb_total` in favour of a template reference.
- **Existing `/schedule/*` paths:** they keep working. If any response shape changes,
  publish the change in `openapi.json` and update the Angular client in the same PR.
- **Retired env vars:** `CLUSTER_TOTAL_*` become CapacityService fallbacks, used only
  when discovery has no data, and the response says which source was used.

## Consequences

- Scheduling logic leaves the shared router layer, and `ScheduledEvent` leaves
  `models.py`, lowering the `lines_api_models_py` ratchet.
- Students lose the schedule access they have today (problem 4 above). The dashboard
  hides the schedule panel for them.
- Bookings are checked against real vSphere capacity and against unbooked running
  ranges, which closes the overtax gap.
- Any standards-compliant calendar client can subscribe with no integration work.
- Adding Outlook two-way sync later is one backend file plus one registry line.
- **Risks:**
  - recurrence rules (`RRULE`) and exceptions are easy to get wrong. v1 supports
    simple weekly recurrence only;
  - time-zone edge cases around DST;
  - feed tokens are bearer secrets and must be treated as such.

## Delivery slices (one PR each, from `main`, `bash scripts/dod.sh` green)

1. Package skeleton. Move `routers/scheduling.py` and `ScheduledEvent` in, and route
   range deletion through the service. Add `schedule:read` and `schedule:write` and
   tenant-scope list and create (problems 4 and 5). Paths and response shapes are
   unchanged, and the dashboard hides the schedule panel from Students.
2. Capacity. The booking check, `/check`, `/capacity` and `/timeline` ask a
   `CapacityProvider` (`scheduler/capacity.py`), the seam that ADR 0005's
   CapacityService implements.
   - Until 0005 lands, `EnvCapacity` serves today's numbers: env totals, bookings
     only, and `supply_source: "env"`.
   - Demand comes from the template's VM specs (`range_topology.template_demand`). A
     contract test checks it against `worker/render.py` for every shipped template.
     Typed totals remain only for bookings that name no template.
   - The lead time and teardown grace count as committed capacity.
   - Refusals give one reason per resource and the window.
   - Over-capacity policy: `GET`/`PUT /schedule/policy`, stored in `scheduler_settings`.
3. Lifecycle, guarded state transitions, conflicts (done: `scheduler/lifecycle.py`,
   `service.conflicts`, migration `b5c6d7e8f9a0`).
4. The clock: provision lead, activation, teardown, reminders (done: `scheduler/clock.py`,
   `app/range_lifecycle.py`, migration `c6d7e8f9a0b1`).
5. ICS feed with per-user tokens (done: `scheduler/feed.py`, `scheduler/ics.py`,
   migration `d7e8f9a0b1c2`).
   - Endpoints: `GET`, `POST` and `DELETE /schedule/feed-token`, and the public
     `GET /schedule/feed/{token}.ics`.
   - The controls sit in the dashboard's schedule panel for now. A profile page waits
     for the GUI redesign (mockups first).
   - Staff only. Students' own-sessions feed waits for bookings to know their
     attendees.
6. Emailed invites (`REQUEST`/`CANCEL`) through the SMTP channel (done:
   `scheduler/invites.py`, plus a `text/calendar; method=…` part in
   `notifications/smtp.py`).
   - `REQUEST` goes out when a booking is created or scheduled, and again when it
     moves (same `UID`, higher `SEQUENCE`).
   - `CANCEL` goes out when it is cancelled, or to the previous Instructor when it
     changes hands.
   - Recipients are the Instructor only, until attendees exist.
   - The organizer is `SCHEDULER_ORGANIZER_EMAIL`, else `SMTP_FROM`. Replies go to
     that mailbox and are not read back.
   - Sending is a background task and best effort.
7. `calendar_backends/` seam with `null`, plus contract tests. `microsoft_graph` later.
8. Scheduler UI: calendar view, capacity bar per time slot, subscribe button.

## Decisions log

- 2026-10-04 — Calendar visibility: everyone except Students. Assumed (not yet
  confirmed): Students still get their own sessions in a personal feed.
- 2026-10-04 — Over-capacity: an admin-set policy, `block` (default) or `warn`. This
  replaces the per-booking admin force flag.
- 2026-10-05 — The policy is platform-wide, not per tenant (shared cluster).
- 2026-10-05 — Slice 2 shipped ahead of ADR 0005 behind `CapacityProvider`, so
  0005 replaces one function (`get_capacity_provider`) and nothing else.
  - Still 0005's: vSphere supply, the overcommit policy, and counting running ranges
    with no booking.
  - Found while building it: four shipped templates declare a `vm_count` that
    differs from what they build (soc-training 25 vs 23, cloud-security 20 vs 16,
    red-team 30 vs 28, large-enterprise 54 vs 50). Demand uses what is built.
- Moot: whether Students can see other Students' names. They can't see the calendar,
  and their own feed omits other attendees.

- 2026-10-05 — The clock lives in the API instead of in new worker tasks.
  - **The ADR's original plan didn't work yet.** Worker tasks reading
    `scheduled_events` need either the generated table mirror (`WORKER_TABLES`, not
    yet on main, PR #6) or raw SQL, which the MOSA ratchet forbids. Booking-specific
    tasks would also have duplicated range-state logic that already lives in the API.
  - **The API owns the rules, and the worker still does all hypervisor work** through
    the existing contracts.
  - **Booking-to-range rules:**
    - A booking with no `range_id` (template only) moves through its states but
      builds nothing. Whether a booking creates its range or exercise is still open.
    - Ranges that are `destroyed` cannot be provisioned again (`_RANGE_TRANSITIONS`),
      so a range booked weekly must not be torn down between sessions. The clock
      only tears down ranges it built from `created` or `failed`.
  - **Reminders go to the Instructor only, by email**, until the Student-feed question
    is answered. They are at most once: the booking is marked before sending.

- 2026-10-06 — An adversarial review of slices 1–4 found these, now fixed:
  - finishing one booking destroyed the range the next booking of it needed;
  - the `scheduler_settings` migration failed when `create_all` ran first;
  - the ownership flag was committed after the build;
  - an edit could race the clock;
  - `/tick` blocked the event loop.

  Still open:
  - `schedule:admin` is held by the `admin` role. If admins are per tenant, any
    tenant's admin can change the platform-wide policy. It needs a platform-admin
    notion.

## Open questions

- Does a booking create its exercise, or link to an exercise that already exists?
- Who gets reminders, when, and on which channels (email, in-app)?
- Confirm: do Students get their own sessions in a personal feed, or nothing at all?
