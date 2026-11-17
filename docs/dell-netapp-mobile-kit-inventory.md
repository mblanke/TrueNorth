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

> **Claim-testing status (2026-11-08, checked against this repository's source):**
> `docs/guides/01-site-prerequisites.md` §1.1 is the owner's current reference kit and states
> (lines 70–72) that this file is an **earlier planning inventory** — NetApp arrays, a different
> firewall model and 15U cases — and that where the two disagree, that table is the current design.
> Marks used below: **[source: file:line]** = corroborated by repository source;
> **[superseded — 01-site-prerequisites §1.1]** = contradicted by the current reference kit;
> **[unverified — needs quote/datasheet]** = no repository source can confirm it;
> **[live-only]** = can only be checked on the built kit, never from this repository.

---

# 1. Executive Summary

## Kit Summary

| Category | Quantity | Item | Primary Role |
|---|---:|---|---|
| AI / GPU Compute | 1 | Dell PowerEdge R7725 GPU Node | AI inference, training support, synthetic users, LLM workloads |
| Virtualization Compute | TBD | Dell PowerEdge R6625 Servers | Hypervisor / cyber range workload hosts |
| Storage | 2 | NetApp Storage Arrays | Shared storage, VM backing store, range data, telemetry |
| Firewall | 1 | Palo Alto PA-3430 [superseded — 01-site-prerequisites §1.1:71 "a different firewall model"; the current design names no model] | Perimeter security, segmentation, VPN, traffic inspection |
| Switching | 1 [superseded — current design has 2×, 01-site-prerequisites.md:61] | Dell S5248F-ON or equivalent [source: 01-site-prerequisites.md:61] | 25/100GbE fabric for compute, storage, and firewall connectivity |
| Rack Transport | TBD | Shock-mounted 15U rack cases [superseded — the current reference kit is 9U, 01-site-prerequisites.md:50,72] | Mobile deployment, equipment protection |
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
| Platform | Dell PowerEdge R7725 GPU Node [source: docs/ai-fleet-sizing.md:18, docs/guides/01-site-prerequisites.md:60] |
| Form Factor | Rack server |
| CPU | AMD EPYC-class dual-socket platform, exact CPU to be verified [source: docs/ai-fleet-sizing.md:22 — the deployed AI node is 2× AMD EPYC 9535, 256 threads] |
| GPU | 2 × NVIDIA H200 GPUs, based on current R7725 planning reference [source: docs/ai-fleet-sizing.md:25 — 2× NVIDIA H200 NVL, 144 GB each] |
| GPU Use Case | LLM inference, RAG acceleration, model serving, synthetic traffic, analytics |
| Memory | To be verified from final quote [source: docs/ai-fleet-sizing.md:23 — the deployed node has 2.0 TiB RAM] |
| Local Storage | To be verified from final quote [source: docs/ai-fleet-sizing.md:24 — the deployed node has 14 TB NVMe `/data`] |
| Network | High-speed NICs / adapters to be verified from final quote |
| Power | High-draw GPU server, requires server-room power/cooling |
| OS Recommendation | Ubuntu Server LTS [source: docs/guides/01-site-prerequisites.md:60 — bare-metal Ubuntu 24.04] |
| Remote Access | VPN-protected SSH, preferably through Palo Alto VPN / jump host / Tailscale where allowed [source: docs/arc2-course-studio.md:516 and install/README.md:946 — Tailscale is in use] |

## Recommended Software Baseline

| Layer | Recommended Component |
|---|---|
| Host OS | Ubuntu Server LTS |
| GPU Runtime | NVIDIA Driver + CUDA Toolkit |
| Containers | Docker Engine + NVIDIA Container Toolkit [source: docs/guides/02-install-guide.md:923-935] |
| Model Serving | vLLM, Ollama, or TGI depending on model choice [source: docs/ai-fleet-sizing.md:27 — the deployed stack is vLLM behind LiteLLM, explicitly "not Ollama"; TGI unverified] |
| API Gateway | LiteLLM / OpenAI-compatible router [source: docs/ai-fleet-sizing.md:31] |
| UI | Open WebUI, optional [source: install/README.md:395, control-plane/api/app/ai_backends/base.py:28] |
| Vector DB | Qdrant [unverified — the repository AI stack backs its RAG with pgvector (docs/ai-fleet-sizing.md:34); Qdrant appears nowhere else in source] |
| Monitoring | Prometheus, Grafana, NVIDIA DCGM exporter [source: RESUME.md:157-166 — dcgm-exporter container with a Prometheus `dcgm` target; infra/platform/docker/compose.monitoring.yml] |
| Remote Admin | SSH, Cockpit optional, VPN only [unverified — Cockpit appears nowhere in source] |
| Security | UFW / nftables, Palo Alto policy enforcement, restricted inbound access [source: docs/deployment/vmware-site-runbook.md:203 — nftables; UFW unverified] |

## Candidate AI Workloads

| Workload | Notes |
|---|---|
| Llama 3.1 / 3.3 70B-class | General cyber / PM / coding assistant [source: ai-orchestrator/app/backends/vllm.py:11 — `meta-llama/Llama-3.3-70B-Instruct` example; docs/architecture.md:788] |
| Qwen Coder family | Coding-heavy agent workflows [source: docs/taz-model-recommendation.md:9 — Qwen/Qwen3-Coder-Next; docs/ai-fleet-sizing.md:47 — Qwen 112B-A10B FP8 target] |
| Mistral / Mixtral family | General reasoning and fast assistant workloads [source: docs/ai-fleet-sizing.md:40-41 — Mistral 7B, Mistral Small 3 24B; docs/architecture.md:789 — mixtral:8x7b] |
| Vision model | Diagram/image interpretation if supported [source: docs/ai-fleet-sizing.md:41 — Mistral Small 3 24B is multimodal] |
| Embedding model | RAG search over docs, advisories, logs, artifacts [source: docs/ai-fleet-sizing.md:42,49 — bge / bge-m3 embedding server] |
| Reranker model | Improves retrieval quality for threat hunt and project-doc workflows [unverified — no reranker appears in source] |

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
| CPU | AMD EPYC 9754-class, 96 cores, if using previously discussed control-plane option [unverified — needs quote; source names the role only, docs/guides/01-site-prerequisites.md:58] |
| RAM | 512GB, if using previously discussed control-plane option [unverified — needs quote] |
| Local Storage | Approx. 7.6TB NVMe, if using previously discussed control-plane option [unverified — needs quote] |
| Hypervisor | VMware / vSphere, Proxmox, or other selected platform [source: docs/guides/01-site-prerequisites.md:58 — ESXi 8.0; docs/architecture.md:39-48 — Proxmox provider] |
| Notes | Best suited for management, orchestration, core services, and smaller supporting workloads |

### R6625 Hypervisor Plane Variant

| Component | Specification |
|---|---|
| Platform | Dell PowerEdge R6625 |
| Role | Main cyber range hypervisor host |
| CPU | AMD EPYC 9654-class, 96 cores, if using previously discussed hypervisor option [source: docs/guides/01-site-prerequisites.md:57 — 2× AMD EPYC 9654] |
| RAM | 1TB, if using previously discussed hypervisor option [source: docs/guides/01-site-prerequisites.md:57; docs/vm-build-sheet.md:217-218 — "1 TB RAM each in the hypervisor variant"] |
| Local Storage | Approx. 15TB NVMe, if using previously discussed hypervisor option [source: docs/guides/01-site-prerequisites.md:57 — 15.36 TB NVMe] |
| Hypervisor | VMware / vSphere preferred for SimSpace-style integration, Proxmox possible for lab use [source: docs/guides/01-site-prerequisites.md:57 — ESXi 8.0; docs/TrueNorth Range.md:236 — SimSpace reference] |
| Notes | Best suited for dense VM workloads and range simulation nodes |

## Recommended Baseline Services

| Service | Purpose |
|---|---|
| Hypervisor Manager | vCenter / vSphere or Proxmox cluster manager |
| DNS / DHCP / NTP | Range infrastructure services |
| Git / Registry | Local automation and image repository |
| Telemetry Stack | Elastic / OpenSearch / HELK / Security Onion-style tooling [source: infra/platform/docker/compose.prod.yml:383-384 — OpenSearch; docs/README.md:122 + docs/TrueNorth Range.md:18 — HELK; CHANGELOG.md:490 — Security Onion per range] |
| Identity | Keycloak / AD / LDAP depending on range design [source: .env.example:18-23 — Keycloak] |
| Automation | Ansible, Terraform, Packer [source: install/roles/ (Ansible); docs/BUILD.md:471 + .github/workflows/terraform-plan.yml (Terraform); .github/workflows/packer-build.yml (Packer)] |
| Jump Host | Controlled administrative entry point |
| Logging | Syslog, OpenSearch, Velociraptor, Zeek, Suricata as required [source: content/ranges/large-enterprise/template.yaml:366-378,390-396 — Suricata and Velociraptor services; content/courses/instructor/up-net-03-network-monitoring-with-zeek-suricata.yaml — Zeek] |

---

# 4. NetApp Storage Arrays

> **[superseded — 01-site-prerequisites §1.1]** The owner's current reference kit
> (docs/guides/01-site-prerequisites.md:55-64) has **no NetApp arrays**; range hosts carry one
> local datastore each (line 59). This section survives as planning input: the greyspace plan
> still references this inventory's NetApp figure (~600 TB, NFS read-only,
> docs/greyspace-plan.md:36-37) and its storage-VLAN concept (docs/greyspace-corpus.md:131).

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
| Vendor | NetApp [source: docs/adr/0007-greyspace.md:30-31 — NetApp, NFS read-only] |
| Quantity | 2 arrays [unverified — needs quote] |
| Capacity | Approx. 300TB each, based on previous kit planning [unverified — needs quote; the ~600 TB combined figure is quoted as planning input in docs/greyspace-plan.md:37] |
| Combined Raw / Nominal Capacity | Approx. 600TB before dedupe/compression assumptions [unverified — needs quote] |
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

> **[superseded — 01-site-prerequisites §1.1]** The current design keeps a **Palo Alto** firewall
> (docs/guides/01-site-prerequisites.md:63) but does **not** name the PA-3430; the guide's note
> (line 71) lists "a different firewall model" as one of the ways this inventory differs from the
> current design. The current reference firewall configuration is §4.2 of that guide.

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
| Model | PA-3430 [superseded — current design names no model, docs/guides/01-site-prerequisites.md:63,71] |
| Role | NGFW / VPN / segmentation gateway |
| Interfaces | 40/100GbE capable platform class, exact optic/interface layout to verify [unverified — needs datasheet/quote] |
| Use Case | Secure mobile cyber range perimeter |
| Deployment Mode | Routed firewall preferred |
| Management | Dedicated management interface / admin VLAN |
| VPN | Remote admin access and controlled participant access |

## Suggested Zones

> **[superseded — 01-site-prerequisites §2]** TrueNorth's isolation model is
> `vlan_trunk_no_l3` (docs/guides/01-site-prerequisites.md:110-118): **no device has a layer-3
> interface on any range VLAN**, and the site firewall **denies** all traffic to and from range
> networks. The reference firewall has only four zones — TN-MGMT, TN-OOB, TN-AI,
> TN-RANGE-UPLINK (lines 281-284). Range-blue/red/user zones on the firewall, as below, are not
> part of the current design; range segmentation is per-range, inside the range.

| Zone | Purpose |
|---|---|
| `MGMT` | Admin access, hypervisor management, NetApp management [source: docs/guides/01-site-prerequisites.md:132 — management VLAN routed by the firewall] |
| `RANGE-BLUE` | Blue team systems [superseded — range VLANs have no L3 anywhere, 01-site-prerequisites.md:114-118] |
| `RANGE-RED` | OPFOR / adversary systems [superseded — as above] |
| `RANGE-USERS` | Synthetic users / student endpoints [superseded — as above] |
| `SERVICES` | DNS, DHCP, Git, registry, monitoring [unverified — no such zone in the current design] |
| `AI` | R7725 / model-serving network [source: docs/guides/01-site-prerequisites.md:135,281-284 — TN-AI zone; only TCP 4000 (LiteLLM) allowed from management, line 260] |
| `STORAGE` | NetApp and datastore access [superseded with §4 — no NetApp in the current kit; referenced only by the greyspace plan, docs/greyspace-corpus.md:131] |
| `WAN` | Controlled uplink / external access [source: docs/guides/01-site-prerequisites.md:136,295-298 — TN-RANGE-UPLINK zone, deny-all in and out] |

---

# 6. Dell S5248F-ON / High-Speed Switch

## Role

The switch is the high-speed network fabric tying together servers, NetApp storage, firewall, and management infrastructure.

## Working Specification

| Component | Specification |
|---|---|
| Vendor | Dell |
| Model | S5248F-ON or equivalent [source: docs/guides/01-site-prerequisites.md:61 — the current kit has 2× S5248F-ON (SONiC)] |
| Role | Core / aggregation switch |
| Access Ports | 48 × 25GbE class ports, based on discussed model [unverified — needs datasheet; the guide's port map is consistent with it: host ports Ethernet0-5, ISL Ethernet48/52, firewall Ethernet56, docs/guides/01-site-prerequisites.md:198-200] |
| Uplinks | 100GbE uplinks, based on discussed model [unverified — needs datasheet] |
| Use Case | Compute-storage-firewall fabric |
| OS | Dell OS10 / SONiC option, depending procurement [source: docs/guides/01-site-prerequisites.md:186-194 — SONiC; Dell Enterprise SONiC Klish CLI named for the trunk form] |
| Notes | Ideal for dense 25Gb server links with 100Gb uplinks to firewall/storage/core |

## Suggested VLANs

> **[superseded — 01-site-prerequisites §2.1]** The reference VLAN plan differs and collides
> with this one: management 10, out-of-band 11, **vMotion 20**, **AI 30**, range uplink 40, and
> range networks come from a **pool of 2000–2999** with no layer 3 anywhere
> (docs/guides/01-site-prerequisites.md:130-137). Below, 20/30/40 mean different things and AI
> is 80. The reference kit's switch configuration uses the guide's numbers
> (docs/guides/01-site-prerequisites.md:201-203).

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
| Rack Size | 15U class, based on previous planning [superseded — the current reference kit is 9U, docs/guides/01-site-prerequisites.md:50,72] |
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

> **[live-only]** Every checkbox below requires the built kit (hardware, network, storage or a
> live model server). None of them can be verified from this repository.

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

A deployable Dell / NetApp cyber range and AI acceleration kit built around R6625 virtualization hosts, an R7725 2×H200 GPU node [source: docs/ai-fleet-sizing.md:25], dual high-capacity NetApp arrays [superseded — no NetApp in the current kit, docs/guides/01-site-prerequisites.md:59-64], a Palo Alto PA-3430 firewall [superseded — model not in the current design, docs/guides/01-site-prerequisites.md:63,71], and a 25/100GbE Dell switching fabric [25GbE per docs/guides/01-site-prerequisites.md:61; 100GbE unverified].

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
