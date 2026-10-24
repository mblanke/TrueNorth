# Result transfer: current foundation and operator flow

The result-transfer module carries a numeric assessment result from an Unclassified
TrueNorth installation to an independent Secret installation. Unclassified retains an opaque
subject token and pass/fail. Secret maps that token to the learner and stores the numeric
record. The current implementation is a library and persistence foundation; it is not yet an
administrator screen or an accredited cross-domain solution.

## What exists

- `app/result_transfer/contract.py`: versioned envelope, Ed25519 signature,
  X25519/HKDF/AES-256-GCM encryption, exact public-field allow-list and correction link.
- `app/result_transfer/media.py`: bounded controlled-media export/import, no overwrite,
  symlink/device refusal, filename binding and a grade-free consumption ledger.
- `app/result_transfer/models.py`: ciphertext-only Unclassified outbox, Secret subject
  bindings and authoritative imported grades.
- `app/result_transfer/service.py`: classification guards, outbox creation, immutable subject
  binding, idempotent import, linear correction and receipt application.
- `app/result_transfer/receipt.py`: signed grade-free Secret acknowledgement or safe rejection.
- Alembic revision `4d8f7a1c2b90` and focused adversarial tests.

## Intended controlled-media run

1. Secret generates and protects an X25519 recipient private key and an Ed25519 receipt
   signing key. Only their public keys leave Secret.
2. Unclassified generates and protects its Ed25519 result-signing private key. Secret enrolls
   that public key as an explicitly trusted source.
3. Secret creates a random subject token, stores its learner mapping with `bind_subject`, and
   provisions only the token to Unclassified through the approved identity process.
4. A scoring producer creates a `ResultClaim`. `queue_result` encrypts it before persistence;
   the outbox row contains ciphertext and the allowed pass/fail projection.
5. `export_package` writes one `<package UUID>.tnresult.json` file to controlled media. The
   operator follows local media handling and chain-of-custody procedures.
6. Secret copies the file into quarantine. After external malware/media checks, `load_package`
   verifies its size, type, filename, destination and source signature. `consume_package`
   decrypts it and invokes an idempotent callback.
7. The callback calls `import_result` and commits the Secret transaction using the package ID
   as its idempotency key. It must commit before returning. The filesystem receipt is written
   after the callback; a crash in between safely repeats the database import.
8. Secret creates a signed `ResultReceipt`. Unclassified applies it with `apply_receipt`, which
   accepts it only when signer, reverse route, package ID and digest match the outbox row.

Never print a decrypted claim, key, learner mapping or numeric result in an operator command,
log, metric, trace or support bundle.

## Configuration boundary

`TN_CLASSIFICATION_LEVEL` is `unclassified`, `secret` or `top_secret`.
`TN_ENCLAVE_ID` is the stable lowercase route identity for one independent deployment. It is
1–63 letters, digits or hyphens and must not be reused for another enclave. Both settings are
required in production by API startup, Compose and installer preflight. Development and test
default to `unclassified` and `development` so local tooling keeps working.

The configured enclave ID is part of the security boundary: an Unclassified outbox refuses a
source enclave that differs from `TN_ENCLAVE_ID`; Secret import refuses a package addressed to
another enclave; and receipt application refuses a destination that differs from it. Secret
subject binding and grade import refuse to run on Unclassified, while outbox creation and
receipt application refuse to run on Secret.

Key generation helpers exist for tests and controlled provisioning. Production keys must be
created inside the approved custody boundary and supplied to a caller without logging them.
Key IDs are the first 16 hexadecimal characters of the SHA-256 public-key digest.

## Subject provisioning foundation

Secret can now issue a `truenorth.subject-provisioning.v1` credential while retaining the
token-to-person binding locally. The signed credential contains only its random credential
ID, route, validity window, tenant ID and random subject token. Its strict allow-list rejects
added identity fields, tampering, an untrusted signer, a wrong destination, expiry and a
future issue time. Lifetimes are capped at 90 days.

This establishes the cross-enclave contract only. Unclassified account creation, one-time
credential delivery, Keycloak provisioning, revocation media and assignment to a verified
pseudonymous local account remain required before a learner can use it.

## Verification

Focused checks cover public-field leakage, tampering, wrong signer, wrong recipient, wrong
destination, malformed score/evidence, timezone handling, media limits, symlinks, overwrite,
filename mismatch, replay, subject rebinding, idempotent import, linear correction, receipt
tampering and migration structure. A fresh SQLite migration, downgrade to the prior head and
re-upgrade also pass. The full repository DoD passed on 2026-10-23 with 8,751 tests passing and
164 skipped. A PostgreSQL migration run remains required because this host has no configured
`TEST_POSTGRES_ADMIN_URL` and the PostgreSQL-only cases were among the skips.

## Required next work

1. Add an approved subject-token issuance/provisioning workflow; never reuse TN user IDs as
   opaque tokens.
2. Connect quiz, practical exercise and class close-out producers in one transaction, then
   replace their readable Unclassified numeric persistence with pass/fail-only records.
3. Add hardware-backed key references, key rotation and trusted-source enrollment. Installer-
   managed enclave identity and classification are implemented; set unique values for every
   deployment.
4. Add quarantine state, package age/count policy, malware scanner integration, audit events,
   correction approval and operator UI/API with tenant and role tests.
5. Run DLP tests across the Unclassified database, API, logs, traces, exports and backups.
6. Demonstrate 100 synthetic results with duplicates, reordering, corrections, wrong keys,
   lost media and destination outage between two isolated installations.
