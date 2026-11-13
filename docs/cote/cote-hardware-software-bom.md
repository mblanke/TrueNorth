# COTE / CSTE: hardware and software to deploy TrueNorth

**Status:** draft for planning, 2026-11-13. **Hardware purchases superseded** by `cote-gap-bom-with-mobile-kit.md` (the mobile kit is already owned); sizing and software lists here still apply. **Budget frame:** the minor-project PAD, which allocates
$1,800,000 to hardware/software over FY 27/28–29/30 ($1.1M / $450K / $250K). Professional services
($400K/yr) are separate.

**Every dollar figure below is a rough order-of-magnitude (ROM) estimate in CAD, for budgeting
only.** None comes from a quote. Replace each one with a vendor or standing-offer quote before the
figures go into the PAD. VMware and Microsoft licensing carry the largest uncertainty: check first
whether DND enterprise agreements (through SSC) already cover them, because that is the biggest
cost lever in this list.

Sources in this repo: `docs/vm-build-sheet.md` (management plane, templates, per-range capacity,
software list), `docs/dell-netapp-mobile-kit-inventory.md` (hardware working specs),
`docs/deployment/vmware-site-runbook.md` (vSphere prerequisites), `docs/ai-fleet-sizing.md` (AI node),
`infra/platform/docker/compose.prod.yml` (platform services), and
`docs/cote-unclassified-8.5-plan.md` (enclave model, result transfer, IOC targets).

---

## 1. What has to exist (design drivers)

| Driver | Consequence for the bill of materials |
|---|---|
| TrueNorth provisions ranges through **vSphere** (`vsphere_api`); Proxmox and Hyper-V are experimental only | ESXi + vCenter 8 with a **vSphere Distributed Switch** (range port groups on VLANs 100–199, port mirroring for sensors). A vDS needs vSphere Foundation / Enterprise Plus-class licensing, not vSphere Standard |
| Ranges are **no-egress** | An internal software depot (TN-DEPOT01) plus one controlled-egress build host. Every package, image and ISO must be mirrored |
| **Independent install per enclave**; Unclassified holds no names or readable grades; Secret is the grade authority | A full Unclassified range site, plus a small Secret "result authority" installation (platform only, no ranges), plus a low-to-high transfer path |
| IOC target: a 15-person class doing individual labs plus one collective exercise, survivable through a host loss (N+1) | Size the compute below |
| Key custody (HSM/KMS) is an open item in the 8.5 plan | A hardware key store in each enclave |
| Authoring and AI features call a local OpenAI-compatible endpoint (ARC², quiz/course/exercise generation, AAR summaries) | A GPU node, sized for 5–10 authors (it is not in the Student runtime path) |

### Capacity sizing (from `vm-build-sheet.md` §4, ceiling values before instant-clone savings)

| Load | vCPU | RAM |
|---|---:|---:|
| 15 individual labs × medium-enterprise (8 VMs, 20 vCPU, 44 GB each) | 300 | 660 GB |
| 1 collective range: red-vs-blue (40 VMs) **or** soc-training (23 VMs) | 69–80 | 157–160 GB |
| Greyspace (simulated users), sensors and range services | ~40 | ~100 GB |
| **Range total (one class)** | **~420** | **~920 GB** |
| Management plane (TN-MGMT01, DC, depot, build, KMS, WSUS, vCenter) | ~50 | ~260 GB |

The binding constraint is RAM. vCPU is overcommitted at up to 4 per thread
(`tn_vsphere_max_vcpu_per_thread`). Three 1 TB range hosts carry one class, with N+1 headroom from
the shared pages of instant clones. Year 3 adds a fourth host so that two classes can run at once,
or one large-enterprise range (142 vCPU / 375 GB) alongside a class.

---

## 2. Unclassified site: hardware

