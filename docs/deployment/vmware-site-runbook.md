# VMware lab — on-site runbook

How to connect TrueNorth to the 4-host vSphere lab and get from "platform installed" to
"TrueNorth builds an isolated range on its own".

The lab itself (ESXi, vCenter, vDS, management VMs) is built and documented by the
deployment repo **`COTE/TrueNorth-Demo`**. Its `state/*.json` files are the authoritative
record of what exists. This runbook starts where that repo stops. Companion documents:

| Document | Use it for |
|---|---|
| [`vm-build-guide.md`](vm-build-guide.md) | Building every golden image (Windows, Linux, pfSense, Security Onion, VyOS…) |
| [`README.md`](README.md) | The read-only discovery script and its outputs |
| [`../../infra/vsphere/packer/README.md`](../../infra/vsphere/packer/README.md) | Packer details |
| [`../vm-build-sheet.md`](../vm-build-sheet.md) | What each template and role contains, and why |

---

## 1. What already exists (from `TrueNorth-Demo/state/`, last updated 2026-08-20)

| Item | State |
|---|---|
| Hosts | 4× Supermicro E300-9D (Xeon D-2146NT **8C/16T**, **512 GB**), ESXi 8.0.3, Enterprise Plus |
| vCenter | VCSA 8.0.3 on **esx01** (`vcsa.truenorth.lab`, 192.168.1.10). DC-Lab / CL-Lab, HA on (it cannot restart VMs: storage is local), DRS manual |
| Storage | **Local only**: `esx01-local` … `esx04-local`, 1.66 TB each. No vSAN, no shared datastore |
| Network | 1G `vSwitch0` = ESXi management on 192.168.1.0/24. 10G **`vDS-10G`** (MTU 9000): `dPG-vMotion` (20), `dPG-TN-MGMT` (30, 10.30.30.0/24, routed), `dPG-VM-40` (40) |
| Range VLANs | **100–199 reserved for TrueNorth** and trunked to every host. None created yet |
| TN-MGMT01 | Ubuntu 24.04, 12 vCPU / 64 GB, esx04, 10.30.30.20, `/srv/truenorth` 500 GB. Docker, Terraform and Packer installed |
| TN-DC01 | Server 2025, esx03, 10.30.30.10. AD `corp.tnrange.lab`, AD CS + LDAPS |
| vCenter accounts | `svc-truenorth` exists, **ReadOnly**. The `TrueNorth-Provision` role is staged but **not assigned** |
| ISOs | `[esx01-local] ISO/` holds 19 files (see the build guide §1) |
| Templates | `tmpl-ubuntu-2404`, `tmpl-ubuntu-2204`, `tmpl-ubuntu-2004`: inventory templates on esx01-local, cloud-init ready |
| Content Library | none |
| TrueNorth platform | **not installed yet** |

Rules carried over from the deployment repo, which still apply:
- **esx01 holds the VCSA.** Never reboot it, put it in maintenance mode or change its
  networking without a human confirming. Keep range VMs off it (`VSPHERE_EXCLUDE_HOSTS`).
- Run `govc` against hosts **by IP**. The control box cannot resolve `*.truenorth.lab`.
  Never run `govc vm.markastemplate` against a vCenter-managed host, because it creates
  ghost inventory objects. Mark templates through vCenter.
- IPMI (10.0.10.101–104) is the human's recovery path. Automation must not touch it.

> ⚠ **Security debt in the deployment repo.** Some committed files under `TrueNorth-Demo/`
> hold plaintext passwords and licence keys (`state/truenorth-resume.json`,
> `state/resume.json`, `state/mgmt-vms.json`, and `.env`, `vsphere.env` and `vmware.env`
> at the repo root). Rotate those credentials, then remove them from the repo and its history.

---

## 2. Before you leave

- [ ] This branch has been merged, or is reachable from the git remote TN-MGMT01 will pull
      from (`install/` `20-fetch-app` pins a ref).
- [ ] Copy `certs/corp-root-ca.cer` from `TrueNorth-Demo` to `install/files/`. Keycloak's
      LDAPS bind fails with a "PKIX path building failed" error without it.
