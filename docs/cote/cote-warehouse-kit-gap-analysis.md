# COTE: the warehouse mobile kit against TrueNorth, and what is still missing

**Status:** draft, 2026-11-13. **Role layout and budget superseded** by `cote-preprod-prod-plan.md` (kit is pre-production; production on DND hosting); the inventory and warehouse checks still apply. **Supersedes** the budget tables in `cote-gap-bom-with-mobile-kit.md`
and `cote-hardware-software-bom.md`. Their software lists, sizing method and diode requirements
still apply.

**Sources:**
- Dell solution 20939737.23, "DND - DSG - Mobile" (V23, 17 Feb 2026)
- NetApp quote 10070747-007, "DND - DSG Mobile Kits" (1 Dec 2025)

The user reports that both are delivered and in the warehouse. **Check the received serials
against these quotes:** the inventory below is what was *quoted*.

ROM figures are rough order-of-magnitude CAD for budgeting, not quotes.

---

## 1. What is in the warehouse

| Item | Qty | Key spec (from the quotes) | Support included |
|---|---:|---|---|
| R6625 **control plane** | 1 | 2× EPYC 9754 (**256 cores**), **512 GB**, 1× 7.68 TB NVMe SED/FIPS, BOSS 2× 960 GB, 4× 25 GbE, iDRAC9 Enterprise, TPM 2.0 | ProSupport Plus 4-hr MC, 60 mo |
| R6625 **hypervisor plane** | 1 | 2× EPYC 9654 (192c), **1 TB**, 1× 15.36 TB NVMe SED/FIPS, 4× 25 GbE | 60 mo |
| R6625 **compute nodes** | **4** | 2× EPYC 9654 (192c), **1.5 TB**, 2× 15.36 TB NVMe SED/FIPS, 4× 25 GbE | 60 mo |
| R7725 **GPU node** | 1 | 2× EPYC 9535 (128c), **2 TB**, **2× NVIDIA H200 NVL 141 GB**, 1× 15.36 TB, 4× 25 GbE + 4× 1 GbE | 60 mo |
| NetApp **AFF A50 HA** | 2 | 48× 15.3 TB NVMe SED (**~734 TB raw** in total), 100 GbE + 25 GbE I/O, ONTAP One (SnapMirror, data-at-rest encryption) | SupportEdge Advisor + 4-hr parts + non-returnable drives, **5 yr** |
| Dell S5248F-ON | 2 | 48× 25 GbE, 6× 100 GbE, **Enterprise SONiC** (5-yr subscription) | 5-yr ProSupport Plus MC |
| Dell E3248PXE-ON | 1 | 48× 1/10 GbE PoE, SONiC Lite: OOB/management | 60 mo |
| APC Smart-UPS Ultra 10 kVA Li-ion | 2 | 4U each | — |
| Palo Alto Core Security Bundle + Premium Support | 1 | Adv. Threat Prevention, Adv. URL, Adv. WildFire, DNS Security, 5-yr support. **The firewall appliance itself is not itemized in the Dell quote: confirm the model (PA-3430?)** | 5 yr |
| TuffTek shock-mounted 19" cases | 2 | Removable racks | — |

### What this means

- **Compute is not a gap.** Five range-capable hosts give **7 TB of RAM and 960 cores**. One
  15-person class needs about 920 GB of range RAM (`cote-hardware-software-bom.md` §1), so the kit
  carries **4–5 concurrent classes, or 3 with N+1**. A large-enterprise range (375 GB) fits easily.
  The top-up line is dropped.
- **Support is not a gap.** Dell, NetApp, SONiC and Palo Alto support run for **5 years** from
  delivery (to about 2031), which covers the whole FY 27/28–29/30 project. The $335K renewal line is
  dropped.
- **Encryption at rest is largely covered.** Every server and array drive is self-encrypting
  (SED, FIPS), and ONTAP data-at-rest encryption is included. This closes part of Epic 5 (encrypt
  database, object store, search and backup volumes) at the hardware layer. Key management for
  the SEDs still needs a design (local, or external KMIP on the HSM).
- **Storage is generous:** ~734 TB raw over two arrays. One array serves datastores, golden
  templates, telemetry and evidence. The other is a SnapMirror replica and backup target, so no
  separate backup server is needed.

