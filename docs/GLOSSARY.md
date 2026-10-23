# TrueNorth glossary

The ubiquitous language of TrueNorth Range: the words to use in code, docs, tickets
and chat. Each entry gives the meaning, where it lives, what it is easily confused with,
and the words to avoid. Decisions and their reasons stay in `docs/adr/`; this file only
names things. Sourced from the code and docs on 2026-10-19. If the code and an entry
disagree, the code wins: fix the entry.

## People and roles

Roles are `UserRole` in `control-plane/api/app/models.py`; their permissions are
`ROLE_PERMISSIONS` in `control-plane/api/app/rbac.py`.

**Student**:
A person taking a course or an exercise (`UserRole.student`).
_Avoid_: Member, Learner, Trainee, user (when you mean the role).
_Not to be confused with_: a team. A Student may belong to a team, but credit,
enrolment and credentials belong to the Student.

**Instructor**:
Staff who run training in their own tenant: ranges, scenarios, exercises, bookings for
their classes, and their courses' releases (`UserRole.instructor`). Sets up their own
class: enrols Students on the courses they teach and builds the teams of the exercises they
run (see **Teaches**).
_Not to be confused with_: Admin. An Instructor can see platform health but cannot
reconfigure infrastructure.

**Teaches** (a course) / **runs** (an exercise):
The instructor's own booking of that course or exercise is upcoming or running: it names
them its instructor, has not ended, and is not draft, cancelled or completed
(`control-plane/api/app/class_setup/scope.py`). A past class grants nothing, and nothing is
changed while another instructor's session of it is running.
Courses and exercises have no owner of their own; the calendar is the record.
_Not to be confused with_: authored (`course:author` is tenant-wide), or booked by
(`created_by`: an admin may book a session for an instructor).

**Observer**:
A read-only evaluator or auditor of ranges and exercises (`UserRole.observer`). Sees no
range credentials (ADR 0012 §13).
_Avoid_: guest, viewer.

**Range Ops**:
Staff who operate the infrastructure: provisioning, hypervisors, the range-support queue
(`UserRole.range_ops`). Cannot author exercises or scenarios.
_Avoid_: DevOps, operator (alone).

**Admin**:
Holds every permission, within a tenant (`UserRole.admin`). The **platform admin** is an
Admin of the platform tenant (`PLATFORM_TENANT_ID`), the only one who reads across tenants
(`docs/security.md`, "AU-6").
_Not to be confused with_: platform admin. A plain Admin is still tenant-scoped.

**Tenant**:
An organisation whose people, ranges, content and records are isolated from every other
(`Tenant` in `models.py`; every by-id lookup goes through `control-plane/api/app/tenancy.py`).
A row from another tenant is a 404, never a 403.
_Avoid_: org, customer, account.

## Ranges and templates

**Range**:
An isolated network of VMs built from a range template for training (`Range` in
`models.py`; lifecycle in `control-plane/api/app/range_lifecycle.py`).
_Not to be confused with_: range template (the design), lab session (one Student's small
range), Greyspace (the simulated internet attached to a range).
_Avoid_: environment, lab (alone).

**Range template**:
The YAML design of a range: nodes, networks, images and declared accounts (`Template` in
`models.py`; schema `scenario-engine/schemas/template.schema.json`). Shown in the UI under
Create → Content.
_Not to be confused with_: golden template. A range template names golden templates by
catalogue id; it is not itself a VM image.

