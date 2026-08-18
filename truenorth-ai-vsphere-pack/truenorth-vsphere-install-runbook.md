# TrueNorth Initial vSphere Installation Runbook

## Purpose

Step-by-step runbook for deploying the first TrueNorth development environment on the four-host Supermicro VMware environment.

Complete `truenorth-vsphere-discovery.md` first.

Do not use placeholder networks, IPs, domains, datastores, or sizing without replacing them with discovered values.

---

## Phase 1 — Preflight

### 1. Confirm discovery outputs

Required:

```text
deployment-readiness.md
vsphere-inventory.md
capacity-plan.md
network-plan.md
storage-plan.md
identity-plan.md
template-plan.md
```

Do not deploy while critical values remain unknown.

### 2. Confirm installation media

Required initially:

```text
Ubuntu Server 24.04 LTS ISO
Windows Server 2022 or 2025 ISO
```

Known location to inspect:

```text
ESX-01 / ISO
```

Verify checksums before use.

### 3. Confirm networks

At minimum identify:

```text
TrueNorth management network
vCenter/ESXi management path
future range-network strategy
```

---

## Phase 2 — Deploy TN-MGMT01

### 4. Create VM

Initial target:

```text
Name:          TN-MGMT01
Guest OS:      Ubuntu Server 24.04 LTS
vCPU:          12-16
RAM:           64 GB minimum
Preferred RAM: 96-128 GB if capacity permits
OS Disk:       100 GB
Data Disk:     500 GB minimum
NIC:           approved TrueNorth management port group
Firmware:      UEFI
```

Adjust after capacity review.

### 5. Install Ubuntu

Configure:

```text
minimal server
OpenSSH
static IP or DHCP reservation
correct DNS
correct NTP
non-root admin user
```

No desktop environment required.

### 6. Validate Linux baseline

```bash
hostnamectl
ip addr
ip route
resolvectl status
timedatectl
df -h
free -h
```

Set hostname:

```text
TN-MGMT01
```

### 7. Prepare persistent application storage

Preferred conceptual root:

```text
/srv/truenorth/
```

Possible layout:

```text
/srv/truenorth/
├── postgres/
├── redis/
├── minio/
├── opensearch/
├── reports/
├── artifacts/
├── logs/
└── backups/
```

Use existing TrueNorth conventions if they already define something better.

---

## Phase 3 — Deploy TN-DC01

### 8. Create Windows VM

```text
Name:       TN-DC01
Guest OS:   Windows Server 2022/2025
vCPU:       4
RAM:        8-16 GB
Disk:       100 GB
NIC:        approved TrueNorth management port group
Firmware:   UEFI
```

### 9. Install Windows Server

Configure:

```text
hostname: TN-DC01
static IP
gateway
DNS bootstrap
time/NTP
VMware Tools
patching according to policy
```

---

## Phase 4 — Configure AD / DNS

### 10. Install AD DS and DNS

Create the development management domain.

Placeholder:

```text
truenorth.lab
```

Confirm final domain before promotion.

### 11. Suggested OU layout

```text
TrueNorth
├── Users
│   ├── Administrators
│   ├── Instructors
│   ├── Students
│   └── Observers
├── Groups
├── Service Accounts
└── Computers
```

### 12. Suggested groups

```text
TN-Platform-Admins
TN-Range-Operators
TN-Instructors
TN-Students
TN-Observers
TN-Content-Authors
```

Map them to actual TrueNorth RBAC rather than inventing new application roles.

---

## Phase 5 — Install TrueNorth

### 13. Determine the deployment source

The current TrueNorth working tree may contain important uncommitted development.

Do not automatically deploy the latest commit if it is stale.

Choose explicitly:

```text
current working-tree snapshot
approved branch
temporary deployment branch
tagged release
```

### 14. Install platform dependencies

Expected categories:

```text
Docker
Docker Compose
Python tooling
Terraform
required CLI tools
```

Use repository-supported versions. Avoid blindly installing `latest`.

### 15. Configure persistent volumes

Ensure high-growth components live on the data disk:

```text
OpenSearch
MinIO
PostgreSQL
reports
exercise artifacts
logs
```

### 16. Configure secrets

Never commit:

```text
database passwords
Keycloak secrets
MinIO credentials
vCenter credentials
API keys
TLS private keys
```

### 17. Start the TrueNorth services

Expected:

```text
FastAPI
Angular frontend
PostgreSQL
Redis
Celery
Keycloak
MinIO
OpenSearch
OpenSearch Dashboards
AI Orchestrator
```

Use actual repository Compose/service definitions.

---

## Phase 6 — Validate TrueNorth

### 18. Health checks

Verify:

```text
API
frontend
PostgreSQL
Redis
Celery workers
Keycloak
MinIO
OpenSearch
AI Orchestrator
WebSockets
```

If the current project CLI still supports it, validate the health command, for example:

```bash
python tools/cli/forge.py health
```

Confirm actual syntax first.

---

## Phase 7 — Federate Keycloak to AD

### 19. Preserve Keycloak

Architecture:

