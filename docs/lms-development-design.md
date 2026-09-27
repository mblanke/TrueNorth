# TrueNorth: learning and professional development

Design proposal · 26 September 2026 · Not implemented in the application

## 1. Product purpose and accepted context

Help a member build foundations, demonstrate capability, discover a specialty, contribute to a team, and develop other people over a career. Deliver a dependable academic LMS inside that longer journey.

The design uses the pathway supplied by the programme stakeholder: three years at Algonquin, then eight months at CTU Kingston with some instruction at RMC, covering DP1–2. This is a design premise, not an independently verified qualification mapping. Do not infer a DP boundary between institutions, invent later DP/rank relationships, or treat elapsed time as a qualification. Exact calendars, credit rules, assessment policies, and authority mappings come from approved programme sources.

Years 4–6 and 7–10 in the prototype are illustrative development horizons measured from programme entry. Individual progression may differ. Course titles, people, deadlines, observations, and opportunities in the prototype are fictional.

The desired result is a strong foundation for everyone, deep specialties for many, room for unusual combinations of ability to emerge, and a growing body of reusable institutional knowledge.

## 2. Product principles

1. One continuing member identity and development record; phase-specific daily workspaces.
2. Required learning, exploration, and specialist depth remain visible and distinct.
3. Academic grades, assessed task performance, competency assertions, and formal qualifications are separate records with separate authorities.
4. Every consequential assertion has attributable evidence, a rubric/version, an assessor, a date, and a scope.
5. Development opportunities include a sponsor, mentor capacity, protected time, prerequisites, an output, and a review date.
6. Members can choose interests, nominate work, correct records, and revisit specialties. Never rank people by a hidden potential score.
7. Institutional systems retain authority over their records. Integrate course delivery and registrar records where appropriate; avoid requiring duplicate submission or grading.
8. Missing data, failed synchronisation, and unassessed capability must not display as zero achievement.

## 3. Daily experiences

| Workspace | Primary question | Default view | Main actions |
|---|---|---|---|
| Member at college | What do I need to learn and do this term? | My semester | Resume a unit; submit work; see feedback; plan the week |
| Member at CTU/RMC | What am I preparing to demonstrate? | My training | Review briefing; attend instruction; practise; submit evidence; inspect assessment |
| Member in a unit | What am I developing and contributing? | My development | Follow supervised work plan; pursue a specialty; review evidence with mentor |
| Instructor | Who needs teaching, feedback, or assessment? | Teaching | Manage an offering; review submissions; support learners; moderate assessment |
| Mentor | What can help this member take the next step? | Mentoring | Review member-selected evidence; agree goals; propose an opportunity; record follow-up |
| Programme lead | Are teaching and handoffs producing the intended outcomes? | Programme | Inspect delivery gaps, receiving-unit feedback, transition queues, and opportunity access |
| Programme administrator | Are structures and records dependable? | Administration | Manage terms, offerings, identities, versions, policy, imports, and reconciliation |

A workspace switch only changes presentation among existing grants. It never grants a role. A member can be a learner in one offering and an instructor in another.

## 4. Information architecture

| Current journey observed in source | Proposed journey |
|---|---|
| Career path → catalogue → descriptive course outline | Phase-aware home → enrolled offering → resumable unit → submission and feedback |
| Quiz and scenario metadata shown on course detail | Activity action with enrollment, attempt, practice/assessment mode, and return context |
| Separate progress and competency screens | Portfolio with distinct work, assessed skills, formal records, and contributions |
| Recommendation text and a fixed-delay reload | Evidence-linked activity suggestion, explicit job state, and mentor/sponsor development workflow |
| Transcript omits unattributable exercises | Participant-bound attempt evidence reviewed before it supports a member record |

Member navigation:

```text
Learning
  Home                     phase-specific work and next commitments
  Programme                curriculum version, current phase, dependencies, transition
  My learning              enrolled offerings and planned development activities
    Offering               Overview / Units / Assignments / Feedback / Schedule
      Unit                 material / practice / assessed activity
  Portfolio                selected work, reflections, reviewed evidence, qualifications
  Opportunities            Explore / Build depth / Contribute
  Development plan         goals, mentor, protected time, review dates

Teaching                   assigned offerings and assessment queues
Mentoring                  assigned members and agreed development plans
Programme management       delivery, transitions, evidence coverage, access to opportunity
Administration             structures, record reconciliation, permissions, audit
```