**Golden template**:
A prebuilt, key-free VM template a range clones for one machine; built by Packer from the
repo (`infra/vsphere/packer/`), registered as a `GoldenImage` (`models.py`,
`control-plane/api/app/golden_images.py`). ADR 0012.
_Avoid_: golden image (the table's name; same thing), base VM, master image.
_Not to be confused with_: range template; host-local template copy.

**Catalogue id**:
The stable short name of a golden template, such as `srv2022`, keyed in
`content/catalogue/vm_iso_catalogue.csv` (`GoldenImage.catalogue_id`). Range templates and
lab profiles may name only enabled catalogue ids.
_Not to be confused with_: the hypervisor template name, or an OS alias that maps to it.

**Promoted build**:
The one registered build of a golden template (build id, checksum, build date, ISO
checksum, per-host copy names; `golden_image_builds`) that an administrator promoted on the
Golden Images page; every new range clones exactly it, and each VM records which build it
got. ADR 0013 §2.
_Not to be confused with_: a registered build (recorded by the template kit, not yet
promoted, never cloned); the newest copy on a host.

**Host-local template copy**:
A clone of a golden template on each range host's own datastore, named
`<id>@<host>-<yyyymmdd>` and stamped with the build it is; the provisioner clones only the
**promoted build's** copy on the target host (`infra/vsphere/packer/README.md`, "Build
records and promotion"; pruned by `worker.template_tasks.prune_template_copies`). ADR 0012
§6, ADR 0013 §2.
_Avoid_: replica, cached template.
_Not to be confused with_: the golden template itself, which the copies are made from.

**Role image**:
A golden template with a server product's prerequisites and media preinstalled, whose
domain-dependent setup runs at deploy time: `srv2022-exchange2019`,
`srv2022-sharepointse`, `srv2022-mecm` (`infra/vsphere/packer/roles.pkr.hcl`,
`control-plane/api/app/windows_roles.py`). Their catalogue rows stay disabled for now.
_Not to be confused with_: a user role (`UserRole`), or a Windows role installed on a
plain base image.

**Declared account**:
A login a range template lists under `accounts:` for one machine, with its password (or
"generate") and whether Students get it (`control-plane/api/app/range_credentials/accounts.py`).
Stored sealed. ADR 0012 §12.
_Not to be confused with_: applied account; implicit staff login.

**Applied account**:
A declared account that the last build reported actually setting on the VM
(`RangeCredential.applied` in `range_credentials/models.py`). Students only ever see
applied accounts.
_Not to be confused with_: declared account, which may not be set yet.

**Implicit staff login**:
A generated, staff-only account TrueNorth adds for a built-in login a machine's golden
template carries and the range template does not declare: the built-in Administrator, the
Linux build user, Security Onion's `soadmin` (`accounts.implicit_staff_logins`; API
`source: implicit`). It is how the template password is replaced at deploy (ADR 0013 §3).
On a lab session's range it is handed to the Student.
_Not to be confused with_: declared account; the template password, which no deployed VM
keeps (the "template default" of ADR 0012 is gone).

**Scenario account**:
A declared account with `scenario: true`: its typed-in password is part of the exercise
(the weak password is the lesson). Kept as authored, never rotated; never a staff login.

