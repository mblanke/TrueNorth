# COTE: pre-production on the mobile kit, production on DND hosting

**Status:** draft, 2026-11-13. **Supersedes** the role layout and budget in
`cote-warehouse-kit-gap-analysis.md`. That document's kit inventory and warehouse checks (§1, §4)
still apply.

**Decisions taken (owner, 2026-11-13):**
- The warehouse **DSG mobile kit is pre-production**.
- **Production runs on DND hosting** provided by ADM(IM)/SSC. The project funds licences, integration
  and missing items, not production hardware.
- **Secret / low-to-high is not being tested yet; direction to follow.** No kit hardware moves to
  Secret. Secret-side spending is **parked** in later years, and released or redirected when direction
  arrives.

ROM figures are rough order-of-magnitude CAD for budgeting, not quotes.

---

## 1. Three environments

| | Pre-production | Production (Unclassified) | Secret result authority |
|---|---|---|---|
| Where | DSG mobile kit (warehouse → secure room) | DND hosting (ADM(IM)/SSC vSphere) | Existing Secret infrastructure (TBD) |
| Purpose | Prove and rehearse everything before it reaches Students | Students, instructors, official training delivery | Names, numeric grades, official record |
| Status | Owned, 5-yr hardware support | **Hosting request to be raised** | **Parked: direction to follow** |
| TrueNorth install | Full: platform, ranges, AI node, depot, build host | Full platform and ranges; authoring optional | Platform only, no ranges |

### Pre-production layout (repo reference: `docs/guides/01-site-prerequisites.md` §1.1)

| Kit unit | Role |
|---|---|
| R6625 Hypervisor Plane (1 TB) | Management host 1: the **TrueNorth VM** (32 vCPU / 128 GB / 200 GB + 1.3 TB) and the depot |
| R6625 Control Plane (512 GB) | Management host 2: vCenter, the backup target VM, the DC VM, TN-BUILD01, TN-KMS01 |
| 4× R6625 compute (1.5 TB each) | Range hosts: about **5 concurrent classes** of capacity, enough to run real load and soak tests |
| R7725 (2× H200) | Bare-metal AI node (vLLM + LiteLLM): content authoring, ARC² and AI features |
| 2× NetApp A50 | Datastores / content library, plus a SnapMirror replica and backups |
| S5248F-ON ×2, E3248, Palo Alto, UPS | Fabric, OOB, perimeter, power, configured as guide 01 §3–§4 |

**Licensing option:** guide 01 runs all six R6625s as ESXi (1,216 cores of vSphere). To cut the
per-core subscription, either run the TrueNorth platform bare metal on the Control Plane node (the
installer targets "a host with Docker") and license 5 hosts, or license only the 4 compute nodes plus
one management host. The budget below assumes **4 licensed ESXi hosts (768 cores)**.

### What pre-production is used for

1. **Gate 2, the real-vSphere evidence** that is missing today: range build, reset, snapshot, restore,
   host loss, failed-build cleanup and isolation checks, with range-certification records.
2. **Golden image builds** (Packer on TN-BUILD01, offline depot), and publishing them as the template
   set that production imports.
3. **Content authoring** on the AI node (ARC²), with reviews and pilot trials before release.
4. **Release-candidate acceptance:** install, upgrade, rollback, load (200 → 500 → 1,200 users),
   72-hour soak and failure drills, before any release is promoted to production.
5. **Range Ops and instructor training** without touching production.
6. *Later, when Secret direction arrives:* a **two-install transfer rehearsal**. A second isolated
   TrueNorth on one compute node acts as the high side, to test sealed result transfer and
   receipts (ADR 0015: the contract can be tested between isolated deployments before any
   cross-domain product is chosen).

## 2. Production on DND hosting: what to ask ADM(IM)/SSC for

These are TrueNorth's real requirements. Shared hosting most often refuses items 3–5, so raise
them first.

