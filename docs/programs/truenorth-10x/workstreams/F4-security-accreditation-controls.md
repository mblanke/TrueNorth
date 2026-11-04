# Foundation F4 — security and accreditation controls

Status: proposed after F0; implementation is gated by security-authority decisions. Suggested
branch: `claude/foundation-f4-security-controls`.

## Closure outcome

An approved threat/data-flow model assigns encryption, key custody, identity, audit, retention
and enclave controls to TrueNorth or its platform. The installer verifies those controls and
acceptance evidence proves data and backups are protected as designed.

## Decision/architecture slice

1. Inventory data classes and flows across browser/edge, API, PostgreSQL, Redis, MinIO,
   OpenSearch, Keycloak, workers, backups, Moodle, range hosts and cross-enclave exchange.
2. Obtain decisions for Unclassified pseudonymous pass/fail, sealed numeric result handling,
   Secret identity/grade authority, retention/appeal and data-diode/guard interface.
3. Allocate at-rest and in-transit protection to application, host, storage, network or
   external KMS/HSM/Vault controls. Name algorithms/modules and required validation level; do
   not advertise FIPS merely because an algorithm exists.
4. Define key generation, custody, rotation, escrow, recovery, compromise and destruction,
   including restore and disconnected-enclave operation.
5. Threat-model console relay, exercise effects, content port, result transfer, installer and
   backup recovery; record mitigations and residual risks.

## Implementation slices

- Add installer preflight/verification for approved disk/storage/database/object encryption
  and internal TLS boundaries.
- Add external key-provider interfaces only where the approved design requires them; retain a
  safe development provider without presenting it as production accreditation.
- Make backup encryption, manifest/signature and restore key prerequisites testable.
- Add adversarial tests for tenant/enclave identity, replay, stale intent, key rotation,
  corrupted ciphertext and unavailable key service.

## Acceptance

- Every sensitive store/flow has owner, control, verification and residual-risk decision.
- Fresh install, upgrade, rotation and restore succeed with the approved key boundary.
- Missing/wrong/revoked keys fail closed without destructive partial writes.
- Logs, metrics, exports and support bundles contain no prohibited values.
- Security authority signs the tested scope; Secret remains separate until tested/accredited.

## Recovery

All encryption migrations require tested backup, dual-read/rotation window where appropriate,
reversible cutover and explicit failure behavior. Never rotate or migrate live keys merely to
complete this packet.

## Skills

Primary: `tn-tenant-auth`; add `tn-release-verification`, `tn-schema-migrations`,
`tn-job-lifecycle` and `tn-change-review`. F4 gates readiness, AI, physical, exchange and
federation release claims.