- [ ] Have the `id_ed25519_lab` SSH key with you (it lets you log in to TN-MGMT01 as `tnadmin`).
- [ ] Pick the named AD account that will be `tn_bootstrap_admin_upn`.
- [ ] **Missing ISOs.** Download these where you have good internet, then upload them to
      `[esx01-local] ISO/` on site (build guide §1): Windows 10 Enterprise eval,
      Server 2019 and 2016 eval, pfSense CE 2.7.2, Security Onion 2.4, VyOS 1.4.
- [ ] Optional: a laptop with `govc` and Python 3.11 + `pyvmomi httpx`, if you want to run
      discovery from your laptop and not from TN-MGMT01.

---

## 3. Day 0 — connect and re-verify (read-only)

Nothing on this day changes the lab.

1. **Reachability.** From your laptop, check vCenter `https://192.168.1.10` and SSH to
   `tnadmin@10.30.30.20`.
2. **Discovery.** On TN-MGMT01, or on any box with pyvmomi:
   ```bash
   VSPHERE_PASSWORD='…' python3 scripts/vsphere-discover.py --host 192.168.1.10 --user administrator@vsphere.local --insecure --range-vlans 100-199
   ```
   This writes `docs/deployment/vsphere-inventory.{md,json}`. Read its **Verdicts** section:
   - storage: should say local-only
   - switch mode: should say `vds`
   - VLAN pool: should say `100-199` is free
   - vTPM key provider: decides how Windows 11 is built
   - ISO coverage table
   - readiness gate

   If anything disagrees with §1, stop and find out why before going on.
3. **Range isolation.** In the UniFi console, confirm that **no network or SVI exists for
   VLANs 100–199**. The ranges only need the VLANs to be trunked on the 10G host ports.
   That way, the only exit from a range is a pfSense that TrueNorth deploys inside the range.
   If a gateway exists on any of those VLANs, ranges have egress.

---

## 4. Day 1 — install the platform and grant provisioning

### 4.1 Platform

Follow [`install/README.md`](../../install/README.md) from your control node.

In `install/inventory/group_vars/all/main.yml` (most values are already set for this lab):

