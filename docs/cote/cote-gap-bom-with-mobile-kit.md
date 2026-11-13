# COTE: what is still missing, given the mobile kit already owned

**Status:** draft, 2026-11-13. **Budget superseded** by `cote-warehouse-kit-gap-analysis.md` (actual kit quotes); the diode requirements in §3 still apply. **Supersedes** the hardware purchase plan in
`cote-hardware-software-bom.md`. That document's sizing, software list and site prerequisites
still apply. **Budget frame:** the minor-project PAD: $1.8M hardware/software over FY 27/28–29/30
($1.1M / $450K / $250K), plus $400K/yr professional services.

**All dollar figures are rough order-of-magnitude (ROM) CAD, for budgeting only. None comes from a
quote.**

## 1. Already owned: the mobile kit (acquired last year)

From `docs/dell-netapp-mobile-kit-inventory.md`. Confirm quantities and licences against the
asset record. **The number of R6625 hosts and the licences bought with the kit are the two
unknowns that move this budget most.**

| Item | Covers |
|---|---|
| Dell R7725, 2× NVIDIA H200 NVL | AI node for ARC² and the AI features |
| Dell R6625 (control-plane and hypervisor variants), qty [confirm] | Management plane and range hosts |
| 2× NetApp (~300 TB each) | Datastores, snapshot reset, telemetry, backup/replica (the second array replaces the separate backup server) |
| Palo Alto PA-3430 | Perimeter, VPN, zone separation |
| Dell S5248F-ON | 25/100 GbE fabric |
| Shock-mounted 15U cases, UPS/PDU | Deployable and fixed operation |

**Sizing check:** one 15-person class (15 individual labs plus one collective range) needs about
420 vCPU / 920 GB of range RAM, plus about 260 GB for the management plane
(`cote-hardware-software-bom.md` §1). That means **at least three 1 TB range hosts plus a management
node**, with N+1. If the kit has fewer, line G4 below tops it up.

## 2. What is missing

| # | Gap | Why COTE needs it | Owned? |
|---:|---|---|---|
| G1 | **Low-to-high data diode** (cross-domain solution) | Moves sealed result envelopes from Unclassified to the Secret grade authority (ADR 0015, Epic 4) without any network path back | **No: main gap** |
| G2 | **High-to-low release path** | A diode is one-way. Two flows go the other way: Secret's signed **receipts** (accepted/rejected, no grades) and Secret-issued **opaque subject credentials** for Unclassified accounts. Both need a reviewed downgrade: controlled media, or a separate approved high-to-low guard | No |
| G3 | **Secret result-authority installation** | The kit is one enclave. Secret needs its own TrueNorth install (platform only, no ranges): subject registry, imported-results ledger, Keycloak federated to the Secret directory | No (unless VMs are available on existing Secret infrastructure) |
| G4 | Range-host top-up | Only if the kit has fewer than 3× 1 TB range hosts plus a management node | Confirm |
| G5 | **HSMs** in both enclaves | Hardware key custody for result-signing, Secret ingestion, release and audit keys: an open release blocker in ADR 0015 | No |
| G6 | **vSphere licensing** (Foundation/Enterprise Plus class) | vDS, port mirroring, instant clone, Native Key Provider. The kit inventory lists the hypervisor as "VMware/vSphere, Proxmox or other", so check whether licences were bought | Confirm |
| G7 | **Microsoft licensing** | Windows Server Datacenter per core on range hosts, Windows client, Office LTSC, SQL, Exchange, SharePoint, KMS | Confirm (DND enterprise agreement?) |
| G8 | Support renewals for the kit | Dell ProSupport, NetApp support, Palo Alto subscriptions expire on the original purchase term | Likely needed from FY 27/28 |
| G9 | Range tool licences | Burp Suite Pro, Cobalt Strike (restricted), Npcap OEM, KAPE, TheHive above community limits | No |
| G10 | Student endpoints | Only if Students cannot use existing Unclassified workstations | Confirm |
| G11 | Physical cyber-physical kits | GRIMM TinyTown (ICS) and a car-hacking bench: HLMR 3 fidelity. The kit frees budget to bring them into this project instead of a follow-on | No |

## 3. The data diode (G1) in detail

### What TrueNorth needs from it

TrueNorth's transfer contract is transport-neutral and file-based (ADR 0015), so any file-transfer
diode fits. Requirements to put into the statement of work:

| Requirement | TrueNorth fact | Diode specification |
|---|---|---|
| Payload | One canonical file per package, **≤ 1 MiB**, created without overwrite | File-transfer mode (no streaming protocol needed). Throughput is trivial: thousands of results a day is well under 1 GB |
| Delivery semantics | Import is idempotent by package ID + digest; duplicates are harmless; corrections supersede | At-least-once delivery is acceptable. The diode must deliver **atomically** (no partially written files visible to the importer) or write to a staging directory and then rename |
| Integrity | Packages are Ed25519-signed and AES-256-GCM encrypted to the Secret recipient key | The diode does not need to inspect or decrypt content. Optional diode-side checksums/forward error correction are welcome |
| Content checks | The high side quarantines: schema, signature, replay and malware checks (Epic 4) | The receive-side proxy drops the file into the TrueNorth import quarantine directory; a content-filter/AV hook on the receive proxy is optional |
| No back channel | No acknowledgements may flow low-ward | True hardware one-way (optical). Lost files are retried from the Unclassified outbox; status reaches Unclassified only through the G2 receipt path |
| Health and audit | Admins need to see pending, exported, received and rejected packages | Send/receive proxy logs and health exported to syslog (Prometheus on each side) |
| Accreditation | Cross-domain solutions need their own approval | A product with a recognized evaluation (Common Criteria; Canadian/allied cross-domain approval). Confirm the acceptable list with the DND cross-domain authority (ADM(IM)) **before** choosing a product |

### Architecture

```
UNCLASSIFIED (mobile kit)                                SECRET
TrueNorth outbox --> send proxy --> [ DATA DIODE ] --> receive proxy --> TrueNorth import quarantine
   (signed, sealed                    one-way, optical               --> signature/replay/malware checks
    files <= 1 MiB)                                                   --> subject mapping, official record
                                                                         |
 receipt inbox  <---- reviewed controlled media (G2: receipts, subject credentials) <---- receipts / credentials
```

### Candidate product families (market research only, not endorsements)

Hardware one-way file-transfer diodes with send/receive proxies from established cross-domain
vendors, for example Owl Cyber Defense, Fox Crypto (Fox DataDiode), Advenica and Waterfall
Security. Ask each vendor for:
- Canadian/allied accreditation status
- the file-transfer proxy feature set (atomic delivery, staging, AV hook)
- rack form factor: it must fit the 15U cases if the deployable configuration needs Secret transfer in the field
- support terms

### ROM

| Element | ROM (CAD) |
|---|---:|
| Diode pair hardware, send/receive proxy servers, file-transfer software licence | 120,000 |
| Integration with the TrueNorth outbox and import (professional services already funded) | — |
| Vendor installation, test support and accreditation evidence | 50,000 |
| Second (HA) diode path: optional, see §5 | (+100,000) |
| Annual maintenance (FY 28/29 onward) | 25,000/yr |

## 4. Revised budget ($1.8M hardware/software, ROM CAD)

| # | Item | FY 27/28 | FY 28/29 | FY 29/30 | Total |
|---:|---|---:|---:|---:|---:|
| G1 | Data diode: hardware, proxies, software, install/accreditation support | 170,000 | 25,000 | 25,000 | 220,000 |
| G2 | High-to-low controlled-media kit (encrypted/write-once media, scanning kiosk, release workstation) | 25,000 | — | — | 25,000 |
| G3 | Secret result-authority installation (2 servers, or a VM allocation) | 100,000 | — | — | 100,000 |
| G4 | Range-host top-up: 2× R6625, 1 TB (**only if** the kit is short) | 180,000 | — | — | 180,000 |
| G5 | HSMs: two per enclave | 25,000 | — | — | 25,000 |
| G6 | vSphere subscription (if not held) | 95,000 | 95,000 | 95,000 | 285,000 |
| G7 | Microsoft licensing reserve (if not under the DND agreement) | 80,000 | — | — | 80,000 |
| G8 | Kit support renewals (Dell, NetApp, Palo Alto) | 120,000 | 120,000 | 95,000 | 335,000 |
| G9 | Range tool licences (initial, then renewals) | 45,000 | 40,000 | 35,000 | 120,000 |
| G10 | Student endpoints (if required) | 50,000 | — | — | 50,000 |
| G11 | Physical kits: TinyTown/ICS (FY 27/28), car-hacking bench (FY 28/29) | 150,000 | 100,000 | — | 250,000 |
| — | Spares, optics, Ubuntu Pro, HSM/media consumables | 60,000 | 70,000 | — | 130,000 |
| | **Total** | **1,100,000** | **450,000** | **250,000** | **1,800,000** |

**If the kit already includes the vSphere and Microsoft licences and enough hosts (G4, G6, G7),
about $545K is released.** In priority order:
1. A second diode path for availability (+$100K).
2. A network HSM in Secret instead of a YubiHSM (+$40K).
3. More GRIMM kits (LITE/INCITE/KITE/CyberHive) or a second TinyTown for concurrent teams.
4. Content production (SME time is the binding constraint on IOC).

## 5. Actions before quoting

1. **Pull the asset record for last year's kit:** R6625 count and memory, and which licences and
   support terms were bought (VMware, Microsoft, Palo Alto subscriptions, NetApp, Dell ProSupport).
2. **Ask the DND cross-domain authority (ADM(IM))** which diode products are acceptable, and whether
   an existing approved cross-domain service could carry the files instead of a project-owned diode.
3. Confirm with the security authority that **controlled media is acceptable for the high-to-low
   receipts and credentials** (G2), or whether a high-to-low guard is required.
4. **Decide where the Secret installation lives:** project servers, or VMs on existing Secret
   infrastructure.
5. Ask GRIMM for a quote and interface data for TinyTown and a car-hacking bench (8.5 plan, Epic 3A).