| # | Item | Qty | Working spec | Role | ROM (CAD) | FY |
|---:|---|---:|---|---|---:|---|
| 1 | Dell PowerEdge R6625: range hosts | 3 | 2× AMD EPYC 9654-class (96c), 1 TB RAM, 2× dual-port 25 GbE, mirrored boot SSD, ~7.6 TB NVMe, iDRAC Enterprise, 3-yr ProSupport | ESXi cluster for ranges | 270,000 | 27/28 |
| 2 | Dell PowerEdge R6625: management node | 1 | 1–2× EPYC (32–64c), 512 GB, 25 GbE, ~7.6 TB NVMe | vCenter, TN-MGMT01, TN-DC01, TN-DEPOT01, TN-BUILD01, TN-KMS01, TN-WSUS01; kept off range scheduling (`VSPHERE_EXCLUDE_HOSTS`) | 55,000 | 27/28 |
| 3 | Dell PowerEdge R7725: AI node | 1 | 2× EPYC 9535, 1–2 TB RAM, **2× NVIDIA H200 NVL (144 GB)**, ~14 TB NVMe | vLLM + LiteLLM endpoint for ARC² and the AI features (`docs/ai-fleet-sizing.md`). Cheaper alternative: 2× L40S / RTX 6000-class with smaller models | 180,000 | 27/28 |
| 4 | NetApp AFF (A-series or C-series), all-flash | 1 HA pair | ~100 TB effective, NFS/iSCSI, ONTAP One, 3-yr support | Shared datastores, golden templates, fast snapshot reset, OpenSearch/telemetry, MinIO evidence | 220,000 | 27/28 |
| 5 | Backup server | 1 | Dell R760xd2-class, ~150 TB raw HDD | Target for TrueNorth backups (`scripts/backup/`: PostgreSQL, MinIO, OpenSearch snapshots, Moodle, escrow) and NetApp vault | 45,000 | 27/28 |
| 6 | Dell S5248F-ON (or equivalent) | 2 | 48× 25 GbE, 100 GbE uplinks, VLT pair, **RSPAN support** | Compute/storage fabric; trunk VLANs 100–199 to every host; MTU 9000 | 50,000 | 27/28 |
| 7 | 1 GbE out-of-band switch | 1 | 48-port managed | iDRAC, NetApp management, PDU management | 8,000 | 27/28 |
| 8 | Optics, DACs, cabling | lot | 25G/100G | | 7,000 | 27/28 |
| 9 | Palo Alto PA-3430 (or PA-1400-series to save cost) | 1 | NGFW + 3-yr Threat Prevention / URL subscriptions | Site perimeter, admin VPN, zone separation (MGMT / BUILD / SERVICES / AI / STORAGE / WAN). Range-internal firewalls stay virtual (pfSense / OPNsense / VyOS) | 95,000 | 27/28 |
| 10 | Rack, UPS, PDUs, KVM | 1–2 racks | 42U, ~10 kVA online UPS, metered PDUs | Optional: shock-mounted 15U cases for a deployable variant (follow-on) | 40,000 | 27/28 |
| 11 | Classroom / Student endpoints | 17 | Laptops or thin clients with a browser (16 Students + 1 instructor) | Access to TrueNorth and range consoles. **Drop this line** if Students use existing Unclassified workstations | 50,000 | 28/29 |
| 12 | Spares | lot | Drives, DIMMs, optics, PSUs | | 30,000 | 28/29 |
| 13 | Range host #4 (or RAM upgrade) | 1 | as item 1 | Capacity growth: two concurrent classes or a large-enterprise range | 90,000 | 29/30 |

## 3. Secret site: result-authority installation

The Secret side is the authority for identity and grades. It receives sealed results (ADR 0015), maps
the opaque subject to a person and keeps the official record. **It runs no ranges**, so it is small.

| # | Item | Qty | Spec | ROM (CAD) | FY |
|---:|---|---:|---|---:|---|
| 14 | Server (or VMs on existing Secret infrastructure, if the host authority offers them, which is cheapest) | 2 | 1× EPYC 32c, 256 GB, local NVMe RAID; Ubuntu 24.04 + Docker, or 2 VMs | 100,000 | 28/29 |
| 15 | Low-to-high transfer: controlled-media kit | 1 | Write-once / encrypted media, an Unclassified-side scanning kiosk, Secret-side import workstation. A **data diode is not included**: if one is needed, it is an ADM(IM) cross-domain dependency (typical cost $100K+) | 25,000 | 28/29 |

