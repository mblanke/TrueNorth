# TrueNorth vSphere Discovery Plan

## Purpose

Inventory the existing VMware environment before deploying TrueNorth.

Known state:

```text
4 × Supermicro ESXi hosts
vSphere / vCenter installed
ESX-01 contains an ISO folder
No TrueNorth VMs deployed
```

This phase is **read-only**.

---

## 1. Required Outputs

Create:

```text
docs/deployment/
├── deployment-readiness.md
├── vsphere-inventory.md
├── capacity-plan.md
├── network-plan.md
├── storage-plan.md
├── identity-plan.md
└── template-plan.md
```

---

## 2. vCenter Inventory

Collect:

```text
hostname
IP
version/build
datacenter names
cluster names
resource pools
folders
Content Libraries
license status
NTP/time
DNS
certificate status
```

---

## 3. ESXi Host Inventory

For each host collect:

```text
hostname
management IP
ESXi version/build
Supermicro model
CPU model
socket count
physical cores
logical CPUs
total RAM
free RAM
NIC count
NIC model
NIC link speed
storage controller/HBA
local storage
shared storage visibility
maintenance state
HA status
DRS status
```

Use:

| Host | CPU | Cores | RAM | Free RAM | NICs | Storage | ESXi |
|---|---|---:|---:|---:|---|---|---|
| ESX-01 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| ESX-02 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| ESX-03 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| ESX-04 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

---

## 4. Storage Inventory

For every datastore collect:

```text
name
type
VMFS/NFS/vSAN/local
host visibility
capacity
used
free
performance characteristics
thin provisioning support
snapshot suitability
template suitability
range VM suitability
```

Explicitly inspect the ESX-01 location containing:

```text
ISO/
```

Determine:

- datastore name
- path
- free space
- whether ESX-02/03/04 can access it
- whether it is host-local
- whether it should remain the ISO source
- whether Content Library should become the standard source

Do not move anything during discovery.

---

## 5. Networking Inventory

Collect:

```text
physical NICs
uplinks
standard vSwitches
distributed switches
port groups
VLAN IDs
trunks
MTU
NIC teaming
management VMkernel
vMotion VMkernel
storage VMkernel
routing
firewall boundaries
```

Use:

| Network / Port Group | VLAN | Purpose | Hosts | Routed? | Candidate Use |
|---|---:|---|---|---|---|
| TBD | TBD | TBD | TBD | TBD | Management |
| TBD | TBD | TBD | TBD | TBD | Range Control |
| TBD | TBD | TBD | TBD | TBD | Exercise |

---

## 6. Management Plane Discovery

Determine where these can safely live:

```text
TN-MGMT01
TN-DC01
vCenter
ESXi management
TrueNorth API
Keycloak
PostgreSQL
Redis
MinIO
OpenSearch
Taz access
admin workstations
```

Requirements:

- exercise networks must not have unrestricted management access
- students must not reach vCenter/ESXi management
- Taz access must be explicit
- TrueNorth must reach the vCenter API
- TrueNorth must reach its AD/DNS
- telemetry flows must be documented

---

## 7. Range Network Discovery

Determine how isolated exercise networks can be implemented.

Candidates:

```text
VLAN-backed port groups
distributed port groups
ephemeral port groups
NSX, if present
isolated standard switches
```

Collect:

```text
available VLAN range
maximum practical port groups
switch automation capability
trunk availability
DHCP strategy
DNS strategy
default gateway strategy
Internet access strategy
NAT strategy
sensor visibility
```

Do not choose the final method before inventory is complete.

---

## 8. DNS / DHCP / NTP / IPAM

Inventory:

```text
existing DNS
existing DHCP
existing NTP
existing IPAM
available management subnet
available range address space
```

Determine whether TrueNorth development should use existing services or dedicated lab services.

---

## 9. Identity Discovery

Determine:

```text
Windows Server media/licensing
desired development AD domain
management account model
service account model
Keycloak deployment model
LDAP federation requirements
TrueNorth RBAC groups
```

Initial group candidates:

