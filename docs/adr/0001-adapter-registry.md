# ADR 0001 — External systems go behind an adapter ABC and a registry

- Status: accepted
- Date: 2026-10-03
- MOSA pillar: modular design, designated key interfaces

## Context
TrueNorth integrates systems a customer will want to swap: hypervisors (vSphere, Proxmox,
Hyper-V, Terraform), identity providers, LMS/LRS, search/telemetry stores, notification
channels, LLM backends. Where those sit behind an interface they are swappable today:

| Seam | Interface | Registry |
|---|---|---|
| Provisioning | `control-plane/worker/worker/provisioners/base.py` `BaseProvisioner` | `provisioners/__init__.py` `get_provisioner()` |
| Auth | `control-plane/api/app/auth_backends/base.py` | `get_auth_backend()` (`AUTH_BACKEND`) |
| LMS / xAPI | `control-plane/api/app/lms/base.py` | `get_lms_backend()` (`LMS_BACKEND`) |
| Search | `control-plane/api/app/search_backends/base.py` | `get_search_backend()` (`SEARCH_BACKEND`) |
| Threat intel feeds | `control-plane/api/app/threat_intel_backends/base.py` `BaseFeedBackend` | `get_feed_backend(feed.feed_type)` |
| Notifications | `control-plane/api/app/notifications/base.py` | `notifications/registry.py` |
| AI | `ai-orchestrator/app/backends/base.py` | backend factory |

Where code calls the vendor directly (`routers/proxmox.py` using `proxmoxer`,
`if hypervisor_type == "vsphere"` in `routers/hypervisors.py`, Moodle branches in
`routers/integrations.py`), replacing that vendor means editing business logic.

## Decision
1. Any new external system is reached only through an ABC in a `*_backends/` (or
   `provisioners/`) package, selected by a registry keyed on config or a DB column.
2. Vendor SDKs (`proxmoxer`, `pyVmomi`, `opensearchpy`, …) are imported only inside
   adapter packages. Routers, tasks and services never import them.
3. No `if <kind> == "<vendor>"` dispatch in routers; ask the registry.
4. Each ABC has one contract test suite that every registered implementation runs.

## Consequences
- `scripts/mosa_check.py` counts violations of 2 and 3 and fails if the count rises
  (ADR 0003). Existing violations are recorded debt to be removed, not precedent.
- Adding a vendor = one new adapter file + one registry line + passing the contract suite.
