# ADR 0009 — `vsphere_api` is the canonical vSphere provisioning path

- Status: accepted
- Date: 2026-10-05 (written as "0004" on `claude/esxi-vsphere-network-scan-1cbabc`,
  f657a35; renumbered and brought up to date 2026-10-07)
- MOSA pillar: modular design, designated key interfaces

Numbering: 0004 is the Moodle course publication ADR on the stage-4 base, and 0005
(detection credit) and 0006 (capacity service) are claimed on open branches, as are two
other drafts numbered 0004 that will need numbers of their own. 0009 avoids all of them.

## Context
A read-only scan of a live 4-host vSphere lab (`DC-Lab`/`CL-Lab`, no shared datastore, a
distributed switch with a dedicated range VLAN) found two registry-selected vSphere
provisioners coexisting with no documented relationship:

| Backend key | Implementation | Live-wired? |
|---|---|---|
| `vsphere_api` | `control-plane/worker/worker/provisioners/vsphere_api.py`: pyVmomi for placement, port groups, clone/reconfigure, snapshots; the Automation REST API for power, Tools and Content Library deploy | Yes: `PROVISIONER_BACKEND`, used by `tasks.py` |
| `terraform_vsphere` | `TerraformProvisioner(hypervisor_type="vsphere")` | No: expects its root module at `TERRAFORM_VSPHERE_DIR` (default `/opt/truenorth/terraform/vsphere`); nothing in this repo or its install tooling points it at `infra/vsphere/terraform/` |

`infra/vsphere/terraform/main.tf`'s header claimed it matched the provisioner_output
schema of both backends, a contract no configuration established, and both backends
ignored a configured resource pool.

A third surface, `truenorth-content-pack/truenorth-content/scenarios/*/range.tf`, is
intentionally separate: its own instructions forbid `terraform apply` from that pipeline
(provisioning is a separate, human-gated step). This ADR does not change its status.

## Decision
1. `vsphere_api` is the canonical runtime provisioner for vSphere. Provisioning features
   (per-range network isolation, VLAN and uplink reservations, resource-pool targeting,
   lab-session leased port groups, older-vCenter support) are built there, behind the
   `BaseProvisioner` interface (ADR 0001).
2. `infra/vsphere/terraform` is kept for one-off reference environments only. Template
   building is Packer's (`infra/vsphere/packer`). `TERRAFORM_VSPHERE_DIR` is not pointed at
   it, and `terraform_vsphere` is not a supported `PROVISIONER_BACKEND` value, until a
   future ADR gives it a real target and a reason to exist alongside `vsphere_api`. It has
   no per-range port groups and no reservations, so it must not build ranges.
3. `main.tf`'s header says so. Its resource-pool ternary (identical branches) is fixed.

## Consequences
- One vSphere runtime path to test (unit fakes in `tests/worker/test_vsphere_provision.py`,
  govmomi's simulator in `tests/integration/test_vsphere_sim.py`, the live lab in
  `tests/integration/test_lab_sessions_vsphere.py`), document, and grant credentials for.
- Done in `vsphere_api` since the scan: per-range port groups on reserved VLANs,
  `VSPHERE_RESOURCE_POOL`, inventory-template clone where there is no Content Library,
  and a legacy-session fallback for vCenters without `/api/session`.
- Still open: `tasks.py` reads per-connection credentials (`_hypervisor_creds`) but the
  provisioner still takes its endpoint and login from the environment; a build and its
  destroy must use the same vCenter, so this is changed for every operation at once or
  not at all.
- ~~`terraform_vsphere` stays registered (ADR 0001 does not require removing an adapter to
  stop using it).~~ Superseded 2026-10-09, below.

## Amendment 2026-10-09 — Terraform backends removed; Proxmox and Hyper-V experimental
- `terraform`, `terraform_proxmox`, `terraform_vsphere` and `terraform_hyperv` are removed
  from the registry, and `provisioners/terraform.py` with them. Nothing else used it, the
  worker image has no `terraform` binary (so every one of them failed at its first
  `terraform init`), and decision 2 already ruled `terraform_vsphere` out as a range
  builder. The worker image no longer creates `/opt/truenorth/terraform/workspaces`, and
  the Helm chart no longer mounts it. `infra/vsphere/terraform` (reference environments)
  and `infra/proxmox/terraform` (its own CI plan) are unchanged.
- `proxmox_api` and `hyperv` stay registered but are **experimental**: off unless
  `EXPERIMENTAL_PROVISIONERS` is true. Neither has had a live run, and the sites served
  are vSphere-only. With the flag off:
  - the worker's `get_provisioner()` raises `ExperimentalProvisionerError`, a final error
    (no retry; the range is recorded `failed` with the reason);
  - the API answers 409, naming the fix, to `POST /ranges` and refuses a lab launch,
    before any range row exists (`app/provisioner_choice.py`; a contract test keeps its
    list equal to the worker registry);
  - the installer preflight refuses `tn_provisioner_backend: proxmox_api|hyperv` unless
    `tn_experimental_provisioners: true` (rendered to `EXPERIMENTAL_PROVISIONERS`).
- Supported backends are `vsphere_api` and `mock` (a site without vCenter, tests).
