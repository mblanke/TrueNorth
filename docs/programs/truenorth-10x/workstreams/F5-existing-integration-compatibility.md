# Foundation F5 — existing integration and compatibility closure

Status: proposed after F0. Suggested branch: `claude/foundation-f5-integration-closure`.

## Closure outcome

Every existing release-note integration limitation is either reproduced and fixed, proven
resolved and removed from current notes, or deliberately retained with a regression test,
operator workaround, owner and exit condition.

## Reproduction-first inventory

Recheck current source and supported upgrade fixtures before creating tickets for:

- cmi5 Moodle activity/release binding;
- LTI-launched Student enrolment and LTI handoff WebSocket behavior;
- telemetry search for indices created before the v1.0.0 upgrade;
- multi-tenant `PLATFORM_TENANT_ID` operational requirement;
- LTI HTTPS installation/preflight behavior;
- pre-connection-record vSphere resources, shared-connection administration and abandon
  cleanup truth;
- failed alert delivery and ARC² backup quiescence;
- any Moodle grade/result path whose installed-host behavior differs from current docs.

F0 also routes current non-integration limitations rather than dropping them: simulated
injects/no host sensors go to F3/F7 and workstreams 02/04; team exercise scoring/standings go
to workstream 02; English-only/accessibility and the optional global keyboard/command-palette
decision go to F6; test/draft content goes to F7.

Current source includes substantial Moodle installer/result functionality. Treat old claims as
test hypotheses, not requirements.

## Delivery method

1. Build minimal upgrade/integration fixtures and reproduce each item at current main.
2. For a resolved item, add/identify the preventing test and update F0 evidence/notes.
3. For a live defect, create one bounded ticket with API/UI/job/persistence path, tenant and
   failure acceptance, migration needs and rollback.
4. Fix one behavioral outcome per PR; regenerate OpenAPI/Angular contracts where affected.
5. Run real Moodle/CATAPULT or supported external implementation lanes where the behavior
   cannot be proven with mocks.

## Acceptance

- No limitation remains merely because an old release page says so.
- Upgrade fixtures prove legacy-index/data behavior without editing historical migrations.
- LTI/cmi5 sessions preserve tenant, enrolment and authorization boundaries.
- WebSocket/session changes have expiry, replay and foreign-tenant tests.
- Multi-tenant platform operations have an explicit authority model and safe migration.
- Operator-visible manual cleanup and failure states are truthful.
- Current release notes list only current, scoped limitations.

## Recovery

Use additive migrations, feature flags/dual-read where needed and disposable external systems.
Do not rewrite old Alembic revisions or production indices in place without a tested recovery.

## Skills

Primary: `tn-telemetry-lms`; add `tn-api-contracts`, `tn-tenant-auth`,
`tn-schema-migrations`, `tn-job-lifecycle` and `tn-release-verification` per ticket.