| Variable | Lab value |
|---|---|
| `tn_provisioner_backend` | `vsphere_api` |
| `tn_vcenter_host` | `192.168.1.10` |
| `tn_vcenter_user` | `svc-truenorth@vsphere.local` |
| `tn_vsphere_datacenter` / `tn_vsphere_cluster` | `DC-Lab` / `CL-Lab` |
| `tn_vsphere_datastore` | any; with `spread` placement each VM goes on its host's own local datastore |
| `tn_vsphere_range_switch_mode` / `tn_vsphere_range_dvs` | `vds` / `vDS-10G` |
| `tn_vsphere_vlan_pool` | `"100-199"` |
| `tn_vsphere_placement` | `spread` (each VM goes on the host with the most vCPU headroom, on that host's local datastore) |
| `tn_vsphere_exclude_hosts` | `[esx01.truenorth.lab]` (the VCSA host) |
| `tn_vsphere_max_vcpu_per_thread` | `4` (see §8) |
| `tn_vsphere_mgmt_network` | `dPG-TN-MGMT`: the provisioner **refuses** to attach range VMs here |
| `tn_vsphere_network` | `""`: fallback only for a VM with no VLAN. Never set it to a management network |
| `tn_vsphere_content_library` | `""`: clone the inventory templates. Set it once a library exists |

Each `tn_vsphere_*` variable maps to a `VSPHERE_*` environment variable in
`.env.production`. The provisioner creates range port groups as `tn-<range8>-v<vlan>`,
puts VMs in folder `truenorth/ranges/<range8>`, and records the VLANs it uses in the
range's `provisioner_output`.

> Windows range VMs are sysprepped at deploy with a **random local Administrator password
> that is not stored anywhere**. Range accounts must come from the template, a role
> snapshot, or post-deploy Ansible, not from that password.

```bash
cd install
ansible-playbook site.yml --ask-vault-pass --check
ansible-playbook site.yml --ask-vault-pass
ansible-playbook site.yml --ask-vault-pass   # a clean second run proves idempotency
```

### 4.1a Build and depot plane (vm-build-sheet.md §1, §8)

The build sheet decided that **TN-BUILD01** and **TN-DEPOT01** are separate VMs. TN-BUILD01
is the only host with internet; TN-DEPOT01 is the only thing a range can reach. They live
on their own port groups, so that neither a range nor a build VM ever gets a path to
TN-MGMT01 (Keycloak, and OpenSearch with CAF material) or to the internet.

**Proposed VLANs.** Confirm they are free (discovery lists the VLANs in use), then create
them on UniFi and as port groups on `vDS-10G`. `scripts/lab/create-portgroups.sh` creates
the port groups (it refuses if either VLAN is already in use, and sets promiscuous / MAC
changes / forged transmits to reject) and prints the UniFi checklist; UniFi stays manual.

| Port group | VLAN | Subnet | Gateway / DHCP | Allowed | Denied |
|---|---:|---|---|---|---|
| `dPG-TN-BUILD` | 31 | 10.30.31.0/24 | UniFi .1, DHCP .100–.200 (build VMs) | → internet; → vCenter 192.168.1.10:443 and ESXi hosts :443/:902 (Packer uploads, ISO mounts); TN-BUILD01 → 10.30.32.10 tcp/22, 8081, 3142 (Ansible, Nexus API, prefetch); control box → 10.30.31.10 tcp/22 | → 10.30.30.0/24 (mgmt) |
| `dPG-TN-SVC` | 32 | 10.30.32.0/24 | UniFi .1, no DHCP | range pfSense WANs → TN-DEPOT01 tcp/8081, 3142 (same L2) | new connections → internet, mgmt, 192.168.1.0/24, 10.30.31.0/24 |

| VM | Port group | IP | Spec | Built from |
|---|---|---|---|---|
| TN-BUILD01 | `dPG-TN-BUILD` | 10.30.31.10 | 8 vCPU / 16 GB / 200 GB | clone of `tmpl-ubuntu-2404`, then Packer, govc, Ansible, ISO tools (`install/lab/build.yml`) |
| TN-DEPOT01 | `dPG-TN-SVC` (NIC 0) | 10.30.32.10 | 4 vCPU / 16 GB / 100 GB + 1–2 TB at `/srv/depot` | clone of `tmpl-ubuntu-2404`, then Nexus OSS (Chocolatey feed, PyPI, Docker, raw `installers`) and apt-cacher-ng (`install/lab/depot.yml`) |
| | `dPG-TN-BUILD` (NIC 1, **disconnected**) | 10.30.31.11 | | the depot's only path upstream; connected for an install/prefetch window only |

Place them on esx02–04 (the most free RAM), each on that host's own datastore.
`scripts/lab/deploy-mgmt-vm.sh` does that (never esx01) and sets the static IPs, hostname
and the `tnadmin` SSH key through cloud-init `guestinfo`. The full order of operations,
with exact commands, is in [`install/lab/README.md`](../../install/lab/README.md):
port groups → VMs → `build.yml` → uplink on → `depot.yml` → `prefetch.yml` → uplink off →
set the `tn_depot_*` values below.

**Who fetches upstream: the depot.** A Nexus proxy repository and apt-cacher-ng make the
outbound request themselves; TN-BUILD01 having internet does not help them. Since
`dPG-TN-SVC` has no internet, TN-DEPOT01 gets a second NIC on `dPG-TN-BUILD` that is
connected only for a window (`scripts/lab/depot-uplink.sh on|off --apply`; `on` refuses
while range VMs exist unless `--force`). Outside the window two independent controls hold:
the NIC is disconnected in vCenter, and the caches are offline (apt-cacher-ng
`Offlinemode`, Nexus proxies `blocked`; `prefetch.yml` flips both and always flips them
back). Replies from 10.30.32.10 are policy-routed out NIC 0, so the uplink never carries
range traffic. Chocolatey packages are also **uploaded into `chocolatey-hosted`** by
TN-BUILD01 during prefetch, so the offline feed answers from local metadata. A
hosted-only depot (filled by pushes, never online) was rejected because the worker uses
the depot as an apt/dnf **forward proxy**, which only a caching proxy can serve.

**Run Packer from TN-BUILD01, not TN-MGMT01**, even though the deployment repo installed
Packer on TN-MGMT01. TN-BUILD01 drives the depot's prefetch. Range post-deploy installs point
at `10.30.32.10` (8081 Nexus, 3142 apt-cacher-ng) and nowhere else.

> **KMS (TN-KMS01) is paused** and will come with future integration and testing.
> Templates and clones run unactivated until then. See §8 of the build sheet.

#### Range WAN uplink to the depot

By default a range is fully isolated: its firewall gets one NIC per range zone and
nothing else, and no range VM can reach the depot. To give ranges the depot, set the
uplink (in `group_vars/all/main.yml`, or the `.env.production` keys in brackets):

| Variable | Lab value | Meaning |
|---|---|---|
| `tn_vsphere_range_uplink_network` (`VSPHERE_RANGE_UPLINK_NETWORK`) | `dPG-TN-SVC` | Network for the edge firewall's WAN. Empty = isolated (the default). The provisioner **refuses** it if it is listed in `VSPHERE_MGMT_NETWORK` |
| `tn_vsphere_range_uplink_pool` (`VSPHERE_RANGE_UPLINK_POOL`) | `10.30.32.100-10.30.32.199` | Static WAN addresses, one per range. Must not overlap anything else on VLAN 32 |
| `tn_vsphere_range_uplink_gateway` (`VSPHERE_RANGE_UPLINK_GATEWAY`) | `10.30.32.1` | WAN gateway |
| `tn_vsphere_range_uplink_prefix` (`VSPHERE_RANGE_UPLINK_PREFIX`) | `24` | WAN prefix length |
| `tn_depot_url` (`TN_DEPOT_URL`) | `http://10.30.32.10` | The depot as range guests reach it. Empty = no deploy-time installs |
| `tn_depot_choco_feed` (`TN_DEPOT_CHOCO_FEED`) | empty → `http://10.30.32.10:8081/repository/chocolatey/` | Nexus Chocolatey (NuGet) feed |
| `tn_depot_apt_proxy` (`TN_DEPOT_APT_PROXY`) | **`http://10.30.32.10:3142`** | HTTP **forward** proxy for apt/dnf: apt-cacher-ng on the depot. **Set it**: empty falls back to `TN_DEPOT_URL` (port 80), where nothing listens. Guests keep their stock sources; the Rocky template must use the `http://dl.rockylinux.org` `baseurl` (an HTTPS mirrorlist can only be tunnelled, and the depot refuses tunnels) |
| `TN_DEPOT_PORTS` | `8081,3142` | TCP ports of the depot each range pfSense lets the zones reach through its WAN; everything else out of the WAN is blocked |
| `tn_software_install_timeout` (`TN_SOFTWARE_INSTALL_TIMEOUT`) | `1800` | Seconds per VM for all of its installs |
| `tn_vsphere_provision_budget` (`VSPHERE_PROVISION_BUDGET`) | `3300` | No new provisioning work after this many seconds (Celery redelivers after 3600) |

With the uplink set, the range's **edge firewall** gets an extra NIC **first** (NIC 0 =
WAN) on `dPG-TN-SVC`; the zone NICs follow. The edge is a node with `edge: true` (or
`wan: true`) in the topology, else the first firewall (role `firewall`, or a
pfSense/OPNsense image), else the first router. Its WAN address is reserved from the
pool before the build (under the same lock as the VLANs), stored in the range's
`provisioner_output.uplink`, and released when the range is destroyed. `dPG-TN-SVC`
itself is never created or removed by TrueNorth.

