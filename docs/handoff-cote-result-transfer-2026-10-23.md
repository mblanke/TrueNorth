# Claude handover: COTE Unclassified result transfer

**Date:** 2026-10-23
**Branch:** `codex/cote-result-transfer`
**Worktree:** `/Users/guapo/.codex/worktrees/cote-result-transfer/TrueNorth`
**Starting point:** `github/main` at `4c3f2559`
**Plan:** `docs/cote-unclassified-8.5-plan.md`
**Decision:** `docs/adr/0015-classification-safe-result-transfer.md`
**Technical/operator guide:** `docs/result-transfer.md`

## Objective and owner decisions

Build the COTE gap-closure programme Unclassified first. TrueNorth is installed independently
in each enclave. Secret holds names, the token-to-person mapping and authoritative numeric
grades. Unclassified retains a random opaque subject token and pass/fail. Numeric results move
low-to-high through an approved path. A data diode is the leading transport assumption as of
2026-10-24, still TBC; keep the package contract transport-neutral until its actual interface
and accreditation constraints are confirmed. Do not claim the app has reached 8.5/10 until the plan's
measured gates pass. GRIMM TinyTown, automotive benches and other physical systems eventually
sit behind a generic physical-training-asset provider; no public GRIMM control API has been
established.

## Completed in this branch

The branch contains a complete, reviewable foundation for the result-transfer contract:

- a strict `truenorth.result-transfer.v1` envelope;
- Ed25519 source signatures;
- ephemeral X25519, HKDF-SHA256 and AES-256-GCM encryption to the Secret recipient;
- destination binding and authenticated public header;
- an exact public allow-list containing only opaque token, course release, assessment,
  attempt and pass/fail;
- encrypted tenant, numeric grade, scorer version, evidence digests and correction link;
- bounded canonical `.tnresult.json` media files with no-overwrite, no-follow, regular-file,
  filename-binding and 1 MiB checks;
- a grade-free replay ledger;
- an Unclassified ciphertext-only outbox;
- Secret-only subject bindings and authoritative imported grades;
- idempotent package import, tenant checks and non-forking linear corrections;
- signed grade-free `truenorth.result-receipt.v1` acknowledgements with fixed safe rejection
  categories; and
- explicit production `TN_ENCLAVE_ID` and `TN_CLASSIFICATION_LEVEL` gates in API startup,
  Compose and installer preflight, with source/destination route enforcement; and
- Alembic revision `4d8f7a1c2b90`, ADR, operator guide and adversarial tests.

Files are under `control-plane/api/app/result_transfer/`, with focused tests under
`tests/api/test_result_transfer_*.py`.

The second increment adds `scripts/unclassified_data_inventory.py` and
`docs/security/unclassified-data-baseline.json`. It currently reports 43 schema findings and
fails on any new direct learner-identity, submitted-answer or readable-result column. The
operator/remediation guide is `docs/unclassified-data-inventory.md`. Reductions pass without
editing the baseline; additions require explicit security review.

The next increment adds the strict signed
`truenorth.subject-provisioning.v1` contract. Secret mints a random subject token and keeps
the person mapping in the same transaction; the credential sent to Unclassified contains no
name, email, service number, rank, unit or grade.

The Unclassified receiving side now persists verified credentials in
`provisioned_subject_credentials`. Import is idempotent by credential ID and digest, requires
the trusted Secret source, local Unclassified destination, unexpired validity and an active
local tenant, and leaves the credential `available` without a user mapping. The newest slice
can assign that credential once to a new identity-free TN Student using a Keycloak UUID
supplied by the caller. It creates a separate random TN ID, synthetic email and learner alias,
then records the link and assignment time. It refuses Secret execution, a foreign tenant or
destination, expiry, reuse, and any real identity attributes. It does **not** create the
Keycloak account or deliver a password; the next slice is that adapter and approved one-time
credential channel. Migration `6fa2b4d8e310` passed the full SQLite downgrade/re-upgrade
round-trip. A tenant-scoped Unclassified resolver now returns the assigned opaque token for a
local learner, and migration `7ab3c5e9f421` enforces one token per local user. This is the
prerequisite a scoring producer needs; it does not itself remove any legacy score storage.

The first quiz producer seam is also implemented but intentionally not wired to the live
route. It accepts only a completed attempt belonging to the learner and tenant, requires that
learner's immutable pinned course release, resolves the assigned opaque subject, and queues
the encrypted result in the caller's transaction. Before wiring it, change the Unclassified
quiz response, attempts, module progress, competency assessment, xAPI and LTI paths so they do
not retain or transmit readable scores or answers. Secret-mode grading must remain unchanged.

