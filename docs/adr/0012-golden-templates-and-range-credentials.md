# ADR 0012: Golden templates are prebuilt, host-local and key-free; ranges declare their credentials

- **Status:** accepted 2026-10-19 (owner sign-off). Proposed the same day from a design review of the work-vCenter template build.
- **Date:** 2026-10-19
- **Amended by [ADR 0013](0013-range-certification-credentials-template-pinning.md) (2026-10-21):** §5 (the generic password exists only during template construction), §12–§14 (no generic fallback; unique sealed per-range credentials; audited reveal; no credentials or tokens in wiki pages; Student rotation on reset/rebuild, which ends the 2026-10-19 deferral) and §6 (clone a pinned promoted build id and checksum on every host; no newest-copy selection, no plain-name fallback). Read 0013 for those decisions; the text below is kept as it was decided.
- **Numbering:** 0008 is left free for an open branch (see ADR 0010). 0007 is Greyspace.
- **Code:**
  - host-local copies, prune task, catalogue, Packer CI: #182;
  - range credentials, sealed and role-filtered, with staff-only wiki pages: #183;
  - template-default logins: #185;
  - Security Onion per-range setup: #184;
  - the work-site build kit: #186;
  - Packer depot paths, VS media, generic Linux login and role sources: #187;
  - Exchange 2019, SharePoint SE and MECM setup at deploy: branch `claude/role-images-deploy-setup`;
  - ranges reach TN-DEPOT01 on a third NIC on the range uplink network, 10.30.32.10, tcp/8081 and 3142 only (owner decision 2026-10-20): #214.