Keep the existing range/authoring/operations destinations as separate workspaces for authorised staff. A learner reaches an exercise through the relevant activity with their offering, attempt, and return destination intact.

Canonical routes (proposed):

| Route | Purpose |
|---|---|
| `/learning/home` | Phase-aware home |
| `/learning/programme` | Programme enrollment and curriculum version |
| `/learning/my-learning` | Member's offerings and activities |
| `/learning/offerings/:offeringId` | Scheduled delivery of a published course version |
| `/learning/offerings/:offeringId/units/:unitId` | Resumable unit workspace |
| `/learning/offerings/:offeringId/assignments/:assignmentId` | Instructions, submission, feedback, reassessment |
| `/learning/portfolio` | Developmental evidence and authoritative record views |
| `/learning/opportunities/:opportunityId` | Opportunity requirements, capacity, sponsor, and application |
| `/learning/development-plan` | Agreed goals and review cycle |
| `/teaching/offerings/:offeringId` | Instructor view of an assigned offering |
| `/mentoring/members/:memberId` | Relationship-scoped development view |
| `/programme/transitions/:transitionId` | Handoff package and receiving acknowledgement |

Preserve `/learning/courses` as the catalogue and `/learning/courses/:id` as the course definition. A catalogue course may have several offerings; never silently pick an offering when resolving an old course link. Add an explicit offering choice for enrolled members.

Move `/learning/career-path` to `/learning/programme` with query parameters such as `qual` preserved. Preserve `/learning/qualifications` and the older top-level redirects through the same destination. Redirect `/learning/progress` and `/my-progress` to the transcript section of `/learning/portfolio`; redirect competency links to its skills section. Keep curriculum authoring and course administration links compatible until their replacement workflows are complete. Route redirects must be exercised with direct URLs, refresh, back/forward, filters, and entity IDs.

## 5. Screen and interaction design

### Member home

At college, show term identity, current course work, a combined weekly agenda, upcoming submissions, returned feedback, and the next mentor commitment. Place one optional exploration opportunity below required commitments. No overall readiness percentage.

At CTU/RMC, foreground the current training block, practical preparation, scheduled assessment, remediation, and transition requirements. Preserve the portfolio from college.

During operational development, foreground the agreed development plan, supervised work, specialist projects, mentoring, and contributions. Job/mission systems remain the operational source of truth.

### Course offering

Use a left unit outline and a focused content area, with an adjacent or stacked assignment/feedback summary. Support accessible text, files, captions/transcripts, formative questions, external learning launches, range activity launches, and instructor contact details. Display activity purpose and whether it is practice or assessed.

Assignments carry published rubrics, due dates with timezone, extension applicability, submission history, draft/submitted state, reviewer feedback, moderation status, and reassessment policy. A learner sees which revision was graded. Instructor edits to published requirements create a new version and explain effects on active learners.

Use an external learning launch when an institution owns the activity. Show source and last confirmed update on imported grades; a stale or failed sync must remain distinguishable from an ungraded assignment. Do not duplicate authoritative gradebooks without an agreed record ownership contract.

### Programme map

Show the three college years as terms from actual calendar data, then the eight-month CTU phase with its RMC activities. Treat DP1–2 as the stakeholder-supplied overall span until approved mappings establish exact boundaries. Later development uses capability goals and opportunities, not invented qualification names.

Selecting a requirement reveals its source/version, prerequisite rules, linked teaching, assessed evidence, and status. Distinguish planned, underway, assessed, awaiting approval, and formally awarded. A blocked requirement names the missing dependency and the available support route.

### Portfolio

Provide four views: selected work, assessed skills, formal records, and contributions. Every entry identifies ownership, provenance, review state, visibility, and the source of any claim. Keep a member's reflection separate from an assessor's decision.

A practice artefact may be nominated for review, but cannot grant a qualification. An assessment decision can be contested through a recorded correction/appeal workflow. Revoked or superseded claims remain auditable. Restricted evidence stays in its approved environment; the LMS stores only permitted metadata and references.

### Opportunities and development plan

An opportunity states its purpose, broad category, prerequisites, selection criteria, delivery mode, duration, protected time, sponsor, mentor, capacity, output, review date, and accessibility arrangements. Examples include short explorations, guided projects, supervised research, specialist practice, and teaching contributions.

