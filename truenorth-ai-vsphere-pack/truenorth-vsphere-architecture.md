# TrueNorth vSphere Deployment Architecture

## Purpose

Define the initial deployment architecture for **TrueNorth Range** on the existing VMware environment.

Current known environment:

```text
4 × Supermicro servers
├── VMware ESXi installed
├── vSphere / vCenter available
└── ESX-01 contains an ISO folder/datastore
```

Nothing has been deployed for TrueNorth yet.

Use `truenorth-vsphere-discovery.md` before provisioning so the final design reflects the real host, storage, and network configuration.

---

## 1. Core Design Decision

Do **not** run the entire TrueNorth platform directly on Windows Server.

Use:

- **Ubuntu Server** for the TrueNorth platform stack.
- **Windows Server** for Active Directory and DNS.
- **Keycloak** as TrueNorth's authentication broker.
- AD federated into Keycloak rather than replacing OIDC/JWT.

Initial design:

```text
                     VMware vSphere / vCenter
                              │
              ┌───────────────┴────────────────┐
              │                                │
              ▼                                ▼
         TN-MGMT01                         TN-DC01
       Ubuntu Server                    Windows Server
              │                                │
              │                                ├── AD DS
              │                                ├── DNS
              │                                └── Accounts / Groups
              │
              ├── TrueNorth API
              ├── Angular frontend
              ├── PostgreSQL
              ├── Redis
              ├── Celery
              ├── Keycloak
              ├── MinIO
              ├── OpenSearch
              ├── OpenSearch Dashboards
              ├── AI Orchestrator
              ├── Terraform
              └── vSphere integration
```

---

## 2. Initial VM Sizing

### TN-MGMT01

```text
OS:            Ubuntu Server 24.04 LTS
vCPU:          12-16
RAM:           64 GB minimum
Preferred RAM: 96-128 GB if capacity permits
OS Disk:       100 GB
Data Disk:     500 GB minimum
NIC:           TrueNorth management network
```

OpenSearch will likely be the heaviest service. Final sizing must come from discovery.

### TN-DC01

```text
OS:       Windows Server 2022 or 2025
vCPU:     4
RAM:      8-16 GB
Disk:     100 GB
NIC:      TrueNorth management network
Roles:
  - Active Directory Domain Services
  - DNS
```

### TN-RANGE01

Optional first disposable provisioning target:

```text
Name:     TN-RANGE01
Purpose:  prove TrueNorth can create, configure, start, observe, stop, and destroy a VM
```

---

## 3. Authentication Architecture

Keep Keycloak.

```text
User
 │
 ▼
Keycloak
 │
 ├── local / break-glass TrueNorth accounts
 │
 └── LDAP / AD federation
             │
             ▼
          TN-DC01
```

TrueNorth continues to receive OIDC/JWT identities from Keycloak.

Do not rewrite authentication around Windows Integrated Authentication.

---

## 4. Management Plane vs Range Plane

This separation is mandatory.

### Management Plane

```text
vCenter
ESXi management
TN-MGMT01
TN-DC01
Keycloak
PostgreSQL
Redis
MinIO
OpenSearch
Terraform
Taz integration
administration workstations
```

### Range Plane

```text
exercise domain controllers
student workstations
victim servers
attack simulation systems
sensors
Velociraptor clients
Zeek / Suricata
exercise services
```

Conceptual boundary:

```text
              MANAGEMENT PLANE
                     │
       ┌─────────────┼─────────────┐
       │             │             │
    vCenter      TrueNorth      TN-DC01
       │             │
       └──────┬──────┘
              │
        CONTROLLED BOUNDARY
              │
              ▼
             RANGE
              │
       ┌──────┼────────┐
       │      │        │
      DC    Clients  Servers
```

Students and exercise attack paths must never have unrestricted access to the management domain controller, vCenter, or ESXi management.

---

## 5. Infrastructure AD vs Exercise AD

These are different systems.

### Persistent infrastructure AD

```text
TN-DC01
```

Used for:

- TrueNorth admins
- instructors
- users
- service identities
- management DNS
- platform integration

### Disposable exercise AD

Example:

```text
RANGE-042
├── R042-DC01
├── R042-FILE01
├── R042-SQL01
├── R042-WS001
├── R042-WS002
├── R042-ATTACK01
└── R042-SENSOR01
```

That AD environment is intentionally attackable and destroyed with the range.

---

## 6. VMware Provider Architecture

TrueNorth should evolve from a Proxmox-specific infrastructure layer to a provider abstraction.

```text
infra/
├── providers/
│   ├── proxmox/
│   │   ├── terraform/
│   │   └── packer/
│   │
│   └── vsphere/
│       ├── terraform/
│       ├── packer/
│       ├── templates/
│       └── tests/
│
├── platform/
│   └── docker/
│
└── common/
```

Desired flow:

```text
Scenario
   │
   ▼
Range Definition
   │
   ▼
Infrastructure Provider
   │
   ├── Proxmox
   └── vSphere
```

Scenario logic should not directly care which hypervisor is underneath.

---

## 7. Conceptual vSphere Configuration

```yaml
infrastructure:
  provider: vsphere

  target:
    datacenter: <DISCOVERED>
    cluster: <DISCOVERED>
    resource_pool: TRUENORTH-RANGES

  storage:
    policy: <DISCOVERED>

  network:
    isolation: vlan

  template_source:
    type: content_library
```

Do not hard-code site-specific vCenter names into scenario content.

---

## 8. ISO and Template Strategy

Known starting point:

```text
ESX-01
└── ISO/
```

Initial desired media:

```text
Ubuntu Server 24.04 LTS
Windows Server 2022 or 2025
```

Likely later:

```text
Windows 11
additional Linux images
sensor/appliance images
```

Determine whether the ISO datastore is local to ESX-01 or visible to all four hosts.

After initial infrastructure installation, move toward reusable templates or a vSphere Content Library:

```text
TN-Ubuntu-Base
TN-Windows-Server-Base
TN-Windows-11-Base
TN-Linux-Endpoint-Base
TN-Sensor-Base
```

TrueNorth should eventually clone controlled images instead of repeatedly mounting ISOs.

---

## 9. Initial Networking Requirements

Discovery must identify:

```text
vCenter/ESXi management network
TrueNorth management network
range networks
available VLANs
trunking
port groups
standard/distributed vSwitches
routing
firewall controls
DHCP/IPAM
DNS
NTP
Internet access policy
Taz connectivity
```

Do not place range VMs on the ESXi management network.

Potential future pattern:

```text
Management VLAN
Range-control VLAN
Dynamic exercise VLAN pool
```

Exact IDs must be discovered and approved.

---

## 10. Initial Deployment Milestones

### Milestone 1: Discovery

Produce:

```text
deployment-readiness.md
vsphere-inventory.md
capacity-plan.md
network-plan.md
storage-plan.md
identity-plan.md
template-plan.md
```

### Milestone 2: Management VMs

Deploy:

```text
TN-MGMT01
TN-DC01
```

### Milestone 3: TrueNorth Platform

Install and validate:

```text
API
frontend
PostgreSQL
Redis
Celery
Keycloak
MinIO
OpenSearch
WebSockets
AI Orchestrator
```

### Milestone 4: Identity

Configure:

```text
AD DS
DNS
Keycloak LDAP federation
TrueNorth RBAC mapping
```

### Milestone 5: vSphere Provider

Begin read-only.

Then implement controlled provisioning.

### Milestone 6: First Disposable VM

TrueNorth creates and destroys `TN-RANGE01`.

### Milestone 7: First Complete Range

Deploy a small isolated AD-based exercise.

---

## 11. Taz Role

Taz should become the local **TrueNorth Deployment Engineer**.

Initial read-only capabilities:

```text
taz.vsphere_inventory
taz.vsphere_health
taz.vsphere_hosts
taz.vsphere_networks
taz.vsphere_datastores
taz.vsphere_templates
taz.vsphere_capacity
taz.plan_truenorth_install
taz.validate_range_plan
taz.troubleshoot_provisioning
```

Desired workflow:

```text
Claude
   │
   ▼
Taz inventories vSphere
   │
   ▼
Taz proposes plan
   │
   ▼
Codex reviews code/Terraform
   │
   ▼
Claude reconciles
   │
   ▼
Human approves
   │
   ▼
Bounded execution
```

Do not initially grant Taz unrestricted vCenter write access.

---

## 12. Development Cluster Role

The four Supermicro hosts are the **development/reference deployment**.

Use them to prove:

```text
provisioning
network isolation
identity
templates
range lifecycle
telemetry
scenario execution
scoring
snapshots
cleanup
failure recovery
AAR
```

They do not need to represent the eventual 70,000-VM scale target.

---

## 13. Definition of Done

- [ ] vSphere fully inventoried.
- [ ] ESX-01 ISO location documented.
- [ ] Ubuntu Server ISO available.
- [ ] Windows Server ISO available.
- [ ] Management network confirmed.
- [ ] Range isolation strategy confirmed.
- [ ] TN-MGMT01 deployed.
- [ ] TN-DC01 deployed.
- [ ] AD and DNS functional.
- [ ] TrueNorth stack operational.
- [ ] Keycloak operational.
- [ ] Keycloak federated to AD.
- [ ] TrueNorth login/RBAC works.
- [ ] TrueNorth can read vCenter inventory.
- [ ] Terraform vSphere integration tested.
- [ ] First disposable VM provisioned.
- [ ] First disposable VM destroyed cleanly.
- [ ] Management plane remains isolated from exercise networks.
