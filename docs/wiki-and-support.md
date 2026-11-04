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
caller's own. The bell polls once a minute.

### Email

When the install has an outgoing mail server (`SMTP_HOST` and a sender, see
`app/notifications/smtp.py`), the *personal* events above are also emailed: assigned to you,
a reply, a status change, an internal note. The "new ticket" broadcast to every staff member, and
a reporter's reply to a ticket nobody owns yet, stay in the bell only.

- **No ticket content leaves by email.** A message names the ticket (`TN-12`), says what
  happened and links to it (`WEB_BASE_URL`, else the installer's `LTI_WEB_BASE_URL`). No subject,
  details, reply text, internal-note text or person's name: mail is stored and forwarded by systems
  the platform does not control, and the text of a ticket can describe a range.
- **Each person can turn it off**: the *Email me about tickets* checkbox at the foot of the bell
  (`GET`/`PUT /notifications/settings`, the caller's own row only; `notification_preferences`,
  no row means on). The checkbox is hidden when the install has no mail server.
- **Throttled**: someone already *emailed* about a ticket in the last five minutes is not emailed
  about it again, so a busy thread sends one email, not ten. Only emails count (the notification is
  marked `emailed`): a bell-only broadcast does not use up the allowance, so an assignment is not
  swallowed by an earlier "reporter replied". Two events for one person in one request (a status
  change and an assignment together) send one email.
- **After the commit, at most once.** The emails go out from a background task once the request
  is committed, one attempt each (the shared SMTP channel normally retries three times; a retry
  after a timeout that followed the relay accepting the message would be a duplicate). A failed
  send is logged and never fails the request; a rolled-back change emails nobody. It is best
  effort: a restart between the commit and the send drops the email, and the bell row is already
  there. The throttle reads the database before the commit, so two requests on one ticket landing
  at the same moment on different workers can each send one.
- **Internal notes** go to the assignee only while they are still staff.

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
- **Labels and category** exist on tickets (search matches labels) but have no screen yet.

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

- Live push for the bell (it polls once a minute).
- AI triage ("ask AI", "run diagnostics") from the March design.
- Images inside wiki pages, and full-text search through OpenSearch (search is
  title/body/tags matching in the database today).
