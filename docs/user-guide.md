# TrueNorth Range — User Guide

> End-user documentation for instructors, students, and observers covering every workflow from first login through after-action review.
>
> Checked against the code on `github/main` on 2026-10-12 for v1.2.0. Features this guide
> once described but the release does not have are listed in
> [Not in v1.2.0](#not-in-v120).

---

## Table of Contents

- [Getting Started](#getting-started)
- [Dashboard Overview](#dashboard-overview)
- [Managing Ranges](#managing-ranges)
- [Running Exercises](#running-exercises)
- [Working with Scenarios](#working-with-scenarios)
- [Objectives and Scoring](#objectives-and-scoring)
- [After Action Reports (AAR)](#after-action-reports-aar)
- [Telemetry and Event Viewer](#telemetry-and-event-viewer)
- [Team Management](#team-management)
- [Templates](#templates)
- [AI Assistant](#ai-assistant)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Not in v1.2.0](#not-in-v120)
- [Frequently Asked Questions](#frequently-asked-questions)

---

## Getting Started

### First Login

TrueNorth does not issue you a password. You sign in with your **existing domain
credentials** — the same username and password you use for everything else — and
the range checks them against Active Directory.

> This applies where the deployment federates Active Directory (installer option
> `tn_ldap_enabled`, see [identity.md](identity.md)). Without it, accounts are local
> Keycloak accounts created by an administrator. Directory-backed sign-in has not yet been
> qualified against a test directory for v1.2.0.

1. Navigate to `https://your-truenorth-instance.com` in a modern browser
   (Chrome 120+, Firefox 120+, Edge 120+, Safari 17+)
2. Click **Sign In** — you are redirected to the Keycloak login page
3. Enter your **domain** credentials. If your organisation requires multi-factor
   authentication, you are challenged for it here, as usual.
4. What happens next depends on whether you already have a TrueNorth account:
   - **You have one** — you land on the dashboard.
   - **You do not** — you land on a short **access request** form instead. This
     is expected on a first login and is not an error.

### Requesting access

Authenticating proves who you are. It does not, by itself, create an account —
an instructor admits you.

The form already knows your name, email and directory details, and those fields
are read-only: they come from the directory, and editing them here would only
put the two out of step. It asks for what the directory does not know — your
rank, unit, callsign, nation, time zone, the qualification you are joining, and
your cohort or serial number.

Submit it and you are on a waiting screen that checks for you every 30 seconds.
You can leave it open or sign out and come back. If your request is declined you
will see the reason and can submit a new one.

### Getting started once approved

The first time you sign in with a live account you get three short steps:
confirm the details you already gave, confirm the training path you have been
enrolled on, and a quick tour of where things live. You will not be asked again.

> Full detail, including the instructor's side of the approval queue, is in
> [onboarding.md](onboarding.md).

### Understanding Your Role

Your role determines what you can see and do. The table is the API's permission
matrix (`ROLE_PERMISSIONS` in `control-plane/api/app/rbac.py`):

| Feature | Admin | Instructor | Student | Observer | Range Ops |
|---------|:-----:|:----------:|:-------:|:--------:|:---------:|
| View dashboard | X | X | X | X | X |
| Create ranges | X | X | | | X |
| Provision/destroy ranges | X | X | | | X |
| Create exercises | X | X | | | |
| Start/pause/complete exercises | X | X | | | |
| Submit detections in an exercise | X | X | X | | |
| View telemetry | X | X | | X | X |
| Acknowledge objectives | X | X | | | |
| Generate AARs | X | X | | | |
| View AARs | X | X | X | X | |
| Approve access requests | X | X | | | |
| Manage users | X | | | | |
| Manage tenants | X | | | | |
| View audit logs | X | | | | |

The web app is narrower than the API in places: the Authoring Studio (where the
Ranges page lives) and Scoring & AAR are for instructors and admins only, so Range Ops
manage ranges through the API in v1.2.0; Users & Teams is admin only.

Your role is assigned when your access request is approved. Directory group
membership *suggests* a role to the approver but does not set it — being in
`TN-Platform-Admins` does not make you an administrator here.

> The interface says **Trainee**; the underlying role value is `student`. They
> are the same thing.

### Navigation

The sidebar (`control-plane/web/src/app/app.component.ts`) is grouped by task:

- **Overview → Dashboard**
- **Learn → Learning** — career path, courses, your progress, competency
- **Create → Authoring Studio** — Ranges (and the Range Designer), Scenarios,
  Detections, Threat intel, MESL, Forge, Content (range templates), Courses
- **Prepare & Operate → Exercises, Ops Center, Schedule** (Schedule is hidden from Students)
- **Review → Scoring & AAR**
- **Help → Wiki, Support**
- **Admin → Infrastructure, AI Orchestrator, Integrations, Users, Admin**

In v1.2.0 every role sees the same sidebar apart from Schedule. A page your role may not
use sends you back to the dashboard (route guards) and the API refuses the call.

---

## Dashboard Overview

There is one dashboard for every role (`features/dashboard/dashboard.component.ts`).
It has no per-role widget sets.

| Area | What it shows |
|------|---------------|
| **My sessions** | Students only: your booked sessions and calendar link |
| **Workspace launcher** | Links to Learning, Exercises, the Authoring Studio and Scoring & AAR (and the Schedule for staff) |
| **Platform & capacity** (collapsed) | Counts from the latest five ranges and exercises, API health, hypervisor nodes, cluster capacity and a "What can you deploy?" estimate, recent ranges and exercises |
| **Your development workspace** | Links to courses, learning progress, the competency framework and the people directory |
| **Quick Actions** | Design Range, Build Scenario, Browse Courses, Manage Infra, AI Config |

---

## Managing Ranges

### What Is a Range?

A **range** is a collection of virtual machines that simulates a network environment. Ranges are provisioned from templates and can host one or more exercises.

### Range States

The allowed transitions are in `control-plane/api/app/state_machines.py`. The API
records a request (`provisioning`, `stopping`, `starting`, `destroying`); only the worker
moves the range out of those states, once the hypervisor has answered.

| State | Description | Actions in the Ranges list |
|-------|-------------|-------------------|
| `created` | Range defined but not provisioned | Provision, Rename |
| `provisioning` | The worker is building VMs and networks through the provisioner | Wait |
| `ready` | VMs running, range available | Stop, Destroy |
| `running` | Range in use | Stop, Destroy |
| `stopping` / `starting` | A power request recorded; the worker is powering the VMs off or on | Destroy |
| `stopped` | VMs powered off | Start, Rename, Destroy |
| `destroying` | VMs being torn down | Wait |
| `destroyed` | All VMs removed | — |
| `failed` | Provisioning or an operation failed | Destroy (re-provision through the API) |

v1.2.0 provisions on vSphere (`vsphere_api`) or the `mock` backend; vSphere is proven
against a simulator only (see [release notes](release-notes/v1.2.0.md)).

### Creating a Range

1. Open **Authoring Studio → Ranges** and click the create button
2. Enter a **Range Name** and pick a **Template** (see [Templates](#templates))
3. Click **Create**; the range is created in `created` state

You can also start from **Open in Designer** (Authoring Studio → Ranges → Designer).

### Provisioning a Range

1. In the Ranges list, click **Provision** on a range in `created` state
2. The range moves to `provisioning`, then to `ready` (or `failed`) when the worker finishes

The list does not update itself: reload it to see the new state. No provisioning time is
published for v1.2.0; none has been measured on real vSphere hardware.

### Batch Provisioning

Admins, instructors and Range Ops can provision several ranges in one call through the
API (`POST /ranges/batch-provision`). There is no batch screen in the web app.

### Monitoring a Range

There is no per-range detail page in v1.2.0. The Ranges list shows name, state and
creation time, with links to the Range Designer, background noise and range notes. An
exercise's page shows its range topology, and the Telemetry Explorer (`/telemetry`)
searches a range's events.

### Machine Accounts (Credentials)

A range template can declare the accounts its machines get at deploy (`accounts:`): the
Windows `Administrator`, the Linux install user, and accounts for Students. Each has a
fixed password or `generate: true` (TrueNorth makes one up once per range and keeps it
across rebuilds). Passwords are stored encrypted.

- **Admins, instructors and Range Ops** open the range's page (`/ranges/:id`) and use the
  **Credentials** panel: **Show accounts** loads the list, each password stays masked until
  you click the eye, and the copy buttons copy a username or password without showing it.
  Every time the list is loaded is recorded in the audit log.
- **Students** see a **Your accounts** panel on the exercise page, listing only the
  accounts handed to Students, and only for a range they take part in (their own lab, or a
  running exercise booked to a course they are enrolled on).
- **Observers** do not see credentials.

Each range also gets a page in the staff-only wiki space **Ranges**: its machines,
addresses, networks and the full credential table. TrueNorth rewrites that page when the
range is created, renamed or built, or its template changes. Write your own notes under its
**Notes** heading; they are kept. Students cannot open this space.

A machine with no declared account gets the platform's generic password if the installer
set one (`vault_range_default_password`), otherwise a random password nobody is told.
On Windows only the `Administrator` account is set at deploy; other Windows accounts
are listed but must be created by the scenario (the build reports them as a warning).

### Stopping and Starting a Range

1. Click **Stop** on a `ready` or `running` range; the VMs are powered off
2. Click **Start** on a `stopped` range to power them on again

### Destroying a Range

1. Click **Destroy** in the Ranges list. There is no confirmation step: the request is
   sent at once
2. The worker removes the range's VMs and networks
3. Telemetry already indexed for the range stays in OpenSearch

---

## Running Exercises

### What Is an Exercise?

An **exercise** is a training session conducted on a range. It uses a scenario to inject attack events and defines objectives for Students to complete.

### Exercise States

```
pending → running ⇄ paused
   ↓         ↓        ↓
cancelled  completed (running or paused)
```

| State | Description |
|-------|-------------|
| `pending` | Exercise created, not yet started |
| `running` | Exercise active, scenario events firing |
| `paused` | No further injects fire until it resumes; the duration clock keeps running |
| `completed` | Exercise finished — scored and closed (from `running` or `paused`) |
| `cancelled` | A scheduled booking's exercise withdrawn before it started; there is no cancel button |

### Creating an Exercise

1. Open **Exercises** and click the create button
2. Fill in **Name**, **Range** and **Scenario** (`ExerciseIn` in
   `control-plane/api/app/schemas.py` takes nothing else; there is no team assignment)
3. Click **Create**; the exercise is created in `pending` state with the scenario's
   objectives, and its maximum score is the sum of their points

### Starting an Exercise

1. In the Exercises list click **Start** (instructors and admins), or **Provision & Run
   (simulated)** on the exercise's page
2. The exercise moves to `running` and the worker fires the scenario's timeline

**What happens when you start:**
- The worker dispatches each timeline event to its injector. Every injector except the
  Greyspace breadcrumb one is **simulated**: it writes synthetic telemetry, not real
  network, process or file activity ([scenario-inject-execution.md](scenario-inject-execution.md))
- The injected telemetry is indexed in OpenSearch for the range
- The exercise clock starts

### Pausing an Exercise

1. Click **Pause** in the Exercises list; the exercise moves to `paused`
2. Click **Resume** to set it running again

Paused time still counts towards the scenario's duration.

### Completing an Exercise

**Manual completion:** click **Complete** in the Exercises list. The score is totalled and
the AAR can be generated.

**Automatic completion:** when the scenario's `duration_minutes` has passed since the
start (`control-plane/api/app/exercise_completion.py`). Achieving every objective does not
end an exercise. On the `mock` backend the worker completes the run itself and marks every
objective achieved without evidence: a demonstration, not an assessment.

### Viewing Exercise Progress

The exercise page (`/exercises/{id}`) shows:

| Panel | Description |
|-------|-------------|
| **Score** | Points earned out of the maximum |
| **Objectives** | Each objective with its type, points and achieved state; detection objectives carry the detection panel |
| **Attack Timeline** | The scenario's planned events |
| **Inject log** | What each inject did: `fired`, `skipped` or `failed` |
| **Range Topology** | The range's networks and hosts |

While the exercise is running, the page and the Exercises list poll the API every few
seconds; they do not use a WebSocket.

---

## Working with Scenarios

### Browsing Scenarios

Open **Authoring Studio → Scenarios** (instructors and admins). Each row shows the
scenario's name, version and creation time, with **Open** and **Delete**.

### Opening a Scenario

**Open** loads the scenario into the editor: its timeline events, objectives and other
fields, built from the stored YAML. Where the editor cannot represent part of the YAML it
says so and saving from it is refused, so nothing is silently dropped.

### Creating Scenarios (Instructors)

See the [Scenario Authoring Guide](scenarios.md) for complete YAML schema and authoring instructions.

**Quick path:**
1. Open **Authoring Studio → Scenarios** and click **New Scenario** (or **Draft with AI**)
2. Fill in the fields and add timeline events and objectives
3. Click **Validate** to check the YAML, then **Save**

---

## Objectives and Scoring

### How Objectives Work

Each exercise has objectives defined by its scenario. There are three types
(`ObjectiveType` in `control-plane/api/app/models.py`):

- **detection** — find the attack activity in the range's telemetry
- **response** — carry out a response action
- **deliverable** — produce an artifact

### Detection objectives: Students submit detections

A detection objective is achieved only by what a Student does (ADR 0005,
[docs/adr/0005-detection-credit-is-student-evidence.md](adr/0005-detection-credit-is-student-evidence.md)).
While the exercise is running, the objective's detection panel on the exercise page takes
a detection query. The API runs it against the range's telemetry inside the exercise
window and credits the objective if it finds the attack. The scenario's own query is the
answer key and never achieves anything by itself, so an injected event credits nobody.

### Acknowledging objectives (instructors)

Instructors and admins can acknowledge an objective on a running or paused exercise, in
**Scoring & AAR** (`POST /exercises/{id}/objectives/{ref_id}/ack`, optional text evidence). The objective is
marked achieved, the acknowledger is recorded and the score is re-totalled. Students
cannot acknowledge objectives.

### Understanding Scores

| Element | Description |
|---------|-------------|
| **Points Earned** | Sum of achieved objective points |
| **Points Available** | Total possible points |
| **Percentage** | `(earned / available) * 100` |

There is no rating band (Excellent, Good, ...) in v1.2.0. The score belongs to the
exercise, not to an individual or a team.

---

## After Action Reports (AAR)

### What Is an AAR?

An After-Action Report (AAR) is a record of an exercise built from the database: what was
planned, what happened, and how each objective ended. An AI analysis can be added to it
afterwards; it is optional.

### Generating an AAR

1. Complete the exercise
2. Click **Generate AAR** (the Exercises list, or the exercise page once it is completed;
   instructors and admins)
3. The API assembles the report (`control-plane/api/app/aar_report.py`) and stores it;
   generating again replaces it
4. Optionally, `POST /exercises/{id}/aar/ai-enhance` asks the AI Orchestrator for an
   analysis (summary, strengths and weaknesses, recommendations) and adds it to the
   report. It needs a configured, reachable AI engine.

### Viewing an AAR

In the web app: **Scoring & AAR**, or **Download AAR PDF** in the Exercises list. Through
the API:

```
GET /exercises/{id}/aar        # JSON
GET /exercises/{id}/aar/html   # rendered page
GET /exercises/{id}/aar/pdf    # PDF
```

### AAR Contents

| Section | Description |
|---------|-------------|
| **Exercise, scenario, range** | Names, state, start and completion times |
| **Scores and summary** | Total, maximum and percentage; objectives achieved out of total |
| **Objectives** | Each objective with type, points, achieved state, evidence and time achieved |
| **Injects** | The scenario's planned timeline and any MESL serials |
| **Timeline** | What happened, in time order |
| **Participants** | Who took part |
| **AI analysis** | Only after an AI enhance |

---

## Telemetry and Event Viewer

### Accessing Telemetry

Open the **Telemetry Explorer** at `/telemetry` (it has no sidebar entry in v1.2.0). It needs
`telemetry:read`: instructors, admins, observers and Range Ops; Students do not have it.

### Explorer Features

| Feature | Description |
|---------|-------------|
| **Range** | Pick the range whose events to search |
| **Query** | The search language below; filter by host, type, field and so on here |
| **Charts** | Event volume over time, event types, top hosts |
| **Results table** | Time, type, host, source IP, process, detail, ATT&CK technique |

There is no time-window picker, column selector, raw-event view or export in v1.2.0.
Range telemetry comes from the scenario injectors (synthetic), the noise engine and
whatever is sent to the ingest API; no sensor is installed on range VMs by provisioning.

### Search Syntax

The search bar takes a small, closed language (`control-plane/api/app/search_backends/query.py`),
not raw Lucene. Every term is ANDed:

| Form | Meaning | Example |
|------|---------|---------|
| `field:value` | exact value of one field | `event_type:dns_query` |
| `field:"two words"` | quoted value | `hostname:"ws 01"` |
| `field:prefix*` | value starts with (the `*` only at the end) | `process_name:power*` |
| `field:*` | the field is present | `mitre_technique:*` |
| free text | words anywhere in the event; `-word` excludes | `powershell -notepad` |

Not accepted (the search answers 422): `OR` or `NOT` next to field terms, wildcards
anywhere but the end of a value (`*.corp.local`, `10.*.1`), regular expressions, fuzzy or
proximity operators, and field names starting with `_`.

Common fields: `event_type`, `hostname`, `username`, `source_ip`, `dest_ip`,
`source_port`, `dest_port`, `protocol`, `action`, `severity`, `process_name`,
`mitre_technique`. Event types the scenario injectors and sensors use include
`dns_query`, `http_request`, `process_exec`, `simulated_execution`, `login_attempt`,
`lateral_move`, `network_scan`, `email_phish`, `identity_new_admin_user` and `c2_beacon`.

### Common Queries

| Purpose | Query |
|---------|-------|
| Find phishing emails | `event_type:email_phish` |
| Find PowerShell execution | `event_type:process_exec process_name:powershell*` |
| Find DNS queries from one host | `event_type:dns_query hostname:ws-01` |
| Find new admin accounts | `event_type:identity_new_admin_user` |
| Find brute-force sign-in attempts | `event_type:login_attempt` |
| Find lateral movement over RDP | `event_type:lateral_move dest_port:3389` |
| Find outbound HTTPS from one host | `event_type:http_request hostname:ws-01 dest_port:443` |
| Find events mapped to ATT&CK | `mitre_technique:*` |
| Find high-severity events, any host | `severity:high` |
| Free text, excluding a word | `powershell -notepad` |

---

## Team Management

In v1.2.0 a team is a named group with a type, description, colour and member limit. Teams
are not linked to exercises or scores.

### Creating, editing and deleting a team (admins)

1. Open **Users** → **Teams** tab
2. Click **Add Team**, fill in the name and optional fields, click **Create Team**
3. Use the edit and delete buttons on a team card to change or remove it

### Team membership

Membership is managed through the API only (`GET`/`POST /teams/{team_id}/members`,
`DELETE /teams/{team_id}/members/{user_id}`; `user:update` to change). The web app has no
member screen in v1.2.0.

Team exercises and team scoring are planned for R2; see [Not in v1.2.0](#not-in-v120).

---

## Templates

### What Is a Template?

A **template** defines the virtual infrastructure for a range — the VMs, their operating systems, network topology, and installed software. Templates are the blueprints from which ranges are provisioned.

### Browsing Templates

Templates live in **Authoring Studio → Content** (the old `/templates` address redirects
there). A template is YAML; the editor dialog loads and saves it.

### Shipped Templates

The shipped-content load (`scripts/load_content.py`) imports every
`content/ranges/*/template.yaml`:

| Directory | Template name |
|-----------|---------------|
| `cloud-security` | Cloud Security Training Environment |
| `greyspace-internet` | Small Office with Greyspace Internet |
| `large-enterprise` | Large Enterprise Network |
| `medium-enterprise` | Medium Enterprise Network |
| `red-team` | Red Team Operations Environment |
| `red-vs-blue` | Red vs Blue Cyber Exercise |
| `small-enterprise` | small-enterprise |
| `soc-training` | SOC Analyst Training Environment |

Read the YAML for each template's hosts and networks; this guide does not repeat them.

---

## AI Assistant

### Overview

There is no chat assistant in the web app (no floating icon). AI is used in specific places,
through the AI Orchestrator and a configured engine:

- **Scenarios → Draft with AI** — drafts a scenario for you to edit
- **AAR AI enhance** — `POST /exercises/{id}/aar/ai-enhance` (API only)
- **ARC² Course Studio** — course generation (`/arc2`, instructors)
- **AI Orchestrator page** (Admin section) — engines, model routing and an inference
  playground for administrators

Every AI feature needs an engine configured under **AI Orchestrator**; without one these
actions fail with an error.

---

## Keyboard Shortcuts

There are no application-wide shortcuts in v1.2.0 (see [Not in v1.2.0](#not-in-v120)).
`Esc` closes dialogs, the notification panel and the tour.

The **Range Designer** has its own shortcuts, active when focus is not in a text field
(`features/range-designer/range-designer.component.ts`):

| Shortcut | Action |
|----------|--------|
| `Ctrl/Cmd + S` | Save the diagram |
| `Ctrl/Cmd + Z` | Undo |
| `Ctrl/Cmd + Y` or `Ctrl/Cmd + Shift + Z` | Redo |
| `Ctrl/Cmd + D` | Duplicate the selected node |
| `Delete` or `Backspace` | Delete the selected node |

---

## Not in v1.2.0

Earlier versions of this guide described the following. The v1.2.0 code does not have them;
do not plan training around them.

| Feature | What exists instead |
|---------|---------------------|
| Team exercises (assigning teams to an exercise); planned, R2 | Team records and a membership API only; exercises take no teams |
| Team-based or individual scoring; planned, R2 | One score per exercise |
| Leaderboards and the Student "Team Standings" widget; planned, R2 | None |
| Observer "Score Summary" dashboard widget | None; the dashboard is the same for every role |
| Per-role dashboard widget sets | One dashboard (see [Dashboard Overview](#dashboard-overview)) |
| Global keyboard shortcuts and command palette | Range Designer shortcuts only |
| Floating AI chat assistant | AI in specific features (see [AI Assistant](#ai-assistant)) |
| Rating bands (Excellent ... Unsatisfactory) | Points and percentage only |
| Telemetry time filter, column selector, raw-event view, export | Query box, charts and results table |
| Range detail page, batch provisioning screen, destroy confirmation | Ranges list actions; batch through the API |
| Live updates over WebSocket in the web app | Polling on the exercise pages |
| French or any other interface language; planned milestone after R1 | English only; no i18n |

---

## Frequently Asked Questions

### General

**Q: What browsers are supported?**
A: Chrome 120+, Firefox 120+, Edge 120+, Safari 17+. Chromium-based browsers recommended for best performance.

**Q: Can I use TrueNorth Range on a mobile device?**
A: The interface is responsive but optimized for desktop. Exercise participation requires a desktop browser.

**Q: How do I reset my password?**
A: With Active Directory federation your password belongs to the directory: change it
there. A local Keycloak account can use "Forgot Password" on the login page (the realm
allows it; it needs mail configured in Keycloak), or ask your administrator.

### Ranges

**Q: How long does provisioning take?**
A: It depends on the template and the vSphere cluster. No figure has been measured on real
hardware for v1.2.0.

**Q: Can I keep a range running indefinitely?**
A: Ranges consume resources while running. Stop ranges you are not using.

**Q: What happens to my data when a range is destroyed?**
A: Its VMs and networks are removed. Telemetry already indexed in OpenSearch and stored
reports stay.

**Q: My range is stuck in "provisioning" — what do I do?**
A: Contact your administrator to check the provisioning task and the range's operations
(`GET /ranges/{id}/operations`).

### Exercises

**Q: Can I pause and resume an exercise?**
A: Yes, instructors and admins can. Paused time still counts towards the scenario duration.

**Q: What happens if I disconnect during an exercise?**
A: The exercise keeps running. Reopen the page to see its current state.

**Q: Can multiple people participate in the same exercise?**
A: Yes. Several Students can open the same exercise and submit detections; the score is the
exercise's, shared by everyone in it.

**Q: How are objectives achieved?**
A: Two ways: (1) a Student's submitted detection finds the attack in the range's telemetry
(detection objectives), (2) an instructor or admin acknowledges the objective.

### Scoring

**Q: Can I improve my score after an exercise completes?**
A: No. A completed exercise accepts no more detections or acknowledgements.

**Q: Is there a team score?**
A: No. There is one score per exercise; teams are not linked to exercises in v1.2.0.

**Q: What is a passing score?**
A: Per your organization's policy. TrueNorth v1.2.0 defines no pass mark or rating bands.

### Troubleshooting

**Q: I see "403 Forbidden" when accessing a resource.**
A: Your role does not have permission for that action. Contact your administrator.

**Q: Telemetry viewer shows no events.**
A: Ensure the exercise has started and events have been injected. Check the time range filter.

**Q: AAR generation fails.**
A: Ensure the exercise exists in your tenant and you are an instructor or admin. Only the
optional AI enhance needs the AI Orchestrator.