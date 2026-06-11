# Dell / NetApp Mobile Cyber Range & AI Kit Inventory

## Purpose

This kit is intended to provide a deployable, high-performance cyber range / simulation / AI acceleration environment for COTE-style experimentation, training, synthetic user generation, telemetry collection, and after-action review support.

The design combines:

- Compute for virtualization and range workloads
- GPU acceleration for AI, LLM, synthetic traffic, analytics, and training support
- High-capacity shared storage
- High-speed switching
- Next-generation firewalling
- Ruggedized mobile rack transport

> **Note:** Some exact SKU-level values should be verified against the final Dell / NetApp quote. This file captures the working inventory and known specs from the current planning thread.

---

# 1. Executive Summary

## Kit Summary

| Category | Quantity | Item | Primary Role |
|---|---:|---|---|
| AI / GPU Compute | 1 | Dell PowerEdge R7725 GPU Node | AI inference, training support, synthetic users, LLM workloads |
| Virtualization Compute | TBD | Dell PowerEdge R6625 Servers | Hypervisor / cyber range workload hosts |
| Storage | 2 | NetApp Storage Arrays | Shared storage, VM backing store, range data, telemetry |
| Firewall | 1 | Palo Alto PA-3430 | Perimeter security, segmentation, VPN, traffic inspection |
| Switching | 1 | Dell S5248F-ON or equivalent | 25/100GbE fabric for compute, storage, and firewall connectivity |
| Rack Transport | TBD | Shock-mounted 15U rack cases | Mobile deployment, equipment protection |
| Power | TBD | UPS / PDU components | Runtime protection and clean shutdown |

---

# 2. Dell PowerEdge R7725 GPU Node

## Role

The R7725 is the high-end AI / GPU node in the kit. It is intended to act as the beast-mode accelerator for:

- Local LLM inference
- Multi-agent cyber workflows
- Threat hunting analytics
- Synthetic user and adversary behaviour generation
- Scenario generation
- Automated after-action review summarization
- Large telemetry summarization
- Model fine-tuning / adapter training where appropriate
- GPU-backed data science and cyber analytics

## Known / Working Specification

| Component | Specification |
|---|---|
| Platform | Dell PowerEdge R7725 GPU Node |
| Form Factor | Rack server |
| CPU | AMD EPYC-class dual-socket platform, exact CPU to be verified |
| GPU | 2 × NVIDIA H200 GPUs, based on current R7725 planning reference |
| GPU Use Case | LLM inference, RAG acceleration, model serving, synthetic traffic, analytics |
| Memory | To be verified from final quote |
| Local Storage | To be verified from final quote |
| Network | High-speed NICs / adapters to be verified from final quote |
| Power | High-draw GPU server, requires server-room power/cooling |
| OS Recommendation | Ubuntu Server LTS |
| Remote Access | VPN-protected SSH, preferably through Palo Alto VPN / jump host / Tailscale where allowed |

## Recommended Software Baseline

| Layer | Recommended Component |
|---|---|
| Host OS | Ubuntu Server LTS |
| GPU Runtime | NVIDIA Driver + CUDA Toolkit |
| Containers | Docker Engine + NVIDIA Container Toolkit |
| Model Serving | vLLM, Ollama, or TGI depending on model choice |
| API Gateway | LiteLLM / OpenAI-compatible router |
| UI | Open WebUI, optional |
| Vector DB | Qdrant |
| Monitoring | Prometheus, Grafana, NVIDIA DCGM exporter |
| Remote Admin | SSH, Cockpit optional, VPN only |
| Security | UFW / nftables, Palo Alto policy enforcement, restricted inbound access |

## Candidate AI Workloads

| Workload | Notes |
|---|---|
| Llama 3.1 / 3.3 70B-class | General cyber / PM / coding assistant |
| Qwen Coder family | Coding-heavy agent workflows |
| Mistral / Mixtral family | General reasoning and fast assistant workloads |
| Vision model | Diagram/image interpretation if supported |
| Embedding model | RAG search over docs, advisories, logs, artifacts |
| Reranker model | Improves retrieval quality for threat hunt and project-doc workflows |

---

# 3. Dell PowerEdge R6625 Hypervisor / Control Plane Servers

## Role

The R6625 systems provide the main virtualization and cyber range compute layer. These are the workhorse nodes for:

- VM hosting
- Cyber range infrastructure
- Scenario environments
- Exercise networks
- Project / AI support services
- Management tools
- Security tooling
- Telemetry collection
- Local DevSecOps and orchestration services

