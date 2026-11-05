# Workstream 05 — physical training asset provider

Status: blocked until workstream 02's event/effect envelope is accepted. Suggested branch:
`claude/10x-05-physical-assets`.

## Outcome

Treat TinyTown, vehicle-hacking and future ICS/IoT equipment as reserved, health-checked,
audited training assets whose manual and automated steps are represented truthfully.

## Provider contract

Inventory, capabilities, handling/safety constraints, reserve, preflight, grant access, start,
observe evidence, stop, reset, release, quarantine and custody events. Every operation declares
whether it is automated, operator-confirmed or unsupported; includes idempotency and timeout;
and maps to the common exercise timeline.

## First vertical slice

Implement a manual provider and synthetic assets. An instructor reserves an asset for an
exercise; an authorized operator confirms preflight/start/reset; students see only readiness
and access instructions; ENDEX requires reset/release or an explicit quarantine. Conflicts,
expired holds and missing reset remain visible. No vendor network call is required.

## Acceptance

- Tenant, role and exercise ownership checks cover every operation.
- Concurrent reservation cannot double-book an asset.
- A crash/retry cannot skip reset, release an active asset or reverse newer operator intent.
- Custody/safety acknowledgement is audited without leaking restricted details to students.
- Manual steps never appear as automatic success.
- Timeline and teardown accounting include the asset through completion/quarantine.

## Subsequent slices

After D06 and vendor testing: TinyTown adapter, vehicle adapter, evidence gateway, automated
health/reset where supported, maintenance windows and spare-pool/capacity planning. Any
offensive or physical effect additionally requires D05 safety approval.

## Skills

Primary: `tn-job-lifecycle`; add `tn-scenario-evidence`, `tn-tenant-auth`,
`tn-api-contracts`, `tn-schema-migrations` and `tn-change-review`.