- **Exception to decision 1 (closed):** the Exchange 2019, SharePoint SE and MECM role images were built with their catalogue rows disabled until TrueNorth could run their domain-dependent setup at deploy time (#187). That setup now exists (`worker/provisioners/vsphere_products.py`, `vmware-site-runbook.md` §7.3): after the range's DC, the VM joins the domain and the product is set up **detached through guest operations as the range's domain Administrator** — no stored Windows credential; secrets (the domain password, the SharePoint key) travel only by guest file transfer and are deleted after use. The build leaves the setup `running` and a beat task (`worker.setup_reconcile.reconcile_setups`) finishes it, so a 1–2 h install never hits the Celery soft limit. A range with these roles gets a per-range generated domain Administrator when it declares none, not the site-wide default (cross-range isolation). SharePoint SE runs only with the site's product key (`TN_SHAREPOINT_PRODUCT_KEY`), else it stays staged. The rows are enabled, so decision 1 applies to them like any other template (an unbuilt image falls back to the plain OS). Hardened per adversarial review (mblanke/TrueNorth#193).

## Context

The first real range build on the work vCenter (2026-10-15, "Medium Enterprise Network") stopped before cloning anything: `template 'srv2022' is neither a Content Library item nor an inventory VM template`. The vCenter had no golden templates.

What there was:
- three hand-made Linux items in the `TrueNorth-Templates` Content Library, of unknown provenance;
- a Packer tree (`infra/vsphere/packer/`) that had never run against this vCenter.

The site constrains everything below:
- **No shared storage.** Four ESXi hosts, each with only its own local VMFS datastore. Placement (`VSPHERE_PLACEMENT=spread`) puts each VM on its host's local datastore. So cloning from a template on another host is a full copy over the network, 20–40 GB per Windows VM.
- **Ranges land on esx02–04.** esx01 is excluded from ranges; it holds the ISOs.
- **No route out from the build network.** It must be isolated from the management network. `dPG-TN-BUILD`, VLAN 31, was created for this: no gateway, DHCP from TN-MGMT01 on `10.30.31.0/24`.
- **Licensing.** Windows and server-product media for test comes from the owner's Visual Studio subscription. Production will have a KMS server.
- **Credentials are documented.** Every range setup is to be documented in the TrueNorth wiki, credentials included, and readers' roles differ.

## Decision

### Templates

1. **Every enabled catalogue template is built ahead of time.** That covers the base OS images, appliances, derived images, Windows role images and variants, so a range deployment only ever clones. `c2-server` stays disabled; it is built later, on its own branch, with explicit approval.
2. **Every template is built by Packer from the repo.** The hand-made library items are rebuilt and retired once their replacements pass. A template nobody can reproduce can't be trusted.
3. **Templates carry no product keys.** They use VS media marked "No key required" or the public KMS client (GVLK) keys. Evaluation editions are not used, because they expire and evaluation Servers then shut down. Test ranges run unactivated: fully functional, with a watermark.
   - Production activation is a **Windows activation** setting in the admin portal, alongside the hypervisor connections: a KMS host and port (1688), optionally a MAK key, stored sealed. It is applied at deploy (`slmgr /skms`, `/ato`), with a warning when a VM doesn't activate, never baked into a template.
   - That setting is **built when the production KMS server is set up**, so it is tested against a real KMS server.
4. **Builds have no internet.**
   - A mirror VM, **TN-DEPOT01** (apt cache plus a Chocolatey feed, on the management and build networks), is built first and is the only machine that reaches out.
   - Templates are as current as their ISOs. They are **rebuilt quarterly** from refreshed VS ISOs, which Microsoft updates monthly. A single update file from the Update Catalog can be added to a build for an urgent fix.
   - **No WSUS.** Windows updates are cumulative, so fresh ISOs give the same result without a server to run. WSUS also must not sit on the domain controller.
   - Patch level per scenario (deliberately vulnerable states) belongs to range content, not to templates.
5. **Templates use a generic build password, documented in the wiki.** Admins may change it after the build. Deployed VMs don't keep it: see "Range credentials".
6. **A copy of each template lives on every range host.**
   - After each build, the template is cloned to esx02, esx03 and esx04 as `<id>@<host>-<yyyymmdd>`.
   - The provisioner clones the newest copy on the VM's target host, so the clone stays on that host's disk. It falls back to the plain name.
   - Old copies are removed once no range uses them. Storage cost is about 3× the template set; each host has about 1.5 TB free.
   - Shared storage (NFS, iSCSI or vSAN) is the long-term fix and belongs in the production hardware plan.
7. **The build network stays isolated.**
   - Builds run on esx04, next to TN-MGMT01, which serves the Packer HTTP, WinRM and SSH callbacks on `10.30.31.20`.
   - VLAN 31 is to be trunked to every host. Until then, a DRS "should run on esx04" rule keeps TN-MGMT01 there.
8. **Versions:**
   - VyOS uses the free rolling build `2026.10.01`, pinned in the catalogue, for both the `vyos` and `vyos-1.4` aliases. If it proves unstable, switch to the free VyOS Stream. No subscription.
   - The Security Onion template is OS-only. Its `so-setup` (standalone node, monitoring on the range's mirror network) runs per range at deploy, scripted.
9. **A template counts as built when:**
   - a test clone takes TrueNorth's customization: the Windows Sysprep name, IP and range password, or Linux cloud-init;
   - it is marked `built` in the golden-image list;
   - the test clone is deleted.
10. **The build-out is accepted when "Medium Enterprise Network" and "SOC Analyst" build end to end** on the work vCenter, with Security Onion seeing the mirrored traffic. The forensics range (C208) is a separate project.
11. **Builds are approved once per phase:** depot, offline bases, appliances, depot-dependent images, role images and variants. There is a heads-up before each fragile appliance (pfSense, VyOS, Security Onion), whose installers type keystrokes blind.

### Range credentials

12. **A range template declares its accounts:**
    - the Administrator or install user, plus scenario users per machine;
    - each with a password (or "generate");
    - each with whether it is handed to Students.

    TrueNorth sets exactly those at deploy, replacing today's random per-VM Sysprep password, and falls back to the generic default. Credentials are stored sealed, like hypervisor passwords.
13. **Who sees credentials:**

    | Role | Sees |
    |---|---|
    | admin, instructor, range_ops | every account |
    | Student | only the accounts for machines their exercise uses |
    | observer | none |
14. **Range documentation is generated.** Each range gets a wiki page built from its range template (machines, networks, template versions, credentials) in a staff-only space. It is regenerated when the range or a template changes, and keeps a hand-written "Notes" section. A wiki page can't be filtered per reader, so Students see their accounts on their own lab view instead.

## Consequences

- **Faster deploys.** Range deploys are clones only, and local ones. A range no longer waits for a build or a cross-host disk copy, except when a host's copy is missing.
- **Cost of the template set.** About 30 templates × 3 hosts of storage, and a quarterly rebuild: roughly a day of supervised builds per quarter.
- **Activation isn't proven until production.** Test ranges are unactivated, and activation is untested until the KMS server exists.
- **Credentials can be documented truthfully.** Passwords are intentional and visible to the right roles. A random password nobody can tell a Student is gone. Range templates now carry secrets, so their storage and API are tenant-scoped and role-filtered (ADR 0005 and the S-track controls apply).
- **Security Onion becomes deploy-time work.** It moves from "a VM that boots" to "a configured sensor", which the SOC range needs to mean anything.
- **Isolation at a cost.** No internet in builds keeps the template supply chain auditable: everything comes from the ISO, the depot or the repo. In exchange, TN-DEPOT01 becomes a required piece of lab infrastructure.

## Follow-up (2026-10-20): one vCenter folder tree and tn-* tags

Not a change to the decisions above; it records where their objects live in vCenter.

- **Folders.** Everything TrueNorth creates is under one `TrueNorth` VM folder:
  `TrueNorth/Templates/` (golden templates and their host copies, flat, as decision 6 names
  them), `TrueNorth/Templates/_retired/` (old TrueNorth templates waiting for decision 2's
  retirement, tagged `retired`; moved, never deleted by tooling, never a clone source) and
  `TrueNorth/Ranges/<range-name>-<key>/`, `<key>` the first 12 hex digits of the range id
  (the provisioner's `VSPHERE_RANGE_FOLDER`, previously `truenorth/ranges/<id8>`; 8 digits
  collide across ranges too soon). Range folders are found by the key suffix, never by
  name; an earlier `<id8>` folder is still found but never reused for a new build. An empty
  or `/` `VSPHERE_RANGE_FOLDER` is refused. Templates and copies are found by name anywhere
  but `_retired`, so folder moves are safe.
- **Tags.** Single-cardinality categories on VMs: `tn-type` (golden-template, template-copy,
  range-vm, test-clone, depot, mgmt, retired), `tn-os`, `tn-template`, `tn-range`. The
  template kit tags at build, replicate and test clone; the provisioner tags range VMs at
  clone and deletes the range's `tn-range` tag on destroy. Best effort and bounded: a 5 s
  timeout per call, the first failure stops tagging for that build, none in the budget's
  last two minutes; tagging never fails a build or a range. One definition in
  `worker/provisioners/vsphere_tags.py`, mirrored in `scripts/lab/vcenter_organize.py`
  (contract-tested).
- **Existing objects.** `scripts/lab/vcenter-organize.sh` moves TrueNorth's own objects out
  of the earlier `truenorth/...` tree into this layout and tags them, idempotently. It
  reports and leaves anything it does not recognise as TrueNorth's, never retires a
  powered-on VM, deletes an old folder only when this run emptied it and vCenter reports it
  empty, and refuses unless every range is settled and the running worker builds into
  `TrueNorth/Ranges`. The Infrastructure page lists a connection's VMs grouped by `tn-type`,
  a range VM only to its own tenant or the platform administrator.