**pfSense is configured per range at deploy.** The worker renders a `config.xml` for every
pfSense VM in the range (`control-plane/worker/worker/pfsense_config.py`) and writes it to
the VM's guestinfo (`guestinfo.tn.pfsense.config`, gzip + base64, plus
`guestinfo.tn.pfsense.ifmap` with the NICs' MACs) before the first power-on. The
template's boot script (`/usr/local/sbin/tn-pfsense-config`, an `earlyshellcmd`; build
guide, pfSense) applies it on first boot and reboots once into it. The config holds:
- WAN = NIC 0 (`vmx0`; matched by MAC in the guest), static, at
  `provisioner_output.uplink.ip`/`VSPHERE_RANGE_UPLINK_PREFIX`, gateway
  `VSPHERE_RANGE_UPLINK_GATEWAY`; then one interface per zone at the zone's `.1`. Without
  an uplink, the first zone takes pfSense's WAN slot (no gateway, NAT off).
- Outbound NAT on WAN (automatic), the DNS resolver on the zones, no DHCP.
- Rules: each zone → the firewall's DNS; each zone → `10.30.32.10` (the host of
  `TN_DEPOT_URL`) TCP `TN_DEPOT_PORTS` (default `8081,3142`: Nexus, apt-cacher-ng); the
  template's `network.firewall_rules` (`allow`/`deny`, `ports` as TCP/UDP, `dst: "*"` =
  every range subnet, never the internet; `mirror` is the promiscuous monitoring port
  group's job), or zone ↔ zone when the template has none. Floating rules on WAN: pass out
  to the depot ports, **block out everything else**; inbound on WAN is pfSense's default
  block. Template rules that cannot be translated are skipped and listed in
  `provisioner_output.warnings`.