The workflow is `interested → discussed → applied → reviewed → approved/waitlisted/declined → active → reviewed → closed`. Display decisions and reasons. Interest is not application; application is not a posting or funded time allocation. Show insufficient mentor capacity and unsponsored opportunities honestly.

A development plan contains a small number of member-agreed goals, the supporting evidence or interest, proposed activity, mentor, organisational sponsor, time allocation, next review, and outcome. Revise the plan after review; retain its history.

### Teaching, mentoring, and programme management

Instructors work from concrete queues: submissions awaiting review, missing required work, requested support, assessment moderation, and curriculum defects. Support flags state an observable reason; they are not inferred diagnoses.

Mentors see assigned relationships and explicitly shared evidence. Record observable behaviours and development actions rather than personality labels. Private practice notes are not exposed to supervisors by default.

Programme leads see transition acceptance, assessed outcome coverage, opportunity capacity/access, and aggregated receiving-unit feedback. Every measure includes its denominator, time window, data coverage, and drill-down permissions. Small groups may need suppression. Avoid a member leaderboard.

## 6. End-to-end journeys

### A. Semester learning

An enrolled member opens home → resumes a published unit → saves an assignment draft → submits a revision → receives moderated feedback → corrects or reassesses if permitted → sees the authoritative result and optional portfolio nomination.

Evidence: the same revision and rubric appear in learner and assessor views; reconnect/retry cannot duplicate submission; a pending grade is not shown as zero. A failed upload retains the draft and gives a retry.

### B. Practical development

Member enters a course activity → sees practice/assessment rules → receives an attributable exercise attempt → launches an authorised range session → resumes after reconnect → reviews validator and instructor evidence → returns to the activity with the recorded result.

Evidence: participant identity and attempt ownership are recorded before launch; team outcomes do not automatically grant individual credit; replayed worker events do not duplicate evidence or completion. A failed range launch leaves the academic attempt recoverable and records no failure grade by itself.

### C. Discovery to depth

Member nominates an investigation → mentor records a specific observation → they agree a bounded project → a sponsor allocates time and a reviewer → the member completes and revises work → reviewed output becomes portfolio evidence and, with permission, a teaching contribution.

Evidence: the member can change direction; a declined opportunity includes a reason and next route; review remains human-owned; the contribution has a named maintainer before publication.

### D. Institutional handoff

Programme team prepares a versioned package → member checks factual accuracy and chosen developmental disclosures → authorised staff confirm formal records → receiving owner reviews → gaps generate a support plan → owner acknowledges receipt → follow-up is scheduled.

Evidence: status remains pending until acknowledged; rejected or incomplete packages can be revised without losing prior decisions; no inferred qualification is awarded. Institutional transfers require explicit identity reconciliation and scoped access, never simply removing tenant filters.

## 7. Data model and records of authority

Extend the current FastAPI/SQLAlchemy and Angular Material application. Preserve the existing qualification/PO/EO spine, catalogue, module content, quizzes, and range services where their contracts fit.

```text
Programme → ProgrammeVersion → Requirement + prerequisite edges
ProgrammeVersion → ProgrammeEnrollment(member, cohort, dates, state)
Institution → AcademicPeriod → CourseOffering(CourseVersion, staff, calendar)
CourseOffering → OfferingEnrollment(member, attempt number, status)
CourseOffering → Activity/Assignment → Attempt → SubmissionRevision
Attempt → ExerciseParticipation → EvidenceReference
EvidenceReference → AssessmentDecision(rubric version, assessor, moderation)
AssessmentDecision → RequirementSatisfaction → authorised QualificationAward
Member → PortfolioEntry → permitted EvidenceReference
Member ↔ MentoringRelationship → DevelopmentPlan → Goal
Opportunity → Application → Placement(time, sponsor, mentor) → Contribution
ProgrammeEnrollment → TransitionPackage → ReceivingAcknowledgement
ExternalRecord → SourceMapping + SyncReceipt + ReconciliationIssue
```

The current enrollment uniqueness is `(user_id, course_id)`. Introduce offering enrollments to support repeats and multiple deliveries; do not erase or overwrite the historical enrollment. Course versions and programme versions must be immutable once relied upon by an assessed attempt. Separate accommodations/adjustments from public rubric content and restrict the sensitive supporting details.