A public-key trust registry is now implemented in `app/result_transfer/trust.py` with migration
`8bc4d6fa0532`. Entries are bound to remote enclave, purpose, key ID and validity window;
rotation keys can overlap and retirement is retained. Classification rules allow only Secret
subject/receipt/recipient public keys on Unclassified and Unclassified result-signing public
keys on Secret or Top Secret. Subject credential import can now resolve the exact active
enrolled signer from the signed package. This stores no private material and is not a substitute
for the still-missing HSM/KMS private-key provider or authenticated operator enrollment UI/API.

## Verification evidence

The full repository gate passed at commit `4aa98741`:

```text
8776 passed, 164 skipped, 58 warnings in 441.64s
DoD PASS
```

It also passed the Ruff debt ratchet, MOSA modularity ratchet, OpenAPI drift check, worker task
contract check and worker table mirror check. A fresh SQLite database upgraded through the new
head, downgraded to `32270311dba6`, and re-upgraded successfully.

On this Mac, run the gate with Homebrew OpenSSL first. `/usr/bin/openssl` is LibreSSL and lacks
`openssl passwd -6`, causing six unrelated work-site template tests to fail:

```bash
PATH=/opt/homebrew/bin:$PATH RUFF_CACHE_DIR=/private/tmp/tn-ruff-cache bash scripts/dod.sh
```

PostgreSQL-only tests were skipped because `TEST_POSTGRES_ADMIN_URL` is not configured. Run the
migration upgrade/downgrade and relevant Postgres tests on Taz or another disposable Postgres
before merge.

## Adversarial review findings already fixed

1. The first media reader checked a path and reopened it, leaving a symlink/path-swap window.
   It now opens with `O_NOFOLLOW` and validates the open descriptor. Ledger receipts do the
   same, and export/ledger directories reject symlinks.
2. The first migration imported `GUID` from mutable application code. Revision
   `4d8f7a1c2b90` now carries a frozen local copy.
3. The first service used five direct `db.get` calls whose safety depended on later checks.
   Queries now include tenant or signed route/digest scope. A two-tenant test proves that a
   foreign user or course release cannot be bound/imported.
4. Correction selection depended on timestamp ordering. It now checks the explicit successor
   link, preserves subject/course/assessment/attempt, and the database prevents correction
   forks.

## Highest-priority unfinished work

The current Unclassified app is **not yet compliant with the owner data rule**. Existing users,
quiz attempts, module progress, enrollments and other learning tables still carry names and/or
readable numeric scores. The new outbox proves a safe destination format; no quiz, practical
exercise or class-close-out producer calls it yet.

Continue in this order:

1. Use the completed schema inventory to remove each database finding. Add the still-missing
   behavior tests for API responses, xAPI/LRS, LTI, logs, traces, object/search stores,
   support exports and backups.
2. Complete pseudonymous login provisioning: use an adapter to create the identity-free
   Keycloak account first, assign the signed credential to its returned UUID, and deliver a
   one-time login through an approved separate channel. Add revocation and recovery. Never
   use the TN `users.id` as the opaque token or copy the person mapping to Unclassified.
3. Put signing/decryption keys behind a key-provider adapter and expose authenticated operator
   enrollment for the implemented public-key trust store. Keep generated test keys and a
   controlled development provider; production must use the approved HSM/KMS boundary.
4. Add quarantine/import workflow: copied, scanned, verified, awaiting mapping/release,
   accepted/rejected. Enforce package age/count policy and audit every transition. The current
   media helper assumes an external malware check.
5. Integrate one producer end to end, preferably quiz submission. Seal the numeric result in
   the same transaction boundary, persist only pass/fail on Unclassified, commit Secret import
   idempotently by package ID, and return the signed receipt. Preserve normal Secret-mode
   grading behavior.
6. Repeat for practical exercises and class close-out, then add staff UI/API for outbox,
   quarantine, correction approval and receipts without displaying low-side grades.
7. Demonstrate 100 synthetic results between two isolated TN databases, including duplicate
   media, lost receipt, correction, reordering, wrong signer/recipient/destination, unknown
   subject/release and destination outage.

Do not connect a general Unclassified-to-Secret network path. Do not log decrypted claims or
keys. `consume_package` requires its callback to commit the idempotent Secret database write
before returning; the filesystem receipt is recorded afterward so a crash safely retries.

## Programme work after the result slice

- Finish the class, exercise and dual-calendar journey described as U1-Slice 2 in the plan.
- Qualify enterprise, cloud and OT templates on real writable vCenter.
- Complete GRIMM vendor/safety discovery before writing a vendor adapter. Build the generic
  physical-asset inventory/reservation/preflight/evidence/reset/custody contract first.
- Execute bilingual/accessibility, scale/endurance, security authorization, content approval
  and interoperability gates. These need real environment evidence; unit tests do not earn the
  8.5 score.

## Working rules

Read `CLAUDE.md`, `docs/AGENTS.md` and the relevant `SKILLS/tn-*/SKILL.md`. Run focused tests
while iterating and the full DoD before claiming a slice complete. Preserve the original
checkout's untracked user documents; all implementation work for this slice belongs in the
managed worktree above.