- `provisioner_output.vms[].pfsense` records the interfaces and the config's sha256;
  `tn-pfsense-config status` in the firewall shell shows what guestinfo holds and what
  was applied (`/conf/tn-pfsense-config.log`).
- **Passwords:** none in guestinfo. Range firewalls keep the template's users, groups,
  web GUI/SSH settings and certificates, so the admin password is the template's.
- The UniFi side of VLAN 32 (§ table above) still denies `dPG-TN-SVC` → internet, mgmt
  and 192.168.1.0/24. Both layers are needed: the UniFi rule protects the platform if a
  student reconfigures pfSense.

OPNsense and VyOS get no generated config yet (`TODO(appliance)` in `vsphere_api.py`); a
range that uses one as its edge must carry the rules above in its template.

#### Per-VM software at deploy time

A Range Designer node's **`services`** field (comma-separated in the designer, a list in
the template) names software to put on that VM. After the VM is powered on, VMware Tools
is running and guest customization has finished, the worker installs it **inside the
guest through VMware guest operations** (vCenter starts the process; the worker polls its
exit code). The worker needs no network path into the range; the guest pulls packages
from the depot through its pfSense WAN.

- `content/catalogue/software_catalogue.yaml` maps each name (and aliases such as
  `chrome`, `notepad++`, `apache`) to a Chocolatey id for Windows and apt/dnf packages for
  Linux. Role names such as `dns`, `iis`, `dhcp` stay in `services` and are skipped
  silently; any other unknown name is skipped with a warning in the provision result.
  Every package in the catalogue must be mirrored in the depot. The worker container
  reads the file from the `content/catalogue` mount (`TN_SOFTWARE_CATALOGUE`).
- **Windows**: `choco install <id> -y --no-progress --source <feed>`, one per entry, as
  the built-in Administrator with the random password Sysprep set. That password lives
  only in the worker's memory for the length of the provision task. Chocolatey must
  already be in the Windows templates (the build sheet §5.1 installs template software through it).
- **Linux**: cloud-init creates an ephemeral `tn-install` user (random password, sudo,
  expires in two days). The worker waits for `cloud-init status --wait`, runs
  `apt-get update`/`install` (or `dnf install`) with the depot as HTTP proxy, then locks
  and deletes the user and rewrites the VM's guestinfo without it. Output goes to
  `/var/log/tn-software-install.log` in the guest; Chocolatey's log is
  `C:\ProgramData\chocolatey\logs\chocolatey.log`.
- Results are per VM per package in `provisioner_output.vms[].software`. A failed or
  timed-out package makes the range **partial** (it still goes to `ready`; the errors are
  in `provisioner_output.errors`), never failed. Without an uplink, a depot URL, or an
  edge firewall in the range, installs are skipped with a warning
  (`provisioner_output.warnings`).
