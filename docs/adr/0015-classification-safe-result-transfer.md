# ADR 0015: classification-safe low-to-high result transfer

**Status:** Accepted foundation; operational integration remains open
**Date:** 2026-10-23

## Context

TrueNorth will run as independent installations in Unclassified, Secret and potentially
higher enclaves. The Unclassified instance needs pass/fail to operate training, while the
Secret instance is the authority for learner identity and numeric grades. A shared database,
shared identity service or general network connection across those boundaries would defeat
the enclave model.

The existing application stores names and numeric scores in normal user, quiz, module and
enrollment records. The transfer design cannot by itself make that legacy behaviour safe for
the Unclassified deployment; producers and identity flows must be converted in later slices.

## Decision

1. Each enclave runs its own database, keys, identities and services. Results cross the
   boundary only as an explicit package.
2. Secret issues and retains the mapping from a random opaque subject token to a real user.
   Unclassified may know the token but not the mapping.
3. The package's readable projection is an exact allow-list: opaque subject token, course
   release, assessment, attempt and pass/fail. Numeric score, tenant, scorer and evidence
   digests are inside authenticated encryption.
4. An Unclassified Ed25519 key signs the package. An ephemeral X25519 exchange, HKDF-SHA256
   and AES-256-GCM encrypt it to the Secret recipient key. Package ID, route and public
   projection are authenticated and destination-bound.
5. Controlled media is the first transport. Files are canonical, bounded to 1 MiB, created
   without overwrite, and read only through a no-follow regular-file descriptor.
6. Secret imports are idempotent by package ID and digest. A changed result must name the
   package it supersedes and preserve the attempt identity; correction chains cannot fork.
7. Secret returns an Ed25519-signed receipt containing only package ID, digest, route,
   accepted/rejected and a fixed non-sensitive error category.
8. The common schema contains low-side and high-side tables so the same signed release can be
   installed in every enclave. Application service guards allow subject mappings and readable
   imported grades only when `TN_CLASSIFICATION_LEVEL` is `secret` or `top_secret`. The
   Unclassified authorization package must separately prove those tables remain empty.
9. Keys are passed into the portable contract. Environment variables are not established as
   the production custody design; installer/KMS integration is a later decision.

## Consequences

The core contract can be tested between isolated local deployments before a cross-domain
gateway is selected. Repeated media is harmless after a completed import, and a crash after a
database commit can retry because the Secret ledger is also idempotent. The importer callback
must commit by package ID before the filesystem consumption receipt is written.

This ADR does not authorize a classified deployment and does not complete the data rule. The
following remain release blockers: pseudonymous Unclassified onboarding; conversion of quiz,
exercise and class-close-out producers so readable numeric scores are not retained low-side;
hardware-backed key custody and rotation; source-key enrollment; package age/count policy;
quarantine and malware inspection; correction approval; audit/UI/runbooks; DLP scans; and an
approved transfer mechanism.