Formal awarding authority must be explicitly configured per requirement. Map multiple teaching and assessment activities to an outcome using versioned mappings; distinguish required evidence from supporting evidence. Use precedence rules for equivalent credit, recognition of prior learning, waivers, and supersession. An authorised decision and its reason must accompany exceptions.

## 8. Proposed API and event contracts

These are proposed contracts, not existing endpoints.

| Contract | Essential behaviour |
|---|---|
| `GET /learning/me/home?period_id=` | Phase, assignments, commitments, provenance and freshness; typed missing/error states |
| `GET /programme-enrollments/{id}/requirements` | Versioned requirement statuses and evidence; scope checks |
| `GET /offerings/{id}` | Published course snapshot, staff, units, calendar, source authority |
| `POST /assignments/{id}/submissions` | Immutable revision, idempotency key, enrollment validation |
| `POST /attempts/{id}/exercise-session` | Participant binding, authorised mode, retry-safe launch |
| `POST /assessment-decisions` | Assessor scope, rubric version, evidence, moderation, audit |
| `POST /portfolio-entries/{id}/review-requests` | Explicit member nomination and visibility scope |
| `POST /opportunities/{id}/applications` | Eligibility explanation, capacity handling, no automatic placement |
| `PATCH /development-plans/{id}` | Version/concurrency check, member/mentor scope, preserved history |
| `POST /transitions/{id}/acknowledgements` | Receiving-owner scope, exact package version, outstanding gaps |
| `GET /jobs/{id}` | Queued/running/succeeded/failed status for exports, syncs, recommendations |

Commit business changes and durable outbox events in the same transaction. Consumers deduplicate by event ID and source revision. Events include submission created, assessment released, evidence reviewed, requirement satisfied, award approved, opportunity decided, and transition acknowledged. External xAPI/LTI delivery is downstream of committed records; failure queues an integration retry without losing learner work.

AI may explain approved material with citations, propose remediation from observed gaps, help find eligible opportunities, and draft summaries. It cannot award qualifications, decide placement, manufacture evidence, or write hidden potential scores. Assessment mode controls permitted assistance. Recommendations name evidence, catalogue item IDs, eligibility, and unavailable dependencies; human reviewers own consequential decisions.

## 9. Access, accessibility, and reliability

- Server checks combine tenant/institution agreements, relationship, offering assignment, role, and record sensitivity. Browser navigation is not the access boundary.
- A mentor relationship has purpose, scope, start/end, and revocable sharing. Instructor access is bounded to assigned offerings. Programme administration does not automatically grant access to private notes.
- Explicit member-facing visibility for portfolio entries; separate formal record retention from voluntary sharing. Audit reads of sensitive records and all assessment/award changes.
- Keyboard operation, semantic headings, visible focus, readable contrast, captions/transcripts, responsive reflow, and locale-ready English/French content. No meaning conveyed by colour alone.
- Show dates in the member's timezone with institutional deadline context; handle daylight saving changes and extensions deterministically.
- Autosave drafts with visible status and version checks. Retry uploads and job launches safely. Do not store restricted evidence in browser caches. Provide low-bandwidth material where permitted.
- Empty states, permission failures, stale integrations, interrupted assessments, unavailable ranges, and exhausted mentor capacity have distinct messages and recovery actions.

## 10. Delivery sequence, ownership, and recovery

One implementation owner controls shared routes, schemas, and contract changes within a slice. The following are work boundaries, not authorisation to spawn agents or deploy.

| Slice | Usable outcome | Likely files/contracts | Migration and recovery | Acceptance evidence |
|---|---|---|---|---|
| 0. Record integrity | Existing completion and progress can be trusted | `routers/courses.py`, `quizzes.py`, `adaptive_learning.py`, schemas, enrollment service | Additive policy metadata where needed; audit existing completions without mass revocation | Required modules enforced; same-user/tenant checks; unassessed is distinct from failed; old grades preserved |
| 1. One semester | Member can follow offerings, deadlines, submissions, and feedback | `features/training`, new learner home/offering features, courses API, models, API client | Programme/course versions, academic periods, offerings and offering enrollment; map legacy IDs; feature-flag new home | Complete and repeat one course without overwriting history; instructor moderation; direct links; external-grade ownership |
| 2. One attributable practical | Range activity returns learner evidence and a debrief | Exercise API, quiz API, worker tasks, progress services | Participation, attempt, evidence and outbox tables; keep existing exercise URLs | Individual/team attribution; replay protection; failed launch recovery; no premature completion |
| 3. One development cycle | Mentor and member complete a sponsored project | Portfolio, mentoring, opportunity APIs and Angular features | Relationships, goals, applications, placements, review history; revoke grants independently | Share/revoke evidence; record time and sponsor; review output; no automatic qualification |
| 4. One institutional handoff | Receiving owner accepts a versioned package | Programme management, transcript integration, transition services | Source mappings, package versions and acknowledgements; reconcile duplicates manually | Identity match, scoped access, rejection/revision, receiving confirmation, follow-up |
| 5. Institutional learning | Reviewed contributions and receiving feedback improve delivery | Curriculum authoring, programme analytics, integration workers | Contribution lifecycle and aggregate snapshots; retain historical sources | Publish with maintainer, retire stale material, display denominators, measure opportunity access |