- Installs run in the same Celery task as the build, `VSPHERE_CONCURRENCY` VMs at a time,
  bounded by `TN_SOFTWARE_INSTALL_TIMEOUT` per VM and `VSPHERE_PROVISION_BUDGET` overall,
  so the guest passwords never pass through Redis. A range whose build and installs need
  more than about 55 minutes gets its remaining installs skipped (warning), not a second
  build. Bake large or slow software into templates instead (build guide §4).

The vCenter role (§4.2) also needs `VirtualMachine.GuestOperations.Execute`, `.Query`
and `.Modify` for this.

### 4.2 Grant the vCenter role (a human decision)

Use `scripts/create-vcenter-provision-role.ps1` in **`TrueNorth-Demo`** to assign
`TrueNorth-Provision` to `svc-truenorth` at the **datacenter** level, with propagation on.

Check that the role includes these privileges, which the provisioner now uses:
- `Network.Assign`
- `DVPortgroup.Create` / `Modify` / `Delete`
- `Folder.Create` / `Delete`
- `VirtualMachine.Provisioning.Clone` / `DeployTemplate` / `Customize`
- `VirtualMachine.Config.*`
- `VirtualMachine.Interact.PowerOn` / `PowerOff`
- `VirtualMachine.State.*` (snapshots)
- `VirtualMachine.GuestOperations.Execute` / `Query` / `Modify` (deploy-time software, §4.1a)
- `Datastore.AllocateSpace`
- `Resource.AssignVMToPool`
- `ContentLibrary.*` (read and deploy)
- `Cryptographer.*`, only if you build Windows 11 with a vTPM

Then:
```bash
ansible-playbook playbooks/90-vsphere.yml --ask-vault-pass -e tn_fail_on_missing_vsphere_privs=true
```

### 4.3 Content Library (optional for now)

The provisioner clones **inventory templates** directly (cross-host shared-nothing copy), so a
Content Library is not required. Create one if you want OVF items:
```bash
govc library.create -ds esx01-local TrueNorth-Templates
```

---

## 5. Day 2 — first provision (smoke test)

1. Register the template that already exists. In TrueNorth:
   - `POST /api/golden-images/import-catalogue?hypervisor=vsphere`
   - `PATCH` the `ubuntu-lts` image with `template_name=tmpl-ubuntu-2404`, `build_status=built`
2. Create a one-VM range from Ubuntu in Range Designer and provision it. Check:
   - [ ] the VM lands on esx02, 03 or 04 (never esx01), on that host's local datastore
   - [ ] a port group `tn-<range>-v1xx` appears on `vDS-10G` with a VLAN from 100–199
   - [ ] the VM's NIC is on that port group, **not** `dPG-TN-MGMT`
   - [ ] the guest has the IP TrueNorth rendered (cloud-init `guestinfo`)
   - [ ] stop and start, then snapshot and revert, all work
   - [ ] destroy removes the VM, the port group and the range folder
3. Record the result in `RESUME.md`. If the result is 403, the role from §4.2 is missing
   a privilege; the error names it.

**Unverified until this test.** The provisioner has only run against fakes, built from
real pyVmomi spec objects. If the smoke test fails, the first places to look are:
1. Content Library `find` bodies: is it a bare `{"name": …}` or wrapped in `{"spec": …}`?
   This only matters once a library is set.
2. The response shape of OVF `?action=filter` (`networks`) and the `network_mappings` map.
3. A cross-host clone from `tmpl-ubuntu-2404` (esx01-local) into another host's local
   datastore through `RelocateSpec(host, pool, datastore)`.
4. Whether `CreateDVPortgroup_Task` on vDS-10G accepts the security policy (promiscuous,
   forged transmits and MAC changes off; promiscuous on for monitor/span networks).
5. Ubuntu 24.04 cloud-init applying `guestinfo.metadata` network v2, and re-running on the
   new `instance-id`.
6. Windows Sysprep customization: needs VMware Tools in the template, and maps NICs in
   device-key order.
7. The power endpoints, which report states as `POWERED_ON` / `POWERED_OFF`.
8. With the uplink on: the WAN NIC is NIC 0 in the pfSense guest (`vmx0`), on
   `dPG-TN-SVC`, and the zones follow in order.