---

## 2. Proposed role layout for TrueNorth

| Host | Role | Licensing impact |
|---|---|---|
| R6625 control plane (256c / 512 GB) | **Bare-metal Ubuntu 24.04 + Docker: the TrueNorth platform** (TN-MGMT01: API, workers, PostgreSQL, OpenSearch, Keycloak, MinIO, LRS, Moodle, monitoring). The installer targets "a host with Docker" | **No vSphere licence.** 256 cores of VMware per-core subscription avoided |
| R6625 hypervisor plane (192c / 1 TB) | ESXi: vCenter (VCSA), TN-DC01, TN-DEPOT01, TN-BUILD01, TN-KMS01, TN-WSUS01. Excluded from range placement (`VSPHERE_EXCLUDE_HOSTS`) | vSphere + Windows Server |
| R6625 compute ×3 (192c / 1.5 TB each) | ESXi range cluster (`vsphere_api` provisioning, VLANs 100–199 on the vDS) | vSphere + Windows Server (Windows range VMs) |
| R6625 compute ×1 | **Moved to the Secret enclave** as the TrueNorth result-authority host (bare-metal Ubuntu + Docker, local 2× 15.36 TB NVMe). It is oversized for the role, but owned. Hardware may move low→high, never back | Secret side: none beyond the OS |
| R7725 (2× H200) | Bare-metal Ubuntu + vLLM/LiteLLM: the AI endpoint for ARC² and the AI features (`docs/ai-fleet-sizing.md`) | None |
| NetApp A50 #1 / #2 | Primary datastores, templates, telemetry, MinIO / SnapMirror replica, backups (`scripts/backup/`) | Included |

Moving one compute node to Secret still leaves 3 × 1.5 TB plus the 1 TB hypervisor-plane host,
**about 3 classes concurrently** on the Unclassified side.

---

## 3. What is missing

| # | Gap | Notes | ROM (CAD) |
|---:|---|---|---:|
| 1 | **Data diode** (low-to-high), with send/receive proxies | Result envelopes are single signed and encrypted files ≤ 1 MiB (ADR 0015). Needs atomic delivery into the import quarantine and no back channel. Choose from products the DND cross-domain authority accepts. Requirements: `cote-gap-bom-with-mobile-kit.md` §3 | 170K + 25K/yr maintenance |
| 2 | **High-to-low path** for receipts and Secret-issued subject credentials | A diode cannot carry these. Reviewed controlled media (or a separate approved guard) | 25K |
| 3 | Secret-side enablers for the repurposed node | Secret network connection by the host site; small UPS, KVM and cabling | 30K |
| 4 | **HSMs**, two per enclave | Result-signing / ingestion / release / audit keys; can also serve as the KMIP key manager for SED and ONTAP encryption | 25K |
| 5 | **VMware vSphere** (Foundation / Enterprise Plus class) | **Not on either quote.** Needed for the vDS, port mirroring, instant clone and Native Key Provider. Licence the **4 ESXi hosts = 768 cores** only (the control plane runs bare metal) | ~145K/yr |
| 6 | **Microsoft** licences | **Not on either quote.** Windows Server Datacenter per core on the hosts that run Windows VMs (use DRS host affinity to limit them), Windows 10/11 Enterprise, Office LTSC 2024, SQL 2022, Exchange, SharePoint, MECM, and KMS | ~335K reserve |
| 7 | Range tool licences | Burp Suite Pro, Cobalt Strike (restricted), Npcap OEM, KAPE, TheHive above community limits | 45K, then ~40K/yr |
| 8 | Physical cyber-physical kits | GRIMM TinyTown/ICS (FY 27/28) and a car-hacking bench (FY 28/29), via the Physical Training Asset Provider (Epic 3). The kit has no physical range assets | 150K + 100K |
| 9 | Rack PDUs, optics, console | **No PDUs on the quote.** The UPS outputs alone may not suit 1800 W / 3200 W C15/C19 server inputs. Confirm the UPS output receptacles and add metered PDUs, extra DACs and a console/KVM | 45K |
| 10 | Student endpoints (only if needed) | Browser access for 16 Students plus an instructor | 50K |
| 11 | Ubuntu Pro (if FIPS crypto or Livepatch is required) | Control plane, depot, build, AI node, Secret host | ~15K/yr |

