# Workstream 03 — sanitized digital-twin compiler

Status: proposed; may begin after 00. Suggested branch: `claude/10x-03-digital-twin`.

## Outcome

Convert approved architecture inventory into a reviewable draft range-template change while
removing identities, secrets, operational addresses and unnecessary production detail.

## First vertical slice

Accept one deliberately limited input: approved Terraform plan JSON generated from synthetic
fixtures. Quarantine the upload; reject state files and secret-like values; normalize resources
into a vendor-neutral intermediate representation; apply explicit sanitization/substitution
rules; generate a draft template diff; require human review before promotion. Record input
hash, compiler/rule version, every redaction/substitution, warnings and reviewer decision.

No AI-generated infrastructure is executable in this slice. AI may later suggest mappings,
but the deterministic compiler and policy validate them.

## Acceptance

- Known credentials, tokens, private keys, email/host identities and production addresses are
  rejected or replaced, with synthetic leak tests.
- Parser bombs, oversized inputs, path traversal and cross-tenant artifact references fail
  safely.
- Same input/rule/compiler version produces the same IR and diff.
- Unsupported resources are explicit warnings/errors, never silently dropped.
- Draft cannot provision or become approved content without the existing review/promotion path.

## Subsequent slices

Add reviewed adapters for vCenter, NetBox and architecture inventories; promoted equivalents;
behavioral validation; signed compiler artifacts; drift comparison and approved AI mapping
suggestions. Each source adapter gets its own threat model and fixture corpus.

## Recovery

All outputs begin as draft artifacts. Delete/reject a draft through existing content lifecycle;
never modify the source environment. Compiler/rule versions remain available for reproduction.

## Skills

Primary: `tn-api-contracts`; add `tn-tenant-auth`, `tn-job-lifecycle`,
`tn-schema-migrations` and `tn-change-review`. Integrator owns the IR/version contract.
