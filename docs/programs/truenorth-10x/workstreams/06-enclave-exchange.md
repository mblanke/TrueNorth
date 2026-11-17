# Workstream 06 — enclave-native content and result exchange

Status: proposed; transport-independent work may begin after 00. Suggested branch:
`claude/10x-06-enclave-exchange`.

## Outcome

Complete the existing content-port and result-transfer foundations as one signed,
store-and-forward product flow. Unclassified retains only approved data; Secret receives the
authoritative record through an approved one-way process. A data diode is expected but TBC.

## Contract PR

Version package manifest, source/destination enclave IDs, pseudonymous subject correlation,
course/exercise/release identity, pass/fail/incomplete result, evidence/provenance references,
creation/expiry, signer/encryption metadata, idempotency key, receipt/rejection and replay
rules. Define a transport-neutral outbox/inbox so diode, removable-media or guarded adapters
do not change the domain contract.

D02 and D03 block production identity/transport choices. Because current Unclassified
`ClassResult` intentionally has no numeric grade, “send the grade up” needs an approved design:
either Secret computes/records it from permitted detailed evidence, or an accredited sealed
capture path creates a package without retaining the number on Unclassified. Do not quietly
add a numeric grade column.

## First vertical slice

Using synthetic identities and pass/fail only, export a signed/encrypted package to a local
outbox, import it through a disconnected inbox, verify schema/signature/destination, apply it
idempotently to a synthetic authoritative adapter and produce a signed receipt. Duplicate,
out-of-order, expired, tampered and wrong-destination packages are quarantined or no-ops with
auditable reasons.

## Acceptance

- No name, email, narrative comment, secret, token or numeric score occurs in Unclassified
  result fixtures/packages.
- Tenant/enclave/package ownership is checked before existence details are returned.
- Duplicate delivery and repeated receipt change nothing.
- Key rotation, clock skew, partial media copy, schema upgrade and unavailable destination have
  documented behavior.
- DLP fixtures and package tests fail closed.
- Content flowing down and results flowing up use separate policies/keys/queues.

## Subsequent slices

Approved identity resolution, Secret record adapter, real diode/guard transport, operator
console, dead-letter recovery, content promotion/receipt and reconciliation without a reverse
network dependency.

## Skills

Primary: `tn-telemetry-lms`; add `tn-tenant-auth`, `tn-api-contracts`,
`tn-release-verification`, `tn-schema-migrations` and `tn-change-review`.