## 4. Things to verify in the warehouse (no cost, but they block deployment)

1. **Physical fit.** The quoted gear is about 25U: 6× R6625 1U, R7725 2U, 2× A50 2U each, 3 switches,
   2× UPS 4U each and the firewall. **Two TuffTek cases may not hold it all.** Check the case
   size (the solution is named "9U Mobile"). The site may run part of it in a fixed rack.
2. **The firewall appliance:** confirm a Palo Alto chassis was delivered with the security bundle.
3. **Port mirroring on Enterprise SONiC.** TrueNorth's cross-host sensor feed uses vDS RSPAN: the
   physical switch must carry the RSPAN VLAN **without MAC learning or flooding suppression**
   (`docs/deployment/vmware-site-runbook.md`, port-mirroring section). Confirm SONiC supports this.
   If it does not, use `VSPHERE_PLACEMENT=per-range-host`, which the 1.5 TB hosts make easy.
4. **Power:** the servers have 200–240 V PSUs (1800 W R6625, 3200 W R7725). Estimated load is about
   9–11 kW, against 2× 10 kVA UPS (no N+1 at full load). The site needs suitable 208/240 V feeds.
5. **Phone-home:** the R7725 was ordered with *Dell Connectivity Client – Enabled*. Disable it for
   air-gapped operation.
6. **Support start dates:** confirm that the 5-year terms started at delivery, not at quote, and
   when they end.
7. **NetApp port plan:** the quote has 12× 2-port 100 GbE modules, but the two S5248F-ON switches
   have only about 8 usable 100 GbE ports after the VLT interconnect. Plan most NetApp links on the
   4-port 25 GbE modules.

---

## 5. Revised hardware/software budget ($1.8M, ROM CAD)

| Item | FY 27/28 | FY 28/29 | FY 29/30 | Total |
|---|---:|---:|---:|---:|
| 1. Data diode + maintenance | 170,000 | 25,000 | 25,000 | 220,000 |
| 2. High-to-low controlled-media kit | 25,000 | — | — | 25,000 |
| 3. Secret-side enablers (repurposed node) | 30,000 | — | — | 30,000 |
| 4. HSMs (both enclaves) | 25,000 | — | — | 25,000 |
| 5. VMware, 4 ESXi hosts / 768 cores | 145,000 | 145,000 | 145,000 | 435,000 |
| 6. Microsoft licence reserve | 335,000 | — | — | 335,000 |
| 7. Range tool licences | 45,000 | 40,000 | 35,000 | 120,000 |
| 8. GRIMM TinyTown / car-hacking bench | 150,000 | 100,000 | — | 250,000 |
| 9. PDUs, optics, console, spares/consumables | 45,000 | 40,000 | — | 85,000 |
| 10. Student endpoints (if needed) | 50,000 | — | — | 50,000 |
| 11. Ubuntu Pro | — | 15,000 | — | 15,000 |
| Quote-variance reserve | 80,000 | 85,000 | 45,000 | 210,000 |
| **Total** | **1,100,000** | **450,000** | **250,000** | **1,800,000** |

**The biggest lever is licensing.** If DND/SSC enterprise agreements already cover VMware and
Microsoft, **about $770K is released.** The kit's high core counts (1,216 server cores) make
per-core licensing the dominant cost, which is why the control plane and AI node run bare metal. In
priority order, the released money could fund:
1. A second diode path for availability.
2. A network HSM in Secret.
3. More GRIMM kits (LITE/INCITE/KITE/CyberHive).
4. Course content: SME time is the binding constraint on IOC.

## 6. Next steps

1. Inventory the warehouse against §1 and resolve the §4 checks.
2. Ask SSC/ADM(IM) whether DND agreements cover VMware vSphere Foundation and Microsoft per-core
   server licensing for a training enclave.
3. Ask the DND cross-domain authority which diode products it accepts, and whether controlled media
   is acceptable for the high-to-low receipts and credentials.
4. Get the Secret host site's agreement to accept the repurposed compute node.
5. Request a GRIMM quote and interface data (8.5 plan, Epic 3A).