Roll out to one willing cohort and a small instructor/mentor group. Use additive migrations, resumable backfills with counts and reconciliation reports, feature switches, and stable old routes. Disable new UI safely without dropping history. Do not dual-write authoritative grades indefinitely; define cutover ownership per institution before production integration.

## 11. Success measures and decisions before implementation

Baseline these during the pilot: time to resume work, time to receive feedback, missing/duplicate records, handoff acknowledgement time, access to mentored opportunities, placement completion, reviewed contributions reused, and receiving-unit feedback on demonstrated tasks. Track distributions and denominators rather than a single readiness score. Agree numeric targets after a baseline exists.

Programme owners must supply: approved curriculum and DP mappings; actual academic calendars; authoritative student/grade/qualification systems; assessment/appeal rules; identity and information-sharing agreements; mentor capacity; protected-time authority; evidence retention and classification rules. These are implementation dependencies, not reasons to postpone the design.

## 12. Prototype and verification boundary

The accompanying interactive proposal covers member home in three phases, programme progression, a course workspace, evidence review, development opportunities, mentoring, instructor queues, and programme handoffs. The CTU flow links briefing → practice simulation → attributable sample evidence → formative debrief → a proposed mentor discussion. The contribution view shows technical review, teaching/accessibility review, and maintenance before reuse by another cohort. The programme-lead view can simulate receiving acknowledgement or correction requests. All data is fictional. Actions change local preview state only; no submissions, messages, applications, personnel decisions, or range launches occur.

For application implementation, run targeted API/contract tests, fresh/upgrade migration checks, worker replay tests, role/tenant isolation tests, browser journeys, accessibility checks, and the repository DoD gate. The design prototype does not establish production readiness.

Current source anchors: `control-plane/web/src/app/app.routes.ts`; `features/training/course-detail.component.ts`; `features/my-progress/my-progress.component.ts`; `control-plane/api/app/models.py` (Course, CourseModule, Enrollment, ModuleProgress); `routers/courses.py`; `routers/quizzes.py`; `routers/adaptive_learning.py`; `control-plane/worker/worker/tasks.py`.

### Design verification performed

- JavaScript syntax check with the native Node runtime passed. The HTML fragment renders through the bundled visualization renderer and uses local-only preview interactions.
- Browser checks verified member phase changes, course practice feedback, instructor submission review, mentor evidence review, programme stage selection, and handoff detail navigation.
- Browser checks verified CTU sample evidence → debrief → development discussion; reviewed contribution detail; and simulated receiving acknowledgement/reset.
- Mentor portfolio view excludes the member's private reflection. Its formal-record view states the separate access requirement. These are demonstrated UI rules, not tested production authorisation.
- Opportunity saves are independent per item; saving one does not mark another saved. Fixed a state-restoration defect that returned detail pages to home.
- Inspected desktop layouts in light and dark appearance. Checked the home and assessment layouts at a 320px browser width; corrected table overflow and confirmed the assessment surface and its content both measured 286px, with no right-edge overflow among its tables, sections, buttons, and selects.
- Browser console inspection returned no error entries for the exercised flows. This is a focused interaction review, not a comprehensive accessibility or cross-browser audit.
- Repository `bash scripts/dod.sh` was attempted and stopped before lint/tests. It reports Ruff unavailable, but direct execution and `file .venv/bin/python` identify the underlying issue: the interpreter is a Linux x86-64 ELF binary on this macOS host (`exec format error`). The application gate has not passed. Application source was not modified by this design task.