## 4. Key custody (both enclaves)

| # | Item | Qty | Notes | ROM (CAD) | FY |
|---:|---|---:|---|---:|---|
| 16 | HSMs: YubiHSM 2 FIPS (two per enclave), or one network HSM per enclave if the security authority requires it | 4 | Result-signing, content-signing, release and audit keys are kept separate; the Secret ingestion private key never leaves Secret (8.5 plan §Epic 4). A network HSM such as Thales Luna costs ~$30–50K each | 25,000 | 28/29 |

---

## 5. Software

### 5.1 TrueNorth platform (no licence cost apart from the TrueNorth Crown licence)

These run as containers on TN-MGMT01, pinned by digest in `compose.prod.yml` and installed by the
Ansible installer (`install/`).

| Component | Version in repo | Purpose |
|---|---|---|
| TrueNorth api / worker (provision, scenario, telemetry, beat) / web / ai-orchestrator | release images, cosign-signed with SBOM | The platform |
| PostgreSQL + PgBouncer | 16 / 1.25 | Platform database |
| Redis | 7 | Celery broker / cache |
| MinIO | 2026-08 | Evidence and object storage |
| OpenSearch + Dashboards | 2.13 | Telemetry, detections, search |
| Keycloak | 24 | Sign-in; federates to AD (Unclassified: pseudonymous accounts) |
| SQL LRS (yetanalytics lrsql) | 0.9.9 | xAPI Learning Record Store |
| Moodle | 5.2.3 | Per-tenant LMS (LTI 1.3) |
| nginx | 1.30 | Front door (`:4200`) |
| Prometheus, Alertmanager, Grafana, exporters | pinned | Monitoring and alerting |
| Ubuntu Server 24.04 LTS, Docker Engine + Compose | — | Host OS for TN-MGMT01, the depot, the build host, the AI node and Secret |
| Ansible, Packer, govc | — | Install and template builds |
| Nexus OSS, Chocolatey (internal feed), apt mirror | — | TN-DEPOT01 offline depot |

**Optional paid:** Ubuntu Pro (FIPS 140 modules, Livepatch, extended security maintenance) for about
8 hosts, if the security authority requires FIPS. ROM $10K/yr.

### 5.2 Licensed infrastructure software

