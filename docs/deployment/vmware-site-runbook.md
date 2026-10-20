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

### 4.1a Build and depot plane (ADR 0012; vm-build-sheet.md §1, §8)

**Current design: ADR 0012 and the work-site kit (#186, #187).** There is no TN-BUILD01.
TN-MGMT01 builds the golden templates on esx04, on the isolated build network, and
TN-DEPOT01 is the only machine that reaches the internet. The operator sequence, with exact
commands, is in [`install/lab/README.md`](../../install/lab/README.md), "Work site
(TN-MGMT01 build host)"; the addresses are in
[`install/lab/work-site/inventory/`](../../install/lab/work-site/inventory/).

| Port group | VLAN | Subnet | Gateway / DHCP | Who is on it | Egress |
|---|---:|---|---|---|---|
| `dPG-TN-MGMT` | 30 | 10.30.30.0/24 | UniFi .1 (routed), DNS 10.30.30.10 | TN-MGMT01 10.30.30.20; TN-DEPOT01 10.30.30.30 (SSH, TrueNorth, the depot's upstream path) | through .1, subject to the MGMT firewall |
| `dPG-TN-BUILD` | 31 | 10.30.31.0/24 | **No gateway.** DHCP .100–.199 from TN-MGMT01 (`tn-build-dhcp.service`, dnsmasq) | TN-MGMT01 `ens224` 10.30.31.20 (Packer HTTP 8800–8899, WinRM 5985 and SSH 22 callbacks); TN-DEPOT01 10.30.31.30 (8081 Nexus, 3142 apt-cacher-ng); build VMs | **none**: no route to the internet or to MGMT |
| `dPG-TN-SVC` (range uplink; **to create**, #214) | 32 | 10.30.32.0/24 | UniFi .1 (the range WANs' gateway), no DHCP | range firewall WANs .100–.199 (`tn_vsphere_range_uplink_pool`); TN-DEPOT01 10.30.32.10 (8081 Nexus, 3142 apt-cacher-ng, nothing else) | **none**: UniFi drops 10.30.32.0/24 → internet, MGMT, BUILD, 192.168.1.0/24 |

VLAN 31 is trunked only to esx04 for now (ADR 0012 §7), so a DRS "should run on esx04"
rule keeps TN-MGMT01 there.

| VM | NIC / port group | IP | Spec | Built from |
|---|---|---|---|---|
| TN-MGMT01 (build host) | NIC 0 `dPG-TN-MGMT` | 10.30.30.20 | §1 | already installed; `install/lab/build.yml` with the work-site inventory adds only what is missing (govc, xorriso, ...) |
| | NIC 1 `ens224`, `dPG-TN-BUILD` | 10.30.31.20 | | Packer callbacks and build-network DHCP |
| TN-DEPOT01 | NIC 0 `dPG-TN-MGMT` | 10.30.30.30/24, gw .1 | 4 vCPU / 16 GB / 100 GB + data disk | `scripts/lab/build-phase.sh depot` (`deploy-mgmt-vm.sh TN-DEPOT01 --site work`, a clone of `ubuntu-lts` on esx04), then `install/lab/depot.yml` |
| | NIC 1 `dPG-TN-BUILD`, always connected | 10.30.31.30/24, no gateway | | what builds use: `depot_url = http://10.30.31.30:8081/repository`, `apt_proxy = http://10.30.31.30:3142` (`scripts/lab/pkrvars-work.py --depot`) |
| | NIC 2 range uplink (`dPG-TN-SVC`), always connected (#214) | 10.30.32.10/24, no gateway | | what ranges use: `TN_DEPOT_URL=http://10.30.32.10`, ports 8081 and 3142 only. Added by `build-phase.sh depot` (step 3/5, y/N); `depot.yml` loads its firewall, then puts the address on it |

**Who fetches upstream: the depot, through `dPG-TN-MGMT`'s gateway.** A Nexus proxy
repository and apt-cacher-ng make the outbound request themselves, so a build VM asking
through them never touches the internet. The caches are offline by default
(`tn_depot_upstream_online: false`: apt-cacher-ng `Offlinemode`, Nexus proxies `blocked`);
egress control is that plus the MGMT firewall. There is no uplink NIC to disconnect, so
`scripts/lab/depot-uplink.sh` does not apply (it refuses the work-site depot), and the kit
does not run `prefetch.yml` (it would need internet on TN-MGMT01). Known gaps are in the
work-site README.

**Ranges reach the depot on its third NIC (owner decision 2026-10-20, #214).** Range
firewall WANs sit on the range uplink network (`dPG-TN-SVC`, VLAN 32), and TN-DEPOT01 has
a NIC there at 10.30.32.10. The range firewalls' `TN_DEPOT` rule is unchanged, and no range
gets a path to MGMT: the alternative (routing ranges to 10.30.30.30) was rejected for that
reason. These controls hold on the depot itself (`install/lab/roles/tn_depot`):
- **Host firewall** on that NIC (`nft` table `inet tn_depot_range`, loaded before the
  network at boot): only new TCP connections from 10.30.32.0/24 to 10.30.32.10 on 8081 and
  3142 come in. No SSH, no ICMP, no Docker registry ports (8082/8083), no other depot
  address; the depot opens nothing out of that NIC; nothing is forwarded into or out of it.
- **No forwarding:** `net.ipv4.ip_forward=0` and IPv6 forwarding off, dockerd with
  `"ip-forward": false`, Nexus on host networking (no Docker DNAT). `depot.yml` reads the
  kernel's values back after Nexus is up and fails if any is on.
- **No route out of it:** the NIC has no gateway and no DNS; the default route stays on
  MGMT. `depot.yml` fails if the default route is on it.
- **Fail closed:** the NIC is taken by its MAC only and renamed `tnrange0`; the firewall
  also matches the range subnet on any NIC. netplan never brings it up
  (`activation-mode: manual`); `tn-depot-range-link.service` does, after the firewall unit
  and a check of the live table, and `tn-depot-range-guard.timer` sets it DOWN within a
  minute if the table disappears (e.g. Ubuntu's `nftables.service` flushing the ruleset)
  or forwarding comes on.
- **Online windows:** going online (`prefetch.yml`) is refused while range VMs exist
  (override `tn_depot_force_online`), and the range network gets nothing while online.
- **Least privilege on the services:** apt-cacher-ng serves only Remap-ed repositories
  (`ForceManaged`, port 80; not an open proxy) and its admin pages need a depot-generated
  `AdminAuth`; Nexus anonymous reads only the Chocolatey feed, and Packer reads
  `installers` as `tn-build-read` with a secret generated on TN-MGMT01.
Order matters: cloud-init leaves the NIC unaddressed and DOWN, and `depot.yml` loads the
firewall before it puts 10.30.32.10 on it and before anything brings the link up.

> **Superseded design (noted 2026-10-19, #212).** Until ADR 0012 this section described a
> separate **TN-BUILD01** (10.30.31.10, "the only host with internet") and a depot on its
> own service network: TN-DEPOT01 at **10.30.32.10** on `dPG-TN-SVC` (VLAN 32), with an
> uplink NIC 10.30.31.11 on `dPG-TN-BUILD` connected only for a prefetch window
> (`depot-uplink.sh`). In that design `dPG-TN-BUILD` had a UniFi gateway (.1), DHCP
> .100–.200 and **internet egress**. ADR 0012 replaced it for the work vCenter: the build
> network has no gateway and no internet, and the depot is dual-homed on MGMT and BUILD.
> The TN-BUILD01 tooling (`install/lab/inventory/`, `create-portgroups.sh`,
> `depot-uplink.sh`, `prefetch.yml`) is still in the repo and still documented in the top
> half of `install/lab/README.md`; it is not what the work site runs.

> **KMS (TN-KMS01) is paused** and will come with future integration and testing.
> Templates and clones run unactivated until then. See §8 of the build sheet.

#### Range WAN uplink to the depot

> **Decided (2026-10-20, #214; was an open item of #212).** Range firewall WANs sit on
> `dPG-TN-SVC` (VLAN 32) and reach TN-DEPOT01's third NIC at 10.30.32.10, so the values
> below are the work site's again. `install/inventory/group_vars/all/main.yml` carries
> them (`tn_depot_url`, `tn_depot_choco_feed`, `tn_depot_apt_proxy`, `tn_depot_ports`).
> **TODO (site):** `dPG-TN-SVC` does not exist at the work site yet, so
> `tn_vsphere_range_uplink_network` stays empty (ranges isolated) until the operator steps
> in `install/lab/README.md`, "Ranges reach the depot", have passed; setting it is the
> last of them.

By default a range is fully isolated: its firewall gets one NIC per range zone and
nothing else, and no range VM can reach the depot. To give ranges the depot, set the
uplink (in `group_vars/all/main.yml`, or the `.env.production` keys in brackets):

| Variable | Lab value | Meaning |
|---|---|---|
| `tn_vsphere_range_uplink_network` (`VSPHERE_RANGE_UPLINK_NETWORK`) | `dPG-TN-SVC` | Network for the edge firewall's WAN. Empty = isolated (the default). The provisioner **refuses** it if it is listed in `VSPHERE_MGMT_NETWORK` |
| `tn_vsphere_range_uplink_pool` (`VSPHERE_RANGE_UPLINK_POOL`) | `10.30.32.100-10.30.32.199` | Static WAN addresses, one per range. Must not overlap anything else on VLAN 32 |
| `tn_vsphere_range_uplink_gateway` (`VSPHERE_RANGE_UPLINK_GATEWAY`) | `10.30.32.1` | WAN gateway |
| `tn_vsphere_range_uplink_prefix` (`VSPHERE_RANGE_UPLINK_PREFIX`) | `24` | WAN prefix length |
| `tn_depot_url` (`TN_DEPOT_URL`) | `http://10.30.32.10` | The depot as range guests reach it: scheme and IP address only, **no port, no path** (the worker appends `:8081/repository/chocolatey/` for the default feed; the firewalls' `TN_DEPOT` alias is its host). Empty = no deploy-time installs |
| `tn_depot_choco_feed` (`TN_DEPOT_CHOCO_FEED`) | `http://10.30.32.10:8081/repository/chocolatey/` (empty derives the same) | Nexus Chocolatey (NuGet) feed |
| `tn_depot_apt_proxy` (`TN_DEPOT_APT_PROXY`) | **`http://10.30.32.10:3142`** | HTTP **forward** proxy for apt/dnf: apt-cacher-ng on the depot. **Set it**: empty falls back to `TN_DEPOT_URL` (port 80), where nothing listens. Guests keep their stock sources; the Rocky template must use the `http://dl.rockylinux.org` `baseurl` (an HTTPS mirrorlist can only be tunnelled, and the depot refuses tunnels) |
| `tn_depot_ports` (`TN_DEPOT_PORTS`) | `8081,3142` | TCP ports of the depot each range firewall lets the zones reach through its WAN; everything else out of the WAN is blocked. The depot's own firewall admits exactly these |
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
- The UniFi side of VLAN 32 denies `dPG-TN-SVC` → internet, MGMT, BUILD and
  192.168.1.0/24 (`scripts/lab/create-portgroups.sh --site work` prints the checklist).
  Three layers are needed: the UniFi rule and the depot's own firewall protect the
  platform if a student reconfigures pfSense.

**OPNsense and VyOS are configured per range too** (same policy as pfSense above):
- **OPNsense:** the same config, in OPNsense's legacy `config.xml` layout (root
  `<opnsense>`), in `guestinfo.tn.opnsense.config` / `.ifmap`. The boot script is the
  pfSense one installed as `/usr/local/sbin/tn-opnsense-config`; `tn-opnsense-config
  install` adds the hook `/usr/local/etc/rc.syshook.d/early/50-tn-opnsense-config`
  (build guide, OPNsense). It works on `/conf/config.xml` and logs to
  `/conf/tn-opnsense-config.log`. `provisioner_output.vms[].opnsense` records it.
- **VyOS:** configuration commands (`control-plane/worker/worker/vyos_config.py`) as
  cloud-init user data (`guestinfo.userdata`, `vyos_config_commands`), applied once per
  instance id by VyOS's cloud-init on first boot: `ethN` pinned to each NIC's MAC
  (`hw-id`), addresses, the default route to the uplink gateway, source NAT out of
  `eth0`, a forward filter with default **drop** (replies; zones → depot TCP
  `TN_DEPOT_PORTS`; the template's rules, or zone ↔ zone), and an input filter that drops
  new connections from the WAN. `TN_DEPOT_URL`'s host must be an IP address for the
  depot rule. `provisioner_output.vms[].vyos` records it.
- Neither has had a live run yet: both are tested against the renderers and the vSphere
  fakes (`tests/worker/test_appliance_configs.py`).

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
      `provisioner_output.vms[].software` says `ok` (#214: a Chocolatey package on a
      Windows VM)
- [ ] from the range firewall's shell: 10.30.32.10 tcp/22, 8082, 8083 and ping get no
      answer; 10.30.30.30 and 10.30.31.30 are unreachable (#214)
- [ ] a range VM cannot reach 10.30.30.0/24, 192.168.1.0/24 or vCenter
- [ ] the pfSense LAN interface is the `.1` of each range subnet
- [ ] Security Onion is set up (`securityonion.status` `ok`, see **7.2**) and sees traffic
      on the monitoring port group (promiscuous mode is enabled only there)
- [ ] port mirroring (see **7.1**) copies each zone named in a `mirror` rule to the sensors
- [ ] in a range with Exchange, SharePoint SE or MECM (red-vs-blue, large-enterprise): the
      VM joined the range domain and `vms[].role_setup.status` reaches `ok` (see **7.3**;
      `running` at the end of the build is normal for Exchange: read `C:\TrueNorth\deploy\status`)
- [ ] the full lifecycle works: scenario → telemetry in OpenSearch → AAR → destroy, with
      nothing left in vCenter

### 7.0a Range accounts (ADR 0012)

A range template's `accounts:` are set at deploy: Windows `Administrator` through Sysprep,
Linux accounts through cloud-init (SHA-512-crypt hashes in the VM's `guestinfo.userdata`).
After first boot the worker logs in to each such Linux VM as the throwaway `tn-install`
user, waits for `cloud-init status --wait`, checks every account with `getent passwd`,
deletes `tn-install`, and rewrites `guestinfo.userdata` without the hashes. It waits at most
`VSPHERE_ACCOUNT_SCRUB_TIMEOUT` (default 300 s) and rewrites the guestinfo either way. The
build's warnings say what did not work:

- `the install login failed, so the ephemeral tn-install user may remain`: the VM keeps a
  passwordless-sudo `tn-install` account (random password, never shown) until it expires
  two days after deploy. Remove it by hand if the range lives longer or is shared:
  `sudo userdel -r tn-install` on that VM.
- `account(s) not verified, recorded as not set`: those accounts are shown to staff as
  "Not set by TrueNorth" and never to Students.

**To verify on the real vCenter (not yet done):** vCenter's events and task history may keep
the VM's `extraConfig` as it was before the rewrite (the reconfigure task's arguments), so
the account hashes may stay readable there to anyone with read access to the VM's events,
for vCenter's event retention period. Check `Monitor > Events` / the task details of a range
VM's first reconfigure, and shorten event retention or restrict read access to range VMs if
they show.

Machines with no declared account: Windows gets `TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD` (the
templates' generic Windows password; the deprecated `TN_RANGE_DEFAULT_PASSWORD` only when
it is unset; random per VM when neither is) as its Administrator password; Linux keeps the
template's generic build password (ADR 0012 §5), which is not repeated in cloud-config.
Staff see both as "template default" on the range's Credentials panel.

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

Check after building soc-training (or another range with a sensor zone):
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

### 7.2 Security Onion in SOC ranges (so-setup at deploy)

The `securityonion` template is the SO 2.4 ISO's OS install only (ADR 0012, decision 8).
At deploy, the worker sets up each VM of catalogue id `securityonion` as a **STANDALONE**
node in **airgap** mode (`worker/provisioners/vsphere_securityonion.py`):
- airgap installs everything from the ISO (`/nsm/repo`) and needs no internet or depot;
- management is the VM's non-monitor NIC, with a static address, gateway and DNS from the
  range plan (soc-training: `soc_tools`, 10.60.202.60);
- monitoring is the NIC on the zone a `mirror` rule targets, or one flagged
  `monitor: true` (soc-training: `network_monitoring`). That zone's port group is the only
  promiscuous one, and it is the mirror sessions' destination (7.1). The guest finds both
  NICs by MAC address.

SO 2.4 has no answer file. The worker runs so-setup with its built-in test profile, and
replaces the profile's hard-coded answers with the range's
(`/root/tn-so-answers.env`, deleted after setup). Setup runs detached in the guest, takes
20–60 minutes, and carries on even if the worker stops waiting. The build starts it and
records it `running`; the reconcile task (§7.3) reads its status later, verifies the accounts
and records the outcome.

Before the first SOC range, set on the worker:

| Setting | Value |
|---|---|
| `VSPHERE_SO_INSTALL_USER` / `VSPHERE_SO_INSTALL_PASSWORD` | the template's build login (the user created in the SO installer; wiki). It runs guest operations and `sudo`. (`TN_RANGE_DEFAULT_PASSWORD` is not used: it is the Windows Administrator fallback.) Without these, the machine's `role: admin` account logs in, which works only for a template built with it. |
| `VSPHERE_SO_SETUP_TIMEOUT` | seconds from the start of setup until a node that is not healthy is a `timeout`, default `7200`. Read by the reconcile task; the build itself waits only `VSPHERE_SETUP_BUILD_WAIT` (default 0). |
| `TN_SECRETS_KEY` | already set for hypervisor passwords. A generated web password is stored sealed with it. |

Check after the build:
1. `provisioner_output.vms[].securityonion.status` is `ok` (`so-status` healthy). Other
   statuses are warnings, not a failed range:
   - `running`: still being set up; the reconcile task records the result within minutes of
     it finishing (the accounts appear, as "being set up" to staff until then);
   - `timeout`: `VSPHERE_SO_SETUP_TIMEOUT` ran out;
   - `failed`, with a reason: see `/root/tn-so-setup.log`, `/root/sosetup.log` and
     `/root/errors.log` in the guest;
   - `skipped`: no install login, one NIC only, or an answer so-setup would refuse.
   The current state is a number in `/var/lib/tn-so-setup/status`: 0 healthy, 2/3 running,
   10+ failed.
2. Accounts come from the range's `accounts:` for the machine (ADR 0012):
   - the **first `role: admin`** account becomes the node's OS admin: created if missing,
     password set, `wheel`;
   - the **first `role: user`** account (by username) is the SOC console login. It must be
     an e-mail address (soc-training: `analyst@range.local`, Student-visible); a name
     without `@` gets `@range.local`.
   Both passwords are on the range's Credentials panel. An account is marked applied (and
   shown to Students) only once verified in the guest: the admin by `getent passwd`, the
   web login by `so-user list`. Any other account declared for the machine is not set.
   Browse to `https://<management IP>`
   (`web_url`) from the SOC tools or management zone (`so_allow` on the node adds zones).
   With no `role: user` account, the login is `analyst@range.local` with a password
   generated by the worker, which is never displayed: it is in
   `/root/tn-so-web-credentials` in the guest (root only), and sealed in
   `securityonion.web_password_sealed`.
3. `so-tcpdump` on the monitor interface (`bond0`) sees the source VMs' traffic (7.1, step 3),
   and Zeek/Suricata events reach the Dashboards.

Only a real vCenter can show that the template has open-vm-tools running and that the
install login has sudo. The status is then `failed: could not start …`, or
`guest operations not ready`.

### 7.3 Exchange, SharePoint SE and MECM (product setup at deploy)

The role images `srv2022-exchange2019`, `srv2022-sharepointse`, `srv2022-sharepoint2019` and `srv2022-mecm`
(`infra/vsphere/packer/roles.pkr.hcl`) carry the prerequisites and the staged media only.
Each product needs the range's domain, so the worker finishes it per range
(`worker/provisioners/vsphere_products.py`). It runs on a VM whose node names the role
(`exchange`, `sharepoint_se`, `sccm` …, `windows-server-2022`) and which was cloned from the
role image. Until one of these three images is `built` (golden-image registry; other role images resolve as before) and present on the vCenter, a range builds the plain OS (`srv2022`) instead, with the warning "role image <id> not built — using srv2022; <role> not installed" and `role_setup` `skipped`. Nothing is set up on it, and the range is not partial for it.

Order, per VM:
1. After every VM's Windows roles, so the forest-root DC of the VM's domain has been
   promoted. The domain is the node's `ad_domain`, else the template's `ad_domain`, else
   the first `ad_forest` a DC names. If that DC did not end `ad-ds: ok`, the setup is
   `skipped`.
2. As the VM's local Administrator: check `C:\TrueNorth\role-image.json` (the staged-image
   marker the image build writes; missing = `skipped`, nothing runs). Make
   `C:\TrueNorth\deploy` readable only by Administrators and SYSTEM, deliver the DC's
   password to it by VMware **guest file transfer** (never a command line), set DNS to the
   DC, wait for it (up to 15 min), `Add-Computer` (the join reads then deletes the password
   file), reboot.
3. The VM's own deploy-time software (§4.1a), through the same VSPHERE_CONCURRENCY
   semaphore as the rest, so no package install races the product's installer.
4. If no status file exists yet: reboot first when the software installs left one pending
   (waiting for VMware Tools), then deliver the runner, the (non-secret) parameters and, for
   SharePoint, a secrets file (the
   product key and farm passphrase) by guest file transfer into that folder, then start the
   setup **detached through VMware guest operations authenticated as `<DOMAIN>\Administrator`**.
   There is no scheduled task and no stored Windows credential: the password reaches only the
   vSphere API auth of the guest-operations call. The started process keeps running after the
   call returns (1–2 h). The runner reads its inputs from the delivered file and deletes it in
   a `finally`; a fresh attempt also deletes any leftover, so nothing secret lingers.
   - **Exchange 2019**: `Setup.exe /PrepareSchema`, `/PrepareAD /OrganizationName:"<range name>"`,
     `/Mode:Install /Role:Mailbox`, each with `/IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF`.
   - **SharePoint SE**: only with a product key. It completes the image's own SQL Server 2022
     (`/ACTION=CompleteImage`), runs `setup.exe /config` with the key, then builds a
     single-server farm (`New-SPConfigurationDatabase -LocalServerRole SingleServerFarm`,
     Central Administration on port 9999, farm account `<DOMAIN>\Administrator`). The key is
     masked from the SharePoint setup logs after install. A range SQL VM is not used:
     TrueNorth does not build `srv2022-sql2022` or join SQL VMs to the domain (a warning says so).
   - **SharePoint Server 2019** (Large Enterprise, service `sharepoint_2019`; `sharepoint_se` picks SE):
     the same steps as SE, from `srv2022-sharepoint2019` (Microsoft Learn, *Hardware and software
     requirements for SharePoint Server 2019*, lists Windows Server 2022 and SQL Server 2022 as
     supported). Its prerequisites are installed in the image one by one from the depot: offline
     and deterministic. The documented alternative is `prerequisiteinstaller.exe` with local files
     (`/SQLNCli:`, `/Sync:`, `/AppFabric:`, `/IDFX11:`, `/MSIPCClient:`, `/KB3092423:`,
     `/WCFDataServices56:`, `/DotNet472:`, `/MSVCRT11:`, `/MSVCRT141:`). Upload
     to `installers/roles/sharepoint2019/`: `sqlncli.msi`, `Synchronization.msi`,
     `MicrosoftIdentityExtensions-64.msi`, `setup_msipc_x64.exe`, `WcfDataServices.exe`,
     `vcredist_x64.exe` (VC++ 2012), `VC_redist.x64.exe` (VC++ 2015-2022),
     `WindowsServerAppFabricSetup_x64.exe`, `AppFabric-KB3092423-x64-ENU.exe`.
   - **MECM**: SQL `/ACTION=CompleteImage` (`SQL_Latin1_General_CP1_CI_AS`), `extadsch.exe`,
     the `System Management` container (Full Control for the site server's computer
     account), then `setup.exe /SCRIPT <ini> /NOUSERINPUT`. The ini makes a primary site:
     `ProductID=EVAL` (180 days from install, longer than a range lives), site code `T` plus
     the first two hex digits of the range id, SDK/MP/DP on the server, no service
     connection point.
5. The build waits at most `VSPHERE_SETUP_BUILD_WAIT` (default 0), records the setup
   `running` with its status file, and finishes within its budget. The **reconcile task**
   (`worker.setup_reconcile.reconcile_setups`, beat, every 5 min, worker-provision queue)
   reads the status file in the guest later — re-authenticating as the domain Administrator,
   whose password it re-reads from the range's credentials each run, never stored in the
   output — and records `ok` / `failed` / `timeout`, verifying and marking the accounts
   `applied`. Until then staff see those accounts as **"being set up"** (not the real
   password rows — those are the declared/generated ones they already see; the *applied*
   state is what changes); Students see them only once verified.
   The reconcile follows the range through every live state (ready, running, stopping,
   stopped, starting), so a stop/start mid-setup does not strand it. The timeout is checked
   before any guest operation: a VM that is powered off, unreachable or no longer accepts the
   login still ends `timeout`. One run at a time (Redis lock), at most 90 s of guest waiting per
   VM and 240 s per range (`SETUP_RECONCILE_RANGE_SECONDS`), 1200 s per run
   (`SETUP_RECONCILE_RUN_SECONDS`). Lab-session ranges are skipped. Security Onion: the SOC web
   login is recorded under its declared username; an admin account that is the install login
   gets its declared password first; an admin is applied only once a login with its declared
   password works.

The domain Administrator is the DC's built-in Administrator after forest promotion. A range
with a product role image gets a **per-range generated Administrator** for the product VM
and the domain controllers when the template declares none (the credential service, #183;
staff-visible, sealed), so one range's compromise does not expose another range's domain
admin. A declared `Administrator` for the DC (`accounts:`, §7.0a) is used instead when
present. The site-wide template default is no longer the fallback for these machines.

> **To verify on the first real build:** the built-in domain Administrator (RID 500) is
> assumed to get a full (unfiltered) token under the default UAC policy, so a guest-operations
> process started as it can run Exchange / ConfigMgr / SharePoint setup. If a real build shows
> a filtered token, that is a follow-up (a stored-credential scheduled task would be the
> fallback), not this change. A filtered token shows up as `failed` with "the status file ...
> exists but the domain Administrator cannot read it (access denied)" (status read exit 201).
> The worker must also reach each ESXi host on **443** for the
> guest file transfer (mblanke/TrueNorth#194); if it cannot, the setup is `failed` with that
> reason and nothing is left half-done.

Settings (worker):

| Setting | Value |
|---|---|
| `TN_SHAREPOINT_PRODUCT_KEY` (installer `vault_sharepoint_product_key`) | the site's SharePoint key from the Visual Studio subscription, for the SharePoint product its ranges use: SharePoint Server 2019 for Large Enterprise, SE for an SE range. One variable for both; a site running SE and 2019 ranges at once would need two keys, which is not supported yet. A key for the wrong version fails setup (status 13). SharePoint 2019 also has a public 180-day trial key on Microsoft's download page; TrueNorth does not embed it. Empty: the role is `skipped`, "staged (no product key)", and nothing runs on the VM. Never logged; preflight checks its form. |
| `VSPHERE_ROLE_SETUP_TIMEOUT` (installer `tn_vsphere_role_setup_timeout`) | seconds before a setup still running is a `timeout`, default `7200`. Measured from the domain join and read by the reconcile task, not waited on in the build. |
| `VSPHERE_SETUP_BUILD_WAIT` | seconds the build itself waits for a setup after starting it, default `0` (record `running`, leave it to the reconcile task). |
| `TN_DEPOT_URL` | MECM only. When the image has no `C:\TrueNorth\media\mecm-prereqs`, the runner downloads `<TN_DEPOT_URL>:8081/repository/installers/roles/mecm/configmgr-prereqs.zip` through the range uplink. |

**ConfigMgr prerequisite files.** ConfigMgr setup needs the files `setupdl.exe` downloads
(`SMSSETUP\BIN\X64\setupdl.exe <folder>` on a machine with internet, same ConfigMgr
version as the ISO). Zip that folder's contents and upload it once as the `tn-prefetch`
user:
`curl -u tn-prefetch --upload-file configmgr-prereqs.zip http://10.30.30.30:8081/repository/installers/roles/mecm/configmgr-prereqs.zip`.
With it on the depot at image build time, `role-image.ps1` stages it in the image and no
range downloads it. Also put `msodbcsql18.msi` there: current ConfigMgr setup refuses a
site server without ODBC Driver 18.

Check after the build:
1. `provisioner_output.vms[].role_setup.status` (also on `roles.<role>`). Anything other
   than `ok` is a warning, never a partial range:
   - `running`: still being set up — the reconcile task finishes it (normal at the end of a
     build; Exchange takes 1–2 h); `timeout`: not healthy within `VSPHERE_ROLE_SETUP_TIMEOUT`;
   - `failed`, with a reason: see `C:\TrueNorth\deploy\runner.log` and the product log the
     reason names (`C:\ExchangeSetupLogs`, `C:\ConfigMgrSetup.log`, `C:\ExtADSch.log`, the
     SharePoint Setup log in `C:\TrueNorth\deploy`);
   - `skipped`: no product key (SharePoint), not a staged image, no DC of the domain, the DC
     not promoted, or no budget left.
   The current state is a number in `C:\TrueNorth\deploy\status`: 0 done, 2–9 running, 10+
   failed (`vsphere_products.STATUS`). The runner never starts twice on one VM: a
   redelivered build (or the reconcile task) finds that status file and only reads it.
2. `accounts_applied`: the VM's own Administrator, plus (once the reconcile task verifies the
   setup) any account the product sets. Staff see an unverified account on a still-running
   machine as "being set up" on the Credentials panel and the wiki page.

---

## 8. Capacity — what fits

The range hosts are esx02–04: 24 physical cores, 48 threads, about 1.5 TB of RAM and
about 4.9 TB of thin-provisioned storage. **CPU is the bottleneck, not RAM.** How many
ranges fit depends entirely on how far you overcommit vCPUs. Range sizes are the shipped
`content/ranges/*/template.yaml` as of 2026-10-07; ranges are sized for realism and quotas
govern capacity, so these are not targets. The management VMs (TN-MGMT01 and the others)
use about 34 of the vCPUs below. Recompute after any template change with
`.venv/bin/python scripts/range-capacity.py` (add `--vcpu-per-thread 2` for the
4-per-core column).

| Range | vCPU | RAM | Fits at 4 vCPU/core (96 vCPU, ~62 after mgmt VMs; discovery's conservative figure) | Fits at 4 vCPU/thread (192 vCPU, 158 after mgmt VMs; the provisioner default `VSPHERE_MAX_VCPU_PER_THREAD=4`) |
|---|---:|---:|---|---|
| medium-enterprise | 20 | 44 GB | 3 | 7 |
| cloud-security | 52 | 126 GB | 1 | 3 |
| soc-training | 69 | 157 GB | 0 | 2 |
| red-team | 67 | 149 GB | 0 | 2 |
| red-vs-blue | 82 | 162 GB | 0 | 1 |
| large-enterprise | 142 | 375 GB | 0 | 1 (disk: 7.2 TB provisioned; only fits thin) |

Counts are per range type on an otherwise empty lab; mixed ranges share the same 158 vCPU
(for example one red-team + one cloud-security = 119). The provisioner
places per host (64 vCPU each at 4/thread, less the management VMs on that host), so a
range larger than one host's headroom is split across hosts.

Exercise VMs mostly sit idle, so 4 vCPU per thread (8 per hyper-threaded core) is workable for training.
Watch CPU ready time in vCenter: above about 10%, students will feel it. To run more or
larger ranges, the next step is linked clones (not built yet).

---

## 9. Stop conditions

Stop and ask the human if any of these happen:
- discovery disagrees with §1
- a range VM gets an address on 10.30.30.0/24 or 192.168.1.0/24
- anything targets esx01 for a change
- any privilege is granted above datacenter level