| # | Requirement | Detail (repo source) |
|---:|---|---|
| 1 | **vCenter 8.0 API access** for a service account | One datacenter; range hosts in their own cluster or a **dedicated resource pool** (guide 01 §1) |
| 2 | **Capacity** | Per class: about **420 vCPU / 920 GB RAM** of range ceiling (instant clones share pages), plus the TrueNorth VM (32 vCPU / 128 GB / ~1.5 TB), backup target, DC and depot VMs. Start with **1 class** and grow to 2–3 |
| 3 | **A vSphere Distributed Switch that TrueNorth may write to** | TrueNorth **creates and deletes port groups itself** (`tn-<range8>-v<vlan>`) on a range vDS (guide 01 §2.2) |
| 4 | **A reserved VLAN block** (e.g. 100–1,000 VLANs) trunked to the range hosts, with **no Layer 3 anywhere** | Range VLAN pool (guide 01 §2.1). Ranges are isolated and have no egress |
| 5 | **vCenter role privileges** (`TrueNorth-Provision`) | Network.Assign; DVPortgroup.Create/Modify/Delete; Folder.Create/Delete; VirtualMachine.Provisioning.Clone/DeployTemplate/Customize; VirtualMachine.Config.*; Interact.PowerOn/Off; State.* (snapshots); GuestOperations.Execute/Query/Modify; Datastore.AllocateSpace; Resource.AssignVMToPool; ContentLibrary.*; Cryptographer.* (Windows 11 vTPM) (`vmware-site-runbook.md` §4.2) |
| 6 | **Port mirroring** on the range vDS | SOC ranges feed sensors by mirroring (vDS RSPAN across hosts, or one host per range). Promiscuous mode on the monitoring port group only |
| 7 | Datastore | ~30 TB thin to start (range ceilings, templates, OpenSearch, evidence); snapshots allowed |
| 8 | Management network | A management port group for the TrueNorth VM, DC, backup and depot. **TrueNorth refuses to put range VMs on it** |
| 9 | Network flows (guide 01 §4.3) | TrueNorth VM → vCenter 443, → every ESXi host 443 (guest file transfer, console relay), → DC 636/53/123, → backup 22. Users → TrueNorth 443 (8443 if Moodle) |
| 10 | Identity, DNS, certificates | AD over LDAPS for Keycloak federation; DNS names and certificates for the TrueNorth URL; NTP |
| 11 | Student and instructor access | A route from the training network / DWAN to TrueNorth on 443. Range consoles are relayed through TrueNorth, so ranges are never exposed |
| 12 | Physical training kits (GRIMM) | A path from the training-site hardware gateway to production, through the site firewall (8.5 plan, Epic 3C) |
| 13 | Backups and monitoring | A backup target outside the TrueNorth VM; alert delivery to the operators |
| 14 | Security authorization | TrueNorth's SA&A inside the hosting environment's existing authorization; ranges run deliberately vulnerable and malicious content inside isolated VLANs, so state this up front |
| 15 | AI features | Optional in production. Authoring stays in pre-production; production can run without an AI node at IOC (features hidden), or request GPU capacity |
| 16 | Cost | Hosting chargeback, if any (**[TBC]**: reserved below) |

## 3. Promoting from pre-production to production

Nothing is built by hand in production. Each promotion is an offline, signed package:

| What | How |
|---|---|
| Platform release | cosign-signed images by digest, `release-manifest.json`, SBOMs, Trivy reports (`docs/release.md`); the installer verifies the signature and installs by digest |
| Golden templates | Built and certified in pre-production, exported as OVF/OVA with checksums, imported to the production content library, then range-certified again in production |
| Course content | Approved course releases (ARC² release bundles / cmi5 packages) imported through TrueNorth's publishing flow |
| Configuration | Kept per environment (secrets, addresses and keys are generated in the destination, never copied) |
| Approval | A release goes to production only after its pre-production acceptance record (§1, item 4) is signed off |

## 4. What is missing, and the budget ($1.8M hardware/software, ROM CAD)

**Re-phased:** with no production hardware to buy, FY 27/28 cannot usefully absorb $1.1M of
hardware/software. Proposed split: **$650K / $650K / $500K**. Professional services stay at
$400K/yr, the total stays at $3.0M, and the PAD cash flow is updated to match.

| Item | FY 27/28 | FY 28/29 | FY 29/30 | Total |
|---|---:|---:|---:|---:|
| VMware vSphere for pre-production (4 ESXi hosts / 768 cores) | 145,000 | 145,000 | 145,000 | 435,000 |
| Microsoft: pre-production uses **evaluation media** (`vm-build-sheet.md` §1); production under the DND agreement. Reserve for gaps (e.g. Office LTSC, KMS host) | 50,000 | — | — | 50,000 |
| Production hosting chargeback (if SSC charges) | 100,000 | 100,000 | 100,000 | 300,000 |
| HSMs: release/content signing in pre-production and production | 25,000 | — | — | 25,000 |
| Range tool licences (Burp Pro, Cobalt Strike, Npcap OEM, KAPE…) | 45,000 | 40,000 | 35,000 | 120,000 |
| GRIMM TinyTown/ICS, then a car-hacking bench (integrated in pre-production first) | 150,000 | 100,000 | — | 250,000 |
| Kit accessories: PDUs, optics, console (warehouse checks) | 45,000 | — | — | 45,000 |
| Student endpoints (only if needed) | 50,000 | — | — | 50,000 |
| Ubuntu Pro (if FIPS crypto / Livepatch is required) | 5,000 | 5,000 | 5,000 | 15,000 |
| **Parked – Secret:** data diode with proxies, plus maintenance | — | 170,000 | 25,000 | 195,000 |
| **Parked – Secret:** high-to-low controlled-media kit | — | 25,000 | — | 25,000 |
| **Parked – Secret:** result-authority host (or VMs on Secret infrastructure) | — | — | 100,000 | 100,000 |
| Quote-variance reserve | 35,000 | 65,000 | 90,000 | 190,000 |
| **Total** | **650,000** | **650,000** | **500,000** | **1,800,000** |

The **$320K parked for Secret** sits in FY 28/29–29/30, so it does not lapse before direction arrives.
If Secret slips past FY 29/30, release it to content, more GRIMM kits or a second production class.
If SSC levies no hosting charge, or VMware is under a DND agreement, up to **$735K** more is freed.

## 5. Questions to close

1. **ADM(IM)/SSC:** can the hosting give a service account write access to a vDS (port-group create
   and delete), a reserved VLAN block with no Layer 3, and port mirroring? If not, TrueNorth needs a
   dedicated cluster within the hosting. *This is the single biggest production risk.*
2. Is there a hosting chargeback, and at what rate?
3. Do DND agreements cover VMware for a non-production training kit, and Microsoft for production?
4. Where will pre-production physically sit, and does it need its own security authorization
   (likely, at a lower bar than production)?
5. Secret: timing, and whether the result authority will be VMs on existing Secret infrastructure.