| # | Product | Scope | Notes | ROM (CAD) | FY |
|---:|---|---|---|---:|---|
| 17 | **VMware vSphere Foundation** (or VCF), with vCenter | Every core on the range and management hosts (Broadcom per-core subscription; minimums apply) | Needed for the vDS, port mirroring, instant clone, DRS and Native Key Provider (Windows 11 vTPM). Fewer, faster cores reduce the subscription. **Check for an existing DND/SSC agreement first** | 95,000/yr × 3 = 285,000 | all |
| 18 | **Microsoft** (reserve, if not covered by DND's enterprise agreement) | Windows Server Datacenter per core on the range hosts (unlimited Windows VMs); KMS host; Windows 10/11 Enterprise for client VMs; Office LTSC 2024; SQL Server 2022; Exchange 2019/SE; SharePoint 2019/SE; MECM | The build sheet assumes these are already held and activated through TN-KMS01 in prod. Test builds use evaluation media | 80,000 reserve | 28/29 |
| 19 | NetApp ONTAP One, Dell ProSupport, Palo Alto subscriptions | | Included in hardware items 1–10 for 3 years | incl. | — |

### 5.3 Range and courseware tools (licensed)

Everything else in `vm-build-sheet.md` §5 is open source: Kali, Security Onion, Zeek, Suricata,
Velociraptor, MISP, Greenbone, Sliver/Mythic, GHOSTS, OpenPLC, LocalStack and others.

| # | Product | Qty | Why | ROM (CAD) | FY |
|---:|---|---:|---|---:|---|
| 20 | Burp Suite Professional | ~10 | Web-application courses (CE is in Kali, but limited) | | |
| 21 | Cobalt Strike (export-controlled; prod only, restricted deploy) | 2 | `c2-server-cs` red-team role; Sliver/Mythic remain the default | | |
| 22 | Npcap OEM | site | Silent Npcap install in Windows templates (free Npcap cannot install unattended) | | |
| 23 | KAPE (commercial use), FTK Imager (registration) | site | DFIR courses | | |
| | **Subtotal, items 20–23** | | Year 1 purchase in FY 28/29, renewal in FY 29/30 | 45,000 + 40,000 | 28/29, 29/30 |
| 24 | TheHive / StrangeBee licence | if > community limits | SOC-training range | within the above | |

### 5.4 Golden image media (from `vm-build-sheet.md` §2)

Windows 10 22H2 / 11 24H2 / 10 LTSC 2021 Enterprise, Windows Server 2016/2019/2022, Windows 7 SP1
(isolated ranges only), Ubuntu 24.04, Rocky 9, Kali, REMnux, SIFT, Security Onion 2.4, pfSense CE 2.7,
VyOS 1.4. These are about 20 templates, built once and cloned many times. Microsoft media is
licensed under item 18.

---

## 6. Budget fit ($1.8M hardware/software envelope, ROM CAD)

| FY | Items | ROM | PAD line |
|---|---|---:|---:|
| 2027/28 | 3 range hosts, management node, AI node, NetApp, backup server, switches, OOB switch, optics, PA-3430, rack/UPS (items 1–10) **+ vSphere yr 1 + misc software ($35K)** | 1,100,000 | 1,100,000 |
| 2028/29 | Secret installation, transfer kit, HSMs, endpoints, spares, tool licences, Microsoft reserve, vSphere yr 2 (items 11, 12, 14–18, 20–23) | 450,000 | 450,000 |
| 2029/30 | Range host #4, tool renewals, vSphere yr 3, support extensions/spares ($25K) | 250,000 | 250,000 |
| **Total** | | **1,800,000** | **1,800,000** |

**If quotes come in high, cut in this order:**
1. Classroom endpoints (−$50K) if existing workstations can be used.
2. The AI node: swap to L40S-class GPUs (about −$80–100K), or defer it to FY 28/29.
3. PA-3430 → PA-1400-series (about −$40K).
4. Range host #4 (−$90K). This leaves capacity for one class at a time.
5. The Microsoft reserve (−$80K), if the DND agreement covers it.

**What is not in this envelope:**
- a data diode
- the physical ICS/automotive kits (GRIMM)
- the deployable transport cases
- Top Secret / Special Access enclaves
- in-service support after FY 29/30: vSphere renewals, tool licences and hardware maintenance, about $150–200K/yr ROM. This feeds the "$[TBC] recurring O&M" line in the PAD.

---

## 7. Site prerequisites (owned by the host site or ADM(IM), not purchased)

- Secure server room: about 2 racks, about 10–12 kW, cooling, physical security accredited for Unclassified (and separately for Secret).
- An Unclassified network drop for Student access, and a controlled internet path for TN-BUILD01 / TN-DEPOT01 updates only (or offline update bundles).
- DNS names and certificates for the platform (`TN_PUBLIC_URL`), AD/LDAPS for Keycloak federation, and NTP.
- IP plan: the platform subnets must not overlap range CIDRs (`TN_RANGE_RESERVED_CIDRS`). Reserve VLANs 100–199 for ranges.
- Security assessment and authorization (ITSG-33) for each enclave, and approval of the low-to-high transfer procedure.

## 8. Open questions to close before quoting

1. Do DND/SSC enterprise agreements already cover VMware and Microsoft? This could release about $365K.
2. Should the Secret side get its own servers, or VMs on existing Secret infrastructure?
3. Controlled media or a data diode for low-to-high transfer (and, if a diode, who funds it)?
4. Is FIPS-validated crypto required? This affects Ubuntu Pro and the HSM choice.
5. Will Students use existing workstations or dedicated classroom endpoints?
6. Is a deployable variant (15U cases) needed in this project, or as a follow-on?