**Reveal** (credential):
Showing one account's password on the credentials panel (`POST
.../credentials/{id}/reveal`). Every reveal is an audit row; the list never carries passwords.

**Build network**:
The isolated network golden templates are built on: port group `dPG-TN-BUILD`, VLAN 31,
10.30.31.0/24, no gateway and no internet; TN-MGMT01 serves its DHCP from 10.30.31.20
(ADR 0012 §7; `docs/deployment/vmware-site-runbook.md` §4.1a).
_Not to be confused with_: the management network, or a range's own networks.

**Depot** (TN-DEPOT01):
The package mirror VM (Nexus with a Chocolatey feed and raw `installers`, plus
apt-cacher-ng) that template builds pull from, so builds need no internet. At 10.30.30.30
(management) and 10.30.31.30 (build); the only machine with internet egress. Range
firewalls reach it on a third NIC, 10.30.32.10 on the range uplink network, TCP 8081/3142
only; it never routes between its NICs (ADR 0012 §4; #214; `install/lab/depot.yml`;
Packer `depot_url`, `TN_DEPOT_URL` for ranges).
_Avoid_: repo server, proxy (alone).

## Training delivery

**Class** (ADR 0014):
One running of a course for a named group, e.g. "C201-26-03", in one tenant. It has a lead
instructor, assistant instructors, observers, a **roll call**, dates and its **class
sessions** (`Class` in `control-plane/api/app/classes/models.py`). Only its lead or an admin
changes it; it is cancelled, never deleted. A booking made without naming the class is not
linked to it (see **Teaches**).
_Not to be confused with_: course (the content a class teaches), cohort (free text on the
registration form, with no link to a class).

**Roll call** (ADR 0014):
The Students on a class, by email (`RollCallEntry`, `app/classes/models.py`; statuses invited,
signed_in, ready, removed, and pending: added after the sessions were approved with no room
left, waiting for an approver). Being on it is approval to join: a roll-call Student's first AD
sign-in with a verified email admits them (`app/classes/onboarding.py`). A class is visible
only to its roll call, instructors, observers and admins.
_Avoid_: roster (in the UI), attendees.

**Class session** (ADR 0014):
One calendar booking of a class (`ClassSession` links a `Class` to a `ScheduledEvent`,
`app/classes/sessions.py`). Booked by the class's lead; it waits for **approval** and holds
nothing until then. Its invites and reminders go to the roll call. It will be *individual*
(every Student gets their own small range; blue on the calendar) or *collective* (one shared
range for an **Exercise**; orange) (#282).

**Approval** (of a class session, ADR 0014 §14-16):
An admin or range ops (`schedule:approve`) approves a class session, rejects it with a reason,
or proposes another time that its instructor accepts or declines
(`app/scheduler/approval.py`). The booking is `pending_approval` until then: no capacity,
range or instructor held, no clock, no invites. Approving re-checks conflicts and capacity.
_Not to be confused with_: registration approval (Request access), roll-call admission.

**Acknowledgement** (planned, ADR 0014):
The rules of use everyone signs on first sign-in, whenever the version changes, and yearly.

**Stale** (content, ADR 0014):
Content whose review is due by time (90–120 days) or that a new release or a known-exploited
vulnerability has flagged, directly or through a golden image or range template it depends on.
The rules are in `control-plane/api/app/content_review/staleness.py`: an overdue review marks
only the item itself; a release or vulnerability flag also marks everything downstream.
**Mark reviewed** clears the item's own reasons and what they caused downstream.

**Exercise**:
One run of training on a range: an `assessment` (individual practical) or a `collective`
(team exercise driven by a MESL), with objectives and a score (`Exercise` in `models.py`;
lifecycle `control-plane/api/app/exercise_lifecycle.py`).
_Not to be confused with_: scenario (the authored attack timeline an exercise may run);
lab session.

**Lab session**:
One Student's (or team's) own small range for one range activity of a course release,
keyed by tenant, user, release, activity and attempt
(`control-plane/api/app/lab_sessions/`). Owns the lease, networks, readiness and teardown;
keeps the Student's evidence after the range is destroyed.
_Not to be confused with_: exercise; booking; a cmi5 session (one AU launch).

**Lab profile**:
The `03-range/lab_profile.yaml` in an ARC² course that describes the small range a range
activity needs, validated the same way by ARC² and by the lab-session service
(`control-plane/api/app/course_releases/lab_profile.py`).
_Not to be confused with_: range template.

**Booking**:
A scheduled, capacity-reserving slot on the calendar for a class, with its instructor,
course and range or template (`ScheduledEvent` in
`control-plane/api/app/scheduler/models.py`). ADR 0004, 0006. A plain booking is
self-service; a **class session** needs **approval** first (ADR 0014 §14).
_Avoid_: reservation, event (in user-facing text).
_Not to be confused with_: lab session, which is launched on demand.

**Ops Center**:
The live view of one running exercise: topology, annotations, shared commands, and the
instructor's live injects (`control-plane/web/src/app/features/ops-center/`, route
`/ops-center/:exerciseId`).
_Not to be confused with_: Range Ops (the role); the admin Infrastructure pages.

**Inject**:
One scenario event delivered into a range by an injector, producing labelled ground-truth
telemetry (`scenario-engine/scenario_engine/injectors/`;
`control-plane/worker/worker/inject_dispatch.py`; `docs/scenario-inject-execution.md`).
_Not to be confused with_: a MESL serial, which may be delivered by a person (white cell,
email, radio) rather than injected.

**MESL** (Master Event Sequence List):
The ordered serials that drive a collective exercise: phase, time, delivery method, expected
action and measure of effectiveness (`MeslEvent` in `models.py`;
`control-plane/api/app/mesl.py`).
_Not to be confused with_: a scenario timeline.

**Validator**:
The check that decides whether an objective is achieved: `opensearch_query`,
`deliverable_check`, `manual_ack`, `greyspace_breadcrumb`
(`scenario-engine/scenario_engine/validators/`; `Objective.validator` in `models.py`).
_Not to be confused with_: the gate, or `arc2.check` validation of course content.

**Detection credit**:
Credit for a detection objective, earned only when a Student's submitted query finds the
attack's ground-truth events inside the exercise window with enough precision
(`control-plane/api/app/detections/credit.py`). ADR 0005.
_Not to be confused with_: the scenario query, which is the answer key and never a grade on
its own.

**AAR** (After Action Report):
The report of one completed exercise (`AfterActionReport` in `models.py`;
`control-plane/api/app/aar_report.py`, `aar_html.py`).
_Avoid_: debrief, summary report.

**Greyspace**:
A simulated internet attached to a range: routing, DNS, rehosted sites and threat-actor
infrastructure, generated from a read-only corpus (`control-plane/api/app/greyspace/`,
`greyspace/`). ADR 0007.
_Not to be confused with_: the noise engine (`control-plane/api/app/noise/`), which simulates
activity inside the range.

## Courses and learning records

**ARC² / Course Studio**:
The module that authors, reviews, validates and packages courses; ARC² is its name and
Course Studio its role (`docs/arc2-course-studio.md`; UI `features/arc2-studio/`).
_Not to be confused with_: the LMS, which delivers releases and records outcomes.

**Course release**:
An immutable, accepted version of a course that the platform delivers. It is uploaded as a
candidate, accepted once, and superseded by the next accepted release
(`control-plane/api/app/course_releases/`).
_Not to be confused with_: release candidate (a platform build). A course "candidate" is
a course release not yet accepted.

**cmi5 / AU** (assignable unit):
cmi5 is the standard TrueNorth implements as an LMS; an AU is one launchable module of an
accepted course release, served by the SPA's AU runtime at `/au/releases/<release>/<n>`
(`control-plane/api/app/cmi5/`; `docs/cmi5.md`). A cmi5 **registration** is a Student's
enrolment; a cmi5 **session** is one AU launch.
_Not to be confused with_: lab session.

**Moodle farm**:
The Moodle instances TrueNorth runs itself for its tenants; each registered node is
a **farm node** (`ManagedMoodleNode`, `control-plane/api/app/moodle_farm/`;
`scripts/moodle-farm.sh`; `docs/moodle-integration.md`). TrueNorth is the record; results
flow back to it.
_Not to be confused with_: an external Moodle registered as a platform, which gets none of
a farm node's account binding.

## Engineering process

**Release candidate** (rc):
A pre-release tag `vX.Y.Z-rcN` on `main`, published by `.github/workflows/release.yml`
for staging acceptance (`docs/release.md`; evidence in `docs/release-notes/`).
_Not to be confused with_: a course release candidate.

**The gate**:
`scripts/dod.sh`, the one command that decides whether a change is done: the ruff ratchet,
the MOSA ratchet, OpenAPI drift and the CI pytest selection; it records a pass in
`.dod-pass`. Web checks only with `DOD_WEB=1`.
_Avoid_: "tests pass", "CI is green" (CI is not the gate, and neither alone means done).

**DoD** (Definition of Done):
Passing the gate on the committed tree (`SKILLS/10-definition-of-done.md`, `CLAUDE.md`).
_Not to be confused with_: a document saying the work is complete.

**MOSA ratchet**:
The gate step `scripts/mosa_check.py`: counts of modularity debt (vendor SDKs outside
adapters, raw SQL in the worker, god-file line counts, and more) may fall but never rise
above `.dod-mosa-baseline`. ADR 0003. Raising a baseline needs an ADR.
_Not to be confused with_: the ruff ratchet (`.dod-ruff-baseline`), the gate's other ratchet.
