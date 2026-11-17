# Workstream 07 — federation and scale cells

Status: blocked until the core evidence, timeline, environment, physical and exchange contracts
are stable. Suggested branch: `claude/10x-07-federation`.

## Outcome

Scale toward the 70,000-vCPU ambition through measured, isolated execution cells with central
policy and admission—not an unproven single scheduler/control plane.

## Cell contract

Cell identity/trust, capabilities, tenant/enclave eligibility, allocatable/reserved capacity,
image/content versions, health freshness, admission quote, lease, provisioning operation,
evidence/result return, drain/quarantine and recovery. Capacity includes CPU overcommit,
memory, storage IOPS/capacity, network, images, licenses and physical dependencies; vCPU alone
is insufficient.

## First vertical slice

Register two synthetic cells and schedule one existing supported range workload. Admission
must select an eligible cell or reject with an explainable capacity/policy reason. A signed
lease fences retries and stale completions. Cell loss becomes visible and recoverable without
marking the range ready or leaking another tenant's capacity/details.

## Acceptance

- Deterministic admission under identical inputs and explicit tie-breaks.
- Stale health/capacity cannot accept new work.
- Duplicate dispatch and late completion cannot create resources or overwrite newer intent.
- Cell/tenant/enclave restrictions are enforced at every nested operation.
- Drain/quarantine and control-plane restart preserve leases and accounting.
- Load/recovery tests state the exact hardware, topology, workload and percentile; no result is
  extrapolated into a 70,000-vCPU claim.

## Scale proof sequence

1. Define workload classes and D07 service objectives.
2. Establish single-cell saturation and failure curves.
3. Prove multi-cell admission, isolation and recovery with synthetic load.
4. Run named site/hardware tiers with storage/network telemetry.
5. Increase only after bottlenecks and teardown/resource accounting are closed.
6. Treat 70,000 vCPU as accepted only at the tested topology or through a defensible measured
   model with separately validated cell limits.

## Skills

Primary: `tn-job-lifecycle`; add `tn-release-verification`, `tn-incident-diagnosis`,
`tn-tenant-auth`, `tn-api-contracts` and `tn-change-review`.
