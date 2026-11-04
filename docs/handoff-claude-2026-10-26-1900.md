# Claude handoff — 2026-10-26 19:00 America/Toronto

**Living document:** update this before the 19:00 handoff if the working tree or evidence
changes. Trust current code/configuration and fresh commands over older status documents.

## Objective and owner decisions

Continue `docs/cote-unclassified-8.5-plan.md` toward an evidence-backed 8.5/10
Unclassified IOC, then repeat the signed TN baseline into Secret and higher enclaves.

- Unclassified must not retain a learner's real identity or readable numeric grade.
- Unclassified retains pseudonymous learner state and pass/fail; Secret retains the person
  mapping and authoritative grade.
- Low-to-high result movement will most likely use a data diode, still TBC. Keep the package
  and transport seam neutral until the vendor/authority interface is confirmed.
- Do not create a general Unclassified-to-Secret network connection.
- GRIMM TinyTown, car-hacking and other physical systems remain in scope after vendor and
  safety discovery.

## Repository state at 14:36 EDT

- Checkout: `/Users/guapo/Documents/Projects/TrueNorth`
- Branch: `main`
- HEAD: `0e045bc401e7` (PR #300 already merged)
- `main...github/main`: `0 0` against the local remote-tracking ref at this check.
- This slice is local and uncommitted. Do not claim it is on GitHub yet.
- Do not stage, edit or delete the unrelated untracked files listed under "Preserve" below.

## Completed in the current local slice

Pseudonymous Keycloak provisioning is now implemented behind an adapter boundary:

1. Create a disabled, marker-tagged Keycloak learner with no password or real identity.
2. Bind the verified imported subject credential to Keycloak's returned UUID.
3. Delete the disabled IdP orphan when TN assignment fails.
4. Activate or recover by rotating a temporary password, passing it only to an injected
   approved-channel callback, then enabling the account after successful delivery.
5. Re-disable on delivery failure; password values are excluded from repr and errors.
6. Revoke by disabling Keycloak, deactivating the TN Student and marking the credential
   `revoked`, which blocks recovery and result-token lookup.
7. Refuse lifecycle operations against an ordinary/unmarked Keycloak account.
8. Keep every TN lookup tenant- and destination-enclave-scoped.

No public password-returning API or delivery channel was added. That is deliberate: the
security authority must approve the separate operator channel first.

The next DLP increment is also local and complete:

- Added an executable Unclassified quiz behavior inventory. It captured 19 initial findings
  across DB, API, xAPI and LTI; its accepted finding set is now empty.
- Unclassified quiz submission now returns only `attempt_id`, `passed` and `late`.
- Unclassified emits only the pass/fail attempt xAPI event with no score; it emits no
  per-question correctness and queues no numeric LTI grade task.
- Secret keeps the full score, answer-review, xAPI and LTI behavior.
- The numeric grade is sealed into the encrypted outbox before the same transaction commits.
  Failure rolls back submission and returns a generic 503; it does not retain or lose a grade.
- Unclassified attempts and module progress retain pass/fail state but no answers or numeric
  values, and no competency grade row is created. Secret persistence remains unchanged.
- The Angular quiz result screen handles both contracts and does not claim that an outbox row
  has already been imported into Secret.
- Runtime routing requires an enrolled `result-recipient` public key plus
  `TN_RESULT_TRANSFER_DESTINATION_ENCLAVE`, `TN_RESULT_TRANSFER_RECIPIENT_KEY_ID` and an
  owner-only regular signing-key file named by `TN_RESULT_TRANSFER_SIGNING_KEY_FILE`.
- Course/class close-out now keeps completion state/time but nulls final numeric/max/letter
  grades on Unclassified, including the shared Moodle-driven completion path. Secret retains
  existing authoritative grade behavior.
- Added an executable practical-exercise behavior inventory with 10 accepted findings across
  shared DB score/evidence, API, xAPI, LTI and the competency worker. Do not scrub these fields
  yet: first add a participant-attributed attempt/release/evidence record so a shared exercise
  total is not misrepresented as every participant's individual result.
- Moodle result pulls now inspect each signed page before application. On Unclassified, any
  readable quiz-grade row refuses the whole page with a grade-free 409, leaves its cursor
  unchanged, and writes no Moodle grade ledger, quiz attempt or progress update. Secret can
  ingest the same page later without a reset. This is interim containment; a sealed Moodle
  producer/pass-fail projection is still open.
- The DoD worktree marker now fingerprints non-ignored untracked files as well as the binary
  tracked diff. Previously, a new migration/source/test could change after a green run without
  invalidating `.dod-pass` until it was staged. The shell and PowerShell gates share the same
  platform-neutral Python implementation and its regression test.

### Files in this slice

- `control-plane/api/app/result_transfer/identity_backends/{base,keycloak,__init__}.py`
- `control-plane/api/app/result_transfer/pseudonymous.py`
- `tests/api/test_result_transfer_keycloak_identity.py`
- `tests/api/test_result_transfer_pseudonymous.py`
- `install/roles/tn_keycloak/defaults/main.yml` (least-privilege role comments only; the
  existing `manage-users` permission already covers these calls)
- `docs/result-transfer.md`
- `docs/cote-unclassified-8.5-plan.md`
- `docs/handoff-cote-result-transfer-2026-10-23.md`
- this handoff
- `control-plane/api/app/routers/quizzes.py`
- `control-plane/api/app/result_transfer/quiz_producer.py`
- `control-plane/api/app/models.py`
- `control-plane/api/app/schemas.py`
- `control-plane/api/app/enrollment.py`
- `control-plane/api/alembic/versions/9d7e2c4a6b81_nullable_unclassified_quiz_scores.py`
- `tests/conftest.py`
- `tests/api/test_unclassified_quiz_behavior_inventory.py`
- `tests/api/test_result_transfer_quiz_producer.py`
- `tests/api/test_migration_unclassified_quiz_scores.py`
- `tests/api/test_enrollment.py`
- `tests/api/test_unclassified_exercise_behavior_inventory.py`
- `control-plane/api/app/moodle_results/{service,runner}.py`
- `control-plane/api/app/routers/integrations.py`
- `tests/api/test_moodle_results.py`
- `scripts/worktree_fingerprint.py`
- `scripts/dod.{sh,ps1}`
- `tests/scripts/test_worktree_fingerprint.py`
- `docs/security/unclassified-exercise-behavior-baseline.json`
- `docs/security/unclassified-behavior-baseline.json`
- `docs/unclassified-data-inventory.md`
- `docs/interfaces/openapi.json`
- `control-plane/web/src/app/core/api/schema.d.ts`
- `control-plane/web/src/app/features/quiz-player/quiz-player.component.{ts,spec.ts}`

## Verification evidence

- Focused lint: passed.
- Pseudonymous + Keycloak lifecycle tests: `12 passed`.
- Complete result-transfer suite: `63 passed`.
- MOSA modularity ratchet: passed with no baseline growth.
- OpenAPI, worker task contract and worker table mirror: no drift.
- Full DoD, rerun with normal localhost/subprocess permissions:
  `8807 passed, 164 skipped, 59 warnings`; **DoD PASS**.
- The first restricted run failed only because the execution sandbox prohibited localhost
  listeners and Packer plugin processes; the unrestricted rerun cleared all those failures.
- The default DoD web checks were skipped by design. Targeted evidence for the outward DLP
  increment passed: 68 focused API tests, API generation/check, Angular lint, the production
  Angular build and all 13 focused quiz-player tests.
- After the persistence/outbox increment: 67 quiz/result-transfer tests passed; the
  behavior ratchet and fail-closed test both passed; 32 focused contract/migration tests
  passed with one PostgreSQL-only skip; the new SQLite migration upgrade/downgrade/re-upgrade
  passed; OpenAPI and generated Angular types match; Angular lint and production build passed.
- After the course-close-out increment: 67 focused enrollment, migration, downgrade and
  Moodle tests passed; focused Ruff passed. OpenAPI and generated Angular types were refreshed.
  Angular lint and production build passed.
- The practical-exercise inventory test and focused Ruff passed with 10 accepted findings.
  The full DoD result above includes the close-out and exercise-inventory increments.
- After the Moodle boundary increment: all 45 Moodle result tests and focused Ruff passed.
  The full DoD result above includes this increment.
- The worktree-fingerprint regression test and focused Ruff passed. The final DoD marker
  includes both tracked changes and all current non-ignored untracked content.

The temporary macOS verification interpreter is `/tmp/truenorth-codex-venv311/bin/python`.
The checked-in `.venv` is currently a Linux ELF environment and cannot execute on this Mac.
Use the Homebrew-backed Python 3.11 environment: uv's standalone runtime is under the user
home, which the ARC2 Seatbelt tests correctly refuse to expose to confined jobs.
For a repeat gate:

```bash
PATH=/opt/homebrew/opt/openssl@3/bin:$PATH \
PY=/tmp/truenorth-codex-venv311/bin/python \
RUFF_CACHE_DIR=/tmp/tn-ruff-cache \
bash scripts/dod.sh
```

## Adversarial review result

No blocking objection remains in this slice. Important residual boundary: cross-system
database/Keycloak operations cannot be atomic. The implementation fails safely—new accounts
stay disabled until delivery, failed TN assignment deletes the disabled orphan, failed
delivery leaves the account disabled, and a TN flush failure after revocation leaves Keycloak
disabled rather than granting access. The eventual operator workflow needs reconciliation and
audit events for interrupted operations.

## Exact next work

1. Re-read `CLAUDE.md`, `docs/AGENTS.md`, and the relevant task skill before editing.
2. Review this local diff; do not redo the completed identity-adapter slice.
3. Add the platform-operator orchestration only after the owner/security authority selects
   the one-time credential delivery channel. Never return a password from an API.
4. Continue the behavior-level DLP inventory across logs, traces, exports and backups. The
   live quiz path and class close-out are done; exercises have a pinned 10-finding baseline;
   readable Moodle grade pages are blocked pending their sealed producer.
5. Provision the three result-transfer runtime settings and enrolled recipient key in a
   disposable Unclassified deployment, then demonstrate export/import/receipt with Secret.
6. Design participant/release/evidence attribution for practical exercises, then repeat the
   sealed-result/pass-fail-only pattern. Course/class close-out grade removal is complete.
7. Follow with HSM/KMS key-provider work, quarantine/import workflow and the 100-result
   two-installation demonstration.

Any endpoint/schema change requires `tn-api-contracts`, OpenAPI regeneration, Angular client
generation and contract checks. Any auth/tenant change requires `tn-tenant-auth` and negative
two-tenant tests. Run the full DoD before claiming done.

## Preserve

These pre-existing untracked paths are not part of this slice:

- `codereview1.md`
- `docs/arc2-programme-production-plan.md`
- `docs/cloud-range-requirements.md`
- `docs/release-plan-v1.2.0.md`
- `docs/review/deep-dive-2026-10-21-probes.patch`
- `docs/review/deep-dive-2026-10-21.md`
- `truenorth-ansible-range-kit/`

## Known documentation drift outside this slice

`docs/README.md`, `docs/current-state.md` and `RESUME.md` contain older release/test counts
and should not be used as evidence for this work. Updating the global state set is a separate
documentation reconciliation task; do not mix it into the security slice without reviewing
current staging/release evidence.