8a. pfSense per-range config: on the firewall console, `tn-pfsense-config status` shows the
   same sha256 for "guestinfo config" and "applied"; `/conf/tn-pfsense-config.log` has one
   "applied … rebooting" line and the VM rebooted exactly once; Interfaces shows WAN at
   `provisioner_output.uplink.ip` and each zone at its `.1`; Firewall → Rules → Floating
   has the depot pass and the block. Not provable off site: that `vmtoolsd --cmd info-get`
   works as an `earlyshellcmd` this early in pfSense CE 2.7.2's boot, that the generated
   config loads without the GUI flagging anything, and the vmxN↔MAC mapping on a 6-NIC
   firewall (soc-training). If the log says "NOT applied", the firewall runs the template
   config: fix the cause, then `tn-pfsense-config apply`.
8b. vCenter task waits are bounded (`VSPHERE_SNAPSHOT_TIMEOUT` per task; each
   WaitForUpdatesEx call returns within 30 s): a clone or snapshot that hangs in vCenter
   fails that VM with "still running after …" instead of holding the worker.
9. Guest operations: `guest.customizationInfo` reports `TOOLSDEPLOYPKG_SUCCEEDED` after
   Sysprep; `StartProgramInGuest` as the Sysprep Administrator runs `choco.exe` elevated;
   cloud-init on Ubuntu 24.04 creates `tn-install` from `guestinfo.userdata` (`users:`
   with `hashed_passwd`), and the clean-up removes it (`getent passwd tn-install` is empty
   a minute after the install).

---

## 6. Days 2–4 — golden images

Follow [`vm-build-guide.md`](vm-build-guide.md). Build order:
`srv2022` → `win11` → `ubuntu-lts` (already done) → `pfsense` → `kali` → `securityonion`
→ `rocky` / `debian13` → `srv2025` → everything else.

Register each image after it builds. `build.sh` does this automatically when
`TN_API_URL` and `TN_API_TOKEN` are set.

---

## 7. First real range

Use soc-training, or a small PO range. Check:
- [ ] `curl https://1.1.1.1` from a range VM **fails** (no egress)
- [ ] with the uplink on: a range VM reaches TN-DEPOT01 on tcp/8081 (Nexus) and tcp/3142 (apt-cacher-ng) and
      nothing else on 10.30.32.0/24; the software in its `services` is installed and
      `provisioner_output.vms[].software` says `ok`
- [ ] a range VM cannot reach 10.30.30.0/24, 192.168.1.0/24 or vCenter
- [ ] the pfSense LAN interface is the `.1` of each range subnet
- [ ] Security Onion sees traffic on the monitoring port group (promiscuous mode is
      enabled only there)
- [ ] port mirroring (see **7.1**) copies each zone named in a `mirror` rule to the sensors
- [ ] the full lifecycle works: scenario → telemetry in OpenSearch → AAR → destroy, with
      nothing left in vCenter

### 7.1 Port mirroring (SPAN / TAP zones)