```text
TN-Platform-Admins
TN-Range-Operators
TN-Instructors
TN-Students
TN-Observers
TN-Content-Authors
```

These are placeholders until mapped against actual TrueNorth permissions.

---

## 10. ISO Media Discovery

Known location:

```text
ESX-01 / ISO
```

Identify existing contents.

Needed initially:

```text
Ubuntu Server 24.04 LTS
Windows Server 2022 or 2025
```

Likely later:

```text
Windows 11
additional Linux distributions
specialized appliances
```

Do not automatically download or upload media during discovery.

---

## 11. Template / Content Library Discovery

Inspect:

```text
VM templates
OVF/OVA files
Content Libraries
gold images
existing Windows templates
existing Linux templates
VMware Tools state
cloud-init compatibility
Windows customization specifications
```

Identify reusable assets without assuming they are suitable for attackable cyber ranges.

---

## 12. Capacity Plan

Calculate after discovery:

```text
total physical cores
total RAM
total usable storage
management reserve
range capacity
safe overcommit assumptions
```

Reserve for:

```text
vCenter
ESXi overhead
TN-MGMT01
TN-DC01
monitoring
one-host failure if HA is required
```

Produce:

| Range Size | VMs | vCPU | RAM | Storage | Concurrent Ranges |
|---|---:|---:|---:|---:|---:|
| Small | TBD | TBD | TBD | TBD | TBD |
| Medium | TBD | TBD | TBD | TBD | TBD |
| Large Dev | TBD | TBD | TBD | TBD | TBD |

Do not invent the numbers before collecting hardware data.

---

## 13. Initial VM Placement Recommendation

Recommend placement for:

```text
TN-MGMT01
TN-DC01
TN-RANGE01
```

Consider:

```text
host RAM
datastore locality
HA
DRS
ISO accessibility
management network
```

If the ISO datastore exists only on ESX-01, note whether installation should occur there before migration/template conversion.

---

## 14. vSphere Automation Readiness

Determine availability of:

```text
vCenter API access
service accounts
Terraform-compatible permissions
govc
PowerCLI
Content Library
guest customization
cloud-init
VMware Tools
template cloning
tagging
resource pools
snapshot operations
```

Recommend a least-privilege TrueNorth vCenter service account.

Do not create it unless authorized.

---

## 15. Taz Read-Only Discovery Tools

Taz should eventually answer:

```text
What hosts exist?
How much free capacity exists?
Which datastore should TN-MGMT01 use?
Which networks are suitable for range traffic?
Is the ISO datastore visible to every host?
What templates exist?
Is the cluster healthy?
Where can a 20-VM exercise fit?
```

Suggested tools:

```text
taz.vsphere_inventory
taz.vsphere_hosts
taz.vsphere_capacity
taz.vsphere_datastores
taz.vsphere_networks
taz.vsphere_templates
taz.vsphere_health
```

---

## 16. Safety

Do not execute during discovery:

```text
terraform apply
terraform destroy
VM deletion
snapshot deletion
port-group deletion
datastore formatting
esxcli network changes
PowerCLI Set-* changes
host maintenance changes
```

If a command may modify state, stop and classify it first.

---

## 17. Deployment Readiness Gate

Proceed only when all are known:

- [ ] vCenter endpoint/version.
- [ ] All four ESXi versions.
- [ ] CPU/core count for all hosts.
- [ ] RAM for all hosts.
- [ ] Physical NICs and link speeds.
- [ ] Datastores and free capacity.
- [ ] ESX-01 ISO datastore/path.
- [ ] ISO datastore host visibility.
- [ ] Management network.
- [ ] Candidate range network strategy.
- [ ] VLAN availability.
- [ ] DNS.
- [ ] NTP.
- [ ] DHCP/IPAM plan.
- [ ] Ubuntu Server ISO availability.
- [ ] Windows Server ISO availability.
- [ ] Content Library status.
- [ ] Existing templates.
- [ ] vCenter API access approach.
- [ ] TrueNorth service account plan.
- [ ] Initial VM placement.
- [ ] Management/range isolation plan.