```text
AD
 │
 ▼
Keycloak
 │
 ▼
OIDC/JWT
 │
 ▼
TrueNorth
```

### 20. Configure LDAP federation

Test:

```text
AD login
group membership
role mapping
disabled account
password changes
account lockout
```

### 21. Map groups to TrueNorth roles

Conceptual example:

```text
TN-Platform-Admins  -> platform administrator
TN-Range-Operators  -> range operator
TN-Instructors      -> instructor
TN-Students         -> student
TN-Observers        -> observer
TN-Content-Authors  -> content author
```

Use actual application permission names.

---

## Phase 8 — vSphere Integration

### 22. Create least-privilege vCenter service account

Do not use the vCenter Administrator identity for TrueNorth.

Begin with read-only inventory permissions.

### 23. Configure Terraform vSphere provider

Keep credentials outside source control.

First validate:

```text
list datacenters
list clusters
list hosts
list datastores
list networks
list templates
```

without creating resources.

### 24. Add provider abstraction

Target:

```text
infra/providers/vsphere/
```

Preserve Proxmox support.

---

## Phase 9 — First Provisioning Test

### 25. Deploy TN-RANGE01

Use TrueNorth/Terraform to perform:

```text
create
clone/template
network assignment
power on
guest ready
telemetry/agent bootstrap
power off
destroy
```

### 26. Verify cleanup

Confirm:

```text
VM removed
virtual disks removed
snapshots removed
temporary network objects cleaned if applicable
IP released
TrueNorth state correct
Terraform state clean
no orphaned artifacts
```

---

## Phase 10 — Base Templates

### 27. Ubuntu

Create:

```text
TN-Ubuntu-Base
```

Include:

```text
open-vm-tools
cloud-init
SSH baseline
time sync
clean identity
no embedded secrets
```

### 28. Windows Server

Create:

```text
TN-Windows-Server-Base
```

Include:

```text
VMware Tools
patched baseline
Sysprep-compatible
not domain joined
no embedded credentials
```

### 29. Windows 11

Later:

```text
TN-Windows-11-Base
```

---

## Phase 11 — First Real Exercise Range

Deploy a small disposable AD environment:

```text
RANGE-001
├── DC01
├── FILE01
├── WS01
├── WS02
├── ATTACK01
└── SENSOR01
```

Validate:

```text
network isolation
disposable AD
scenario execution
telemetry
objective scoring
cleanup
```

---

## Phase 12 — Taz Deployment Integration

### 30. Read-only Taz vSphere tools

```text
taz.vsphere_inventory
taz.vsphere_hosts
taz.vsphere_datastores
taz.vsphere_networks
taz.vsphere_templates
taz.vsphere_capacity
taz.vsphere_health
```

### 31. Workflow

```text
Operator
   │
   ▼
Claude
   │
   ▼
Taz reads current vSphere state
   │
   ▼
Claude prepares change
   │
   ▼
Codex independently reviews code/Terraform
   │
   ▼
Human approval
   │
   ▼
Bounded execution
```

---

## Phase 13 — Backup / Recovery

Document backup and recovery for:

```text
PostgreSQL
MinIO
OpenSearch configuration
Keycloak configuration
TrueNorth configuration
Terraform state
VM templates
TN-DC01 system state
```

VM snapshots are not the complete backup strategy.

---

## Phase 14 — Acceptance Test

- [ ] TN-MGMT01 healthy.
- [ ] TN-DC01 healthy.
- [ ] AD/DNS functioning.
- [ ] Keycloak functioning.
- [ ] AD authentication through Keycloak works.
- [ ] TrueNorth API healthy.
- [ ] TrueNorth frontend healthy.
- [ ] PostgreSQL healthy.
- [ ] Redis healthy.
- [ ] Celery healthy.
- [ ] MinIO healthy.
- [ ] OpenSearch healthy.
- [ ] AI Orchestrator healthy.
- [ ] TrueNorth can read vSphere inventory.
- [ ] TrueNorth can deploy TN-RANGE01.
- [ ] TrueNorth can destroy TN-RANGE01 cleanly.
- [ ] Management plane isolated.
- [ ] Range isolation validated.
- [ ] Base templates documented.
- [ ] Backup/recovery documented.

---

## Phase 15 — Stop Conditions

Stop and investigate if:

```text
exercise networks can reach ESXi/vCenter management
students can reach TN-DC01
vCenter admin credentials are stored in source control
Terraform plans to destroy unrelated VMware objects
ISO storage is not where expected
management routing is unclear
DNS is inconsistent
deployment would overwrite the current dirty TrueNorth working tree
```

Do not improvise around these.

---

## Final Development Topology

```text
                         vCenter
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
           Management                 Ranges
                │                       │
        ┌───────┴────────┐       ┌─────┴─────┐
        │                │       │           │
   TN-MGMT01         TN-DC01   RANGE-001   RANGE-N
      Ubuntu          Windows    disposable disposable
        │                │
        │               AD
        │               DNS
        │
    TrueNorth
        │
   Terraform/vSphere
        │
        └──────── controlled provisioning ─────►
```
