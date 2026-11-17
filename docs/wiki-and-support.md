# Wiki and Support (trouble tickets)

A Confluence-style wiki and Jira-style ticketing, built into TrueNorth so they share
sign-in, tenants and roles, and tickets can point at a range or exercise.

Approved mockup: `docs/mockups/wiki-tickets.html` (PNGs in `docs/mockups/wiki-tickets-png/`).

## Where the code is

| Part | Path |
|---|---|
| Models | `control-plane/api/app/models_wiki.py`, `models_tickets.py` (registered for Alembic in `alembic/env.py`) |
| Migration | `control-plane/api/alembic/versions/a8b9c0d1e2f3_add_wiki_and_tickets.py` |
| API | `control-plane/api/app/routers/wiki.py` (`/wiki`), `routers/tickets.py` (`/tickets`); schemas inline |
| Permissions | `WIKI_READ/EDIT/ADMIN`, `TICKET_CREATE/WORK/ADMIN` in `app/rbac.py` |
| API clients | `core/services/wiki-api.service.ts`, `tickets-api.service.ts`; types alias `core/api/schema.d.ts` (ADR 0002) |
| Screens | `control-plane/web/src/app/features/wiki/`, `features/tickets/` (routes `/wiki`, `/support`) |
| Shared UI | `shared/markdown/` (marked + DOMPurify), `shared/kb-styles.component.ts`, `shared/kb-roles.ts` |
| Tests | `tests/api/test_wiki.py`, `test_wiki_concurrency.py`, `test_tickets.py`, `*.spec.ts` |

## Who can do what

| | Student / Observer | Instructor / Range ops | Admin |
|---|---|---|---|
| Read wiki spaces marked "everyone" | yes | yes | yes |
| Read "staff only" spaces, drafts | no (404) | yes | yes |
| Write and edit pages; see and restore history | no | yes | yes |
| Create / archive spaces | no | no | yes |
| File tickets | student: yes; observer: no (Support explains this) | yes | yes |
| See tickets | only their own | everyone's in the tenant | everyone's |
| Internal notes | never see them | read and write | read and write |
| Triage, assign, the board | no | yes | yes |
| Queues, delete tickets | no | no | yes |

A reporter can edit their ticket's subject and details ("Edit subject and details" on the
ticket), close it once resolved, and reopen it. Their reply to a ticket that is waiting on
them, resolved or closed moves it back to Open, because no staff view lists finished
tickets as work.

## Notifications

The bell in the toolbar shows the signed-in user's unread count (refreshed every minute
and on each page change) and their latest notifications; clicking one marks it read and
opens the ticket. Nobody is told about their own action.

| Event | Who is told |
|---|---|
| New ticket | every staff member of the tenant |
| Ticket assigned | the assignee |
| Staff reply, or status change by someone else | the reporter |
| Reporter reply | the assignee, or every staff member if unassigned |
| Internal note | the assignee (never the reporter) |

Rows live in the `notifications` table (`app/notifications/models.py`, `UserNotification`), written by
`app/notifications/inbox.py` in the same transaction as the change; `/notifications` serves only the
caller's own. In-app only for now: no email, and no live push (the WebSocket
`send_to_user` path exists but the bell polls).

## Behaviour worth knowing

- **Edit conflicts.** A page save carries the revision it started from and is a
  compare-and-set in the database. If someone saved in between, the API answers 409 with
  their version; the editor keeps your text and lets you keep yours (theirs stays in
  History) or load theirs. Every real change advances the revision, including publishing,
  unpublishing, tags and moves, so a stale save cannot quietly undo any of them.
- **History is never rewritten.** Restoring an old revision creates a new one, and also
  names the revision it was decided against. History is editors-only: old revisions can
  predate publication (an unpublished draft holding an answer key).
- **Deleting a page** soft-deletes everything under it.
- **Archiving a space** hides it from the wiki list and from students entirely, and makes
  its pages read-only for staff. Admins find it again with "Show archived" on the wiki
  list and can unarchive it from the space page. Its address stays reserved meanwhile.
- **Ticket numbers** (`TN-n`) are per tenant; on Postgres an advisory lock queues
  concurrent filings (a class reporting the same outage) instead of letting them collide.
  Every change to status, priority, type, assignee, queue, subject, details, range or
  exercise link, and every attachment, is recorded in the ticket's history. Reordering a
  card within its column is not, and doesn't change "last updated" either.
- **Attachments** are stored in MinIO bucket `tickets`: up to 25 MB each and 10 files per
  upload (the API accepts file uploads up to 50 MB per request, `MAX_UPLOAD_SIZE`; other
  requests stay at 10 MB, `MAX_REQUEST_SIZE`). They keep their exact bytes, are always
  downloaded as files (`application/octet-stream`, UTF-8 filenames), never rendered in the
  browser. Internal notes never change what the reporter sees, including "last updated".
- **Queues.** The last queue can't be deleted; a deleted queue's slug can be reused.
- **Board.** New tickets get distinct positions so drag-reordering sticks; the closed
  column shows the 50 most recent.
- **Markdown** from users is sanitised with DOMPurify before display: no scripts, event
  handlers, `javascript:` links, iframes, forms, or `style`/`class`/`id` attributes, and
  rendered text cannot draw outside its box.
- **"Report a problem"** appears on the Exercises list, the exercise page and the Ranges
  list (for roles that may file tickets), and opens `/support/new` with the exercise and
  its range filled in.
- **Category** is one of a fixed list (`GET /tickets/categories`: range or lab, VM console
  or access, network, account or sign-in, scoring, course content, TrueNorth itself,
  something else). The reporter picks it when filing; staff can change it on the ticket.
  Anyone can filter the list by it.
- **Labels** are staff triage tags: lowercase, up to 10 per ticket, each up to 30
  characters from `a-z 0-9 . _ : / -`, added and removed as chips on the ticket (with
  suggestions from the labels already in use, `GET /tickets/labels`) and used to filter
  the list and shown on board cards. **A reporter never sees them**: they are masked in
  every response, left out of the reporter's search, refused as a filter, and their
  history rows are hidden. Only staff can set them.

## Reviews

Security and adversarial reviews ran on 2026-10-05, a browser walkthrough and a third
review on 2026-10-06. Every confirmed finding was fixed with a regression test. The third
review found an app-wide bug outside this section: the input-sanitising middleware
stripped zero bytes from every request body, so every binary upload (ticket attachments,
range documents) was stored corrupted; it now leaves file uploads alone. One was deliberately left alone: a student can link a ticket to
any range in their tenant, as they can already list them all through `GET /ranges`. The
app-wide NaN/Infinity 422-turned-500 found along the way is fixed in
`app/validation_errors.py`.

## Not in this version

- Email notifications (#378), and live push for the bell (#377; it polls once a minute).
- AI triage ("ask AI", "run diagnostics") from the March design (#381).
- Images inside wiki pages (#379), and full-text search through OpenSearch (search is
  title/body/tags matching in the database today, #380).