## Known / Working Specification Variants

### R6625 Control Plane Variant

| Component | Specification |
|---|---|
| Platform | Dell PowerEdge R6625 |
| Role | Control plane / management / orchestration |
| CPU | AMD EPYC 9754-class, 96 cores, if using previously discussed control-plane option |
| RAM | 512GB, if using previously discussed control-plane option |
| Local Storage | Approx. 7.6TB NVMe, if using previously discussed control-plane option |
| Hypervisor | VMware / vSphere, Proxmox, or other selected platform |
| Notes | Best suited for management, orchestration, core services, and smaller supporting workloads |

### R6625 Hypervisor Plane Variant

| Component | Specification |
|---|---|
| Platform | Dell PowerEdge R6625 |
| Role | Main cyber range hypervisor host |
| CPU | AMD EPYC 9654-class, 96 cores, if using previously discussed hypervisor option |
| RAM | 1TB, if using previously discussed hypervisor option |
| Local Storage | Approx. 15TB NVMe, if using previously discussed hypervisor option |
| Hypervisor | VMware / vSphere preferred for SimSpace-style integration, Proxmox possible for lab use |
| Notes | Best suited for dense VM workloads and range simulation nodes |

## Recommended Baseline Services

| Service | Purpose |
|---|---|
| Hypervisor Manager | vCenter / vSphere or Proxmox cluster manager |
| DNS / DHCP / NTP | Range infrastructure services |
| Git / Registry | Local automation and image repository |
| Telemetry Stack | Elastic / OpenSearch / HELK / Security Onion-style tooling |
| Identity | Keycloak / AD / LDAP depending on range design |
| Automation | Ansible, Terraform, Packer |
| Jump Host | Controlled administrative entry point |
| Logging | Syslog, OpenSearch, Velociraptor, Zeek, Suricata as required |

---

# 4. NetApp Storage Arrays

## Role

The NetApp arrays provide shared storage for the mobile range environment.

Primary uses:

- VM datastore storage
- Snapshot-backed exercise reset
- Range images and golden templates
- Telemetry archives
- Exercise datasets
- ISO / image repository
- AI / RAG document corpus
- Backup staging
- Rapid recovery between training iterations

## Working Specification

| Component | Specification |
|---|---|
| Vendor | NetApp |
| Quantity | 2 arrays |
| Capacity | Approx. 300TB each, based on previous kit planning |
| Combined Raw / Nominal Capacity | Approx. 600TB before dedupe/compression assumptions |
| Use Case | Shared cyber range storage and data platform |
| Connectivity | High-speed Ethernet / storage fabric, exact ports to verify |
| Protocols | NFS / iSCSI / SMB, final design dependent |
| Resilience | NetApp snapshots, replication, dedupe, compression |
| Notes | Excellent for repeatable cyber range reset and template lifecycle |

## Suggested Storage Layout

| Storage Pool / Share | Purpose |
|---|---|
| `vm-datastores` | Hypervisor VM backing storage |
| `golden-images` | Clean base VM images and templates |
| `exercise-artifacts` | Exercise build files, injects, malware-safe samples, PCAPs |
| `telemetry` | Logs, Zeek, Suricata, Velociraptor exports |
| `rag-corpus` | Project docs, cyber reports, advisories, courseware |
| `backups` | Config backups, snapshots, exported VMs |
| `aar-output` | After-action reports, timelines, generated summaries |

---

# 5. Palo Alto PA-3430 Firewall

## Role

The Palo Alto firewall provides the secure boundary and segmentation layer for the kit.

Primary uses:

- VPN termination
- Range segmentation
- North/south traffic inspection
- East/west policy enforcement where routed through firewall
- Internet control during exercises
- Safe exposure of management services
- OPFOR / blue team / admin zone separation
- Logging and packet inspection

## Working Specification

| Component | Specification |
|---|---|
| Vendor | Palo Alto Networks |
| Model | PA-3430 |
| Role | NGFW / VPN / segmentation gateway |
| Interfaces | 40/100GbE capable platform class, exact optic/interface layout to verify |
| Use Case | Secure mobile cyber range perimeter |
| Deployment Mode | Routed firewall preferred |
| Management | Dedicated management interface / admin VLAN |
| VPN | Remote admin access and controlled participant access |

## Suggested Zones