A `mirror` rule becomes two sessions on `vDS-10G` per sensor zone (`vsphere_infra.py`,
"Port mirroring"): `tn-<range8>-<zone>` (Distributed Port Mirroring (legacy):
the source zones' VM ports to the sensors' ports, plus one uplink tagged with the
range's RSPAN VLAN) and `tn-<range8>-<zone>-rx` (Remote Mirroring Destination: that
VLAN to the sensors' ports). The local session alone only reaches a sensor on the same
host as the source; the RSPAN VLAN carries the other hosts' copies. They are listed in
`provisioner_output.mirrors`, and the RSPAN VLAN in `networks[]` with `"rspan": true`.
vcsim does not model mirroring, so none of this has been run against a real vCenter.

Before the first range (network team): the RSPAN VLANs come from `VSPHERE_VLAN_POOL`, so
they are already trunked to esx02–04. **MAC learning must be off on the pool VLANs used
for RSPAN** (Cisco: `vlan N` / `remote-span`). Otherwise the switch learns the mirrored
MACs, stops flooding, and sensors miss traffic from other hosts. Which VLAN is RSPAN is
known only after the build (step 1), so the simplest fix is to turn learning off for the
whole pool. If the switch can't do that, use `VSPHERE_PLACEMENT=per-range-host`: with all
of a range's VMs on one host, the local session is all it needs.

Check after building colosseum (or soc-training):
1. vSphere Client → `vDS-10G` → Configure → Port mirroring: the two sessions per sensor
   zone exist and are **enabled**; the sources are the zone VMs' ports, the destinations
   the sensors' ports (never the pfSense port), plus `dvUplink1` and the encapsulation
   VLAN on the first session. A `mirror …` line in the build's `errors` means vCenter
   refused a session. Check its fault text: if vCenter refuses a port as the destination
   of two sessions, report that.
2. Find which hosts the VMs are on. Pick a source VM on a **different** host from the
   sensor, and one on the **same** host.
3. On the sensor (`tcpdump -ni <span NIC> host <source VM IP>`; Security Onion:
   `so-tcpdump`), ping each source VM from its zone's gateway or another VM. Both must
   show up. If only the same-host VM shows up, the RSPAN path is broken: check the
   uplink and trunk, and MAC learning on the RSPAN VLAN.
4. Each ICMP request should appear **once**. Broadcasts (ARP) repeated once per VM in the
   zone mean the copy direction is reversed: set `VSPHERE_MIRROR_DIRECTION=transmitted`
   on the worker, then destroy and rebuild. Unicast packets doubled means it is `both`.
5. A non-range port group does not show the traffic: no leak out of the range.
6. Destroy the range: both sessions are gone from the vDS (destroy also removes any
   `tn-<range8>-*` session that was never recorded).

`VSPHERE_MIRROR_UPLINK` selects the uplink (default: the vDS's first uplink). It must be
an active uplink on every host.

---

## 8. Capacity — what fits

The range hosts are esx02–04: 24 physical cores, 48 threads, about 1.5 TB of RAM and
about 4.9 TB of thin-provisioned storage. **CPU is the bottleneck, not RAM.** How many
ranges fit depends entirely on how far you overcommit vCPUs. Range sizes are from
`vm-build-sheet.md` §4, after the 2026-10-03 right-sizing (old vCPU in brackets). The
management VMs (TN-MGMT01 and the others) use about 34 of the vCPUs below. Recompute after
any template change with `.venv/bin/python scripts/range-capacity.py` (add
`--vcpu-per-thread 2` for the 4-per-core column).

| Range | vCPU | RAM | Fits at 4 vCPU/core (96 vCPU, ~62 after mgmt VMs; discovery's conservative figure) | Fits at 4 vCPU/thread (192 vCPU, 158 after mgmt VMs; the provisioner default `VSPHERE_MAX_VCPU_PER_THREAD=4`) |
|---|---:|---:|---|---|
| medium-enterprise | 16 (20) | 44 GB | 3 | 9 |
| cloud-security | 38 (52) | 126 GB | 1 | 4 |
| soc-training | 48 (69) | 157 GB | 1 | 3 |
| red-team | 48 (67) | 153 GB | 1 | 3 |
| red-vs-blue | 64 (80) | 162 GB | 0 | 2 |
| large-enterprise | 106 (142) | 375 GB | 0 | 1 (disk: 7.4 TB provisioned; only fits thin) |

Counts are per range type on an otherwise empty lab; mixed ranges share the same 158 vCPU
(for example one soc-training + one red-team + one cloud-security = 134). The provisioner
places per host (64 vCPU each at 4/thread, less the management VMs on that host), so a
range larger than one host's headroom is split across hosts.

Exercise VMs mostly sit idle, so 4 vCPU per thread (8 per hyper-threaded core) is workable for training.
Watch CPU ready time in vCenter: above about 10%, students will feel it. To run more or
larger ranges, the next step is linked clones (not built yet). The templates have already
been right-sized (`vm-build-sheet.md` §4); RAM was left alone because it is not the limit.

---

## 9. Stop conditions

Stop and ask the human if any of these happen:
- discovery disagrees with §1
- a range VM gets an address on 10.30.30.0/24 or 192.168.1.0/24
- anything targets esx01 for a change
- any privilege is granted above datacenter level
