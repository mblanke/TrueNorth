# ADR 0006 — CapacityService: real supply and every running range

- Status: proposed
- Date: 2026-10-07
- MOSA pillar: modular design, designated key interfaces
- Related: ADR 0004 (scheduler), which first called this ADR 0005. That number has
  since been taken by `0005-detection-credit-is-student-evidence`, so this is 0006.

## Context

ADR 0004 found three faults in the scheduler's capacity check. The scheduler fixed
one itself: demand is now derived from templates. Two remained:

1. **Supply was a guess.** Cluster totals came from `CLUSTER_TOTAL_*` env vars,
   minus a 15% overhead, whatever hardware is actually there.
2. **Commitments were incomplete.** Only bookings counted. A range started without a
   booking, or kept up between two sessions, used capacity the scheduler could not
   see. That is how the cluster gets overtaxed.

## Decision

1. **A capacity section,** `control-plane/api/app/capacity/`, owns infrastructure
   facts. It reads hypervisor hosts, ranges and templates, and nothing of bookings.
   - `supply(db)` returns usable totals, how many hosts reported them, and their
     `source`.
   - `running_ranges(db)` returns every range holding resources now, sized from its
     template (`range_topology.template_demand`, the worker's own sizing).
2. **Supply** comes from the online hosts of active hypervisor connections:
   - **Overcommit:** vCPU is physical cores × `CAPACITY_VCPU_RATIO` (default 4). RAM
     and disk are 1:1 unless `CAPACITY_RAM_RATIO` / `CAPACITY_DISK_RATIO` say
     otherwise.
   - **Headroom:** `CLUSTER_OVERHEAD_PCT` (default 15) is held back.
   - **Fallback per resource:** a resource counts as discovered only if **every**
     online host reports it. A partial sum would understate the cluster while
     looking like real data. Otherwise that resource falls back to its env value,
     and `source` says which ones did, for example `discovered (RAM, disk: env)`.
3. **Running load:**
   - A range that is provisioning, ready or running holds its template's size until
     it is destroyed. A stopped range holds its disk only.
   - Ranges have no expiry column, so "until destroyed" is the only honest horizon.
     Running ranges count from now on, never in the past.
4. **The scheduler combines them.** `scheduler.capacity.ClusterCapacity` implements
   the `CapacityProvider` seam (ADR 0004 slice 2).
   - Committed = bookings holding the window, plus every running range that **no
     booking of it** covers in that window.
   - A booked range is therefore counted once, through its booking. A range started
     without a booking, or kept up between two sessions, is counted too.
   - Re-checking a booking does not count its own range against it.
5. **One path for every reader.** `/schedule/capacity`, `/check`, `/timeline`, the
   booking check and the dashboard gauge all use this provider, so they cannot
   disagree. Every response carries `supply_source`.

## What this does not do

**Fill in vSphere host figures.** vCenter's REST API reports no host CPU or memory;
those need the SOAP API (pyVmomi), which is allowed only in adapters. Discovery that
reads host hardware is in flight elsewhere: PR #3 (vSphere lab integration) and S5a
(`vsphere_infra.host_capacity`). When either lands and fills
`hypervisor_nodes.cpu_total` / `memory_total_gb` / `storage_total_gb`, supply becomes
`discovered` with no change here. Until then vSphere supply is the env fallback, and
it says so.

**Count shared datastores once.** Disk sums `storage_total_gb` across hosts. vSphere
datastores are usually shared, which would multiply them. Discovery should report each
datastore once, against one host, or leave disk unreported (env fallback) until it
does.

## Consequences

- The overtax gap is closed. A booking is refused when unbooked running ranges have
  already used the room it needs.
- The Range Ops view and the dashboard show the same totals as the booking check.
- No schema change; no new dependency.

## Checks

`tests/scheduler/test_capacity_service.py`:
- env fallback; discovered supply under the policy; per-resource fallback;
- offline hosts and inactive connections excluded;
- running and stopped ranges sized;
- an unbooked range counts; a booked range is counted once; a range kept up between
  sessions counts between them; running ranges do not count in the past;
- through the API, a booking is refused because unbooked ranges fill the cluster.