| Zone | Purpose |
|---|---|
| `MGMT` | Admin access, hypervisor management, NetApp management |
| `RANGE-BLUE` | Blue team systems |
| `RANGE-RED` | OPFOR / adversary systems |
| `RANGE-USERS` | Synthetic users / student endpoints |
| `SERVICES` | DNS, DHCP, Git, registry, monitoring |
| `AI` | R7725 / model-serving network |
| `STORAGE` | NetApp and datastore access |
| `WAN` | Controlled uplink / external access |

---

# 6. Dell S5248F-ON / High-Speed Switch

## Role

The switch is the high-speed network fabric tying together servers, NetApp storage, firewall, and management infrastructure.

## Working Specification

| Component | Specification |
|---|---|
| Vendor | Dell |
| Model | S5248F-ON or equivalent |
| Role | Core / aggregation switch |
| Access Ports | 48 × 25GbE class ports, based on discussed model |
| Uplinks | 100GbE uplinks, based on discussed model |
| Use Case | Compute-storage-firewall fabric |
| OS | Dell OS10 / SONiC option, depending procurement |
| Notes | Ideal for dense 25Gb server links with 100Gb uplinks to firewall/storage/core |

## Suggested VLANs

| VLAN | Name | Purpose |
|---:|---|---|
| 10 | `mgmt` | Out-of-band / management |
| 20 | `storage` | NetApp / hypervisor storage |
| 30 | `vmotion-or-live-migration` | Hypervisor migration traffic |
| 40 | `range-services` | DNS, DHCP, Git, registry, monitoring |
| 50 | `range-blue` | Blue team networks |
| 60 | `range-red` | OPFOR networks |
| 70 | `range-user` | Student / synthetic user systems |
| 80 | `ai` | R7725 / GPU / model-serving network |
| 90 | `telemetry` | Logs, PCAPs, sensors |
| 100 | `wan-edge` | Firewall/uplink transit |

---

# 7. Rack, Power, and Mobility Components

## Role

The kit is intended to be mobile or semi-mobile. The ruggedized rack system protects the equipment and allows it to be deployed as a self-contained cyber range package.

## Working Specification

| Component | Specification |
|---|---|
| Rack Type | Shock-mounted 19-inch mobile rack |
| Rack Size | 15U class, based on previous planning |
| Quantity | TBD |
| Mobility | Removable casters / forklift-friendly case preferred |
| Power | Rack PDU + UPS required |
| Cooling | Server-room / conditioned-space deployment strongly recommended |
| Notes | GPU nodes and NetApp arrays are heavy, loud, and power-hungry |

## Recommended Rack Split

### Rack 1: Compute / AI

| Equipment | Notes |
|---|---|
| R7725 GPU Node | AI / LLM / analytics |
| R6625 compute nodes | Hypervisor / range compute |
| Switch | Top-of-rack fabric |
| PDU | Managed PDU preferred |

### Rack 2: Storage / Security

| Equipment | Notes |
|---|---|
| NetApp Array 1 | Primary storage |
| NetApp Array 2 | Secondary / replica / expansion |
| Palo Alto PA-3430 | Firewall / VPN / segmentation |
| UPS | Runtime protection |
| PDU | Managed PDU preferred |

---

# 8. Capability Mapping

## What This Kit Enables

| Capability | Supported By |
|---|---|
| Cyber range virtualization | R6625 servers + NetApp |
| AI-assisted exercise generation | R7725 GPU node |
| Synthetic user traffic | R7725 + R6625 VMs |
| Defensive threat hunting | R7725 + telemetry stack |
| AAR generation | R7725 + logs + RAG corpus |
| Rapid range reset | NetApp snapshots |
| Segmented exercise networks | Palo Alto + Dell switch |
| High-speed storage fabric | Dell S5248F + NetApp |
| Remote admin access | Palo Alto VPN / jump host |
| Offline / sovereign operation | Local compute + local storage + local models |

---

# 9. Proposed Logical Architecture

```text
                           [ Remote Admin / VPN ]
                                    |
                              [ Palo Alto PA-3430 ]
                                    |
               ------------------------------------------------
               |                    |                         |
            MGMT VLAN            RANGE VLANs                WAN / Transit
               |                    |
        [ Dell S5248F-ON Core / ToR Switch ]
               |
     ---------------------------------------------------------
     |                         |                             |
[ Dell R6625 Hosts ]     [ Dell R7725 GPU Node ]       [ NetApp Arrays ]
     |                         |                             |
Cyber Range VMs          LLM / AI / Analytics          VM Datastores
Scenario Infra           Synthetic Users               Snapshots
Telemetry Tools          AAR Generation                RAG Corpus
```

---

# 10. Recommended Deployment Pattern

## Phase 1: Physical Build

- Rack equipment
- Cable management
- Label all ports and VLANs
- Confirm power draw
- Confirm cooling
- Confirm management access
- Build an asset register

## Phase 2: Network Foundation

- Configure switch management
- Configure VLANs
- Configure trunk/access ports
- Configure Palo Alto zones
- Configure firewall policies
- Configure VPN access
- Configure DNS / NTP

## Phase 3: Storage Foundation

- Configure NetApp management
- Create aggregates / storage pools
- Create NFS/iSCSI datastores
- Create snapshot policies
- Create replication policy between arrays if applicable
- Present storage to hypervisors

## Phase 4: Compute Foundation

- Install hypervisor on R6625 nodes
- Add shared datastores
- Configure virtual networking
- Deploy base infrastructure VMs
- Deploy jump host
- Deploy monitoring

## Phase 5: AI Foundation

- Install Ubuntu Server on R7725
- Install NVIDIA drivers
- Install Docker + NVIDIA Container Toolkit
- Deploy model-serving stack
- Deploy vector DB
- Deploy Open WebUI or API front end
- Deploy monitoring

## Phase 6: Cyber Range Services

- Deploy identity services
- Deploy telemetry stack
- Deploy Velociraptor / sensors
- Deploy scenario templates
- Deploy golden images
- Build reset/snapshot workflows

---

# 11. Minimum Operating Baseline

The kit should not be considered operational until the following work:

## Physical

- [ ] Rack layout complete
- [ ] Power budget validated
- [ ] Cooling validated
- [ ] Cable labels complete
- [ ] Console access validated

## Network

- [ ] Switch reachable
- [ ] Palo Alto reachable
- [ ] VLANs configured
- [ ] Management network isolated
- [ ] VPN works
- [ ] Firewall rules documented

## Storage

- [ ] NetApp arrays reachable
- [ ] Datastores created
- [ ] Snapshot policies configured
- [ ] Hypervisors can mount storage
- [ ] Recovery test completed

## Compute

- [ ] Hypervisors installed
- [ ] Cluster configured
- [ ] Templates created
- [ ] Live migration tested
- [ ] VM deployment tested

## AI

- [ ] R7725 OS installed
- [ ] NVIDIA drivers validated
- [ ] GPU visible via `nvidia-smi`
- [ ] Model server online
- [ ] API reachable from management network
- [ ] At least one 70B-class model tested
- [ ] Monitoring dashboards working

---

# 12. Open Verification Items

These should be checked against the final quote / build sheet:

| Item | Needs Verification |
|---|---|
| R7725 exact CPU model(s) | Yes |
| R7725 RAM quantity | Yes |
| R7725 local disk layout | Yes |
| R7725 NIC layout | Yes |
| R7725 GPU part numbers | Yes |
| R6625 quantity | Yes |
| R6625 exact CPU models | Yes |
| R6625 RAM per node | Yes |
| R6625 storage per node | Yes |
| NetApp exact model | Yes |
| NetApp usable capacity | Yes |
| NetApp controller / shelf layout | Yes |
| Switch exact model / OS | Yes |
| Palo Alto optics / interface modules | Yes |
| Rack case count | Yes |
| UPS/PDU specs | Yes |

---

# 13. One-Line Description

A deployable Dell / NetApp cyber range and AI acceleration kit built around R6625 virtualization hosts, an R7725 2×H200 GPU node, dual high-capacity NetApp arrays, a Palo Alto PA-3430 firewall, and a 25/100GbE Dell switching fabric.

---

# 14. Short Briefing Description

This acquisition provides a mobile, high-performance cyber range infrastructure package capable of supporting virtualized training environments, AI-assisted scenario generation, synthetic user activity, telemetry analysis, and after-action review production. The Dell compute layer provides dense virtualization and GPU acceleration, the NetApp arrays provide resilient shared storage and rapid reset capability, and the Palo Alto / Dell network layer provides secure segmentation, VPN access, and high-speed fabric connectivity.

---

# 15. Practical Notes

- The R7725 should be treated as a server-room GPU node, not a desktop-style AI box.
- The NetApp arrays are central to repeatability; snapshots are the magic trick, not just raw terabytes.
- The firewall and switch design should be finalized before VM deployment, otherwise the range will turn into spaghetti with blinking lights.
- Keep management, storage, range, telemetry, and AI networks separated from day one.
- Build a quote-verified asset sheet before delivery acceptance.
