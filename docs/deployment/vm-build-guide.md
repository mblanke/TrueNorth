# VM build guide — every exercise image on the vSphere lab

How to create the golden templates that TrueNorth clones into ranges. It covers Windows,
the Linux flavours, pfSense, Security Onion, VyOS, the derived images and custom
variants (§5).

- **What** goes into each image, and why: [`../vm-build-sheet.md`](../vm-build-sheet.md).
- **How Packer does it**: [`../../infra/vsphere/packer/README.md`](../../infra/vsphere/packer/README.md).
- **Lab bring-up order**: [`vmware-site-runbook.md`](vmware-site-runbook.md).

## The rules every image follows

1. **Name = catalogue id.** Name the template or Content Library item exactly after the
   catalogue id: `srv2022`, `win11-24h2`, `ubuntu-lts`, `pfsense`… The registry resolves by
   that name. An image built under a different name (for example the existing
   `tmpl-ubuntu-2404`) needs a `PATCH /api/golden-images/{id}` that sets `template_name`.
2. **Generic.** No domain join, no product key (Windows uses the public KMS client
   key/GVLK, or eval media), no per-range users and no secrets. Windows is sysprepped
   (`/generalize /oobe /shutdown`). Linux runs `cloud-init clean` and has its machine-id
   truncated.
3. **VMware guest bits baked in.**
   - Windows: VMware Tools (needed for the IP customization TrueNorth applies at deploy).
   - Linux: `open-vm-tools` and cloud-init with the `VMware` datasource (TrueNorth passes
     each VM's network config through `guestinfo`).
4. **Hardware.** vmxnet3 NICs. Disk controller: pvscsi for Windows (the driver is loaded
   at install time from the VMware Tools ISO). Linux has the driver built in.
   **Do not use virtio**; it is Proxmox-only.
5. **One NIC in the template.** TrueNorth adds and attaches NICs to the range port groups at
   deploy time. pfSense and VyOS templates keep 2 NICs: NIC 1 is WAN, NIC 2 is LAN.
6. **Built on esx01.** The ISO folder `[esx01-local] ISO/` is local to esx01, so builds run
   there. Templates are copied to other hosts when cloned (shared-nothing), so no further
   staging is needed.

## 1. Media — what's on the datastore

These were found in `[esx01-local] ISO/`. Re-check with `scripts/vsphere-discover.py`,
which prints an ISO coverage table.

| Image | ISO present | Status |
|---|---|---|
| srv2022 | `en-us_windows_server_2022_updated_july_2026_x64_dvd_75aa9e18.iso` | ✅ retail/VL media, so it uses the 2022 GVLK |
| srv2025 | `en-us_windows_server_2025_updated_july_2026_x64_dvd_4e6f5a42.iso` | ✅ new setup engine, high risk (see §3) |
| win11-24h2 | `en-us_windows_11_consumer_editions_version_24h2_updated_feb_2026_x64_dvd_4e400c9e.iso` (26H1 also present) | ✅ consumer media, so it uses Pro + the Pro GVLK |
| ubuntu-lts | `ubuntu-24.04.4-live-server-amd64.iso` | ✅ `tmpl-ubuntu-2404` is **already built** |
| kali | `kali-linux-2026.1-installer-everything-amd64.iso` | ✅ |
| rocky | `Rocky-10.2-x86_64-dvd1.iso` | ✅ Rocky 10 |
| debian13 | `debian-13.6.0-amd64-DVD-1.iso` | ✅ |
| parrot | `Parrot-security-7.3_amd64.iso` | ✅ installer ignores preseed, so the build is partly manual |
| win10-22h2 | — | ❌ get the Windows 10 Enterprise eval ISO |
| win10-ltsc | — | ❌ get Windows 10 Enterprise LTSC 2021 (eval or VL); catalogue row is `enabled=no` until it is built |
| srv2019 / srv2016 | — | ❌ get the eval ISOs (2019 is needed for SharePoint 2019) |
| pfsense | — | ❌ get pfSense CE 2.7.2 (Netgate now ships a net installer; the 2.7.2 ISO is on mirrors) |
| securityonion | — | ❌ get Security Onion 2.4.x |
| vyos | — | ❌ get VyOS 1.4 (rolling, or a stream build) |
| win7-sp1 | — | ❌ licensed media only, so this needs procurement |
| Role media | SQL 2022/2025 Ent, Exchange 2016, SharePoint 2019, Office Pro Plus 2021 | ✅ used for role snapshots (§6), not for templates |

The build sheet decided **Exchange 2019 + SE** and **Office LTSC 2024**, but the media on
the datastore are **Exchange 2016** and **Office 2021**. Either obtain the decided versions,
or record the change in `vm-build-sheet.md` §8.

Upload a missing ISO (from a machine that has it):
```bash
govc datastore.upload -ds esx01-local ./pfSense-CE-2.7.2-RELEASE-amd64.iso ISO/pfSense-CE-2.7.2-RELEASE-amd64.iso
```
Then set its file name in `infra/vsphere/packer/lab.auto.pkrvars.hcl`. Until you do,
`iso_pfsense` and the others default to `MUST-UPLOAD-FIRST_…`.

## 2. The Packer path (default)

Run this on **TN-BUILD01** (runbook §4.1a), the only host with internet. It sits on `dPG-TN-BUILD`, where the
build VMs can reach it:

```bash
cd infra/vsphere/packer
cp lab.auto.pkrvars.hcl.example lab.auto.pkrvars.hcl   # fill vcenter_password, windows_admin_password
packer init .
./build.sh list
./build.sh srv2022
```

To register each image automatically after it builds:
```bash
export TN_API_URL=https://<platform>/api TN_API_TOKEN=<infra:write token>
./build.sh srv2022 win11-24h2 pfsense kali
```

Logs go to `infra/vsphere/packer/logs/<name>-<timestamp>.log`. A failed build leaves no
template. Re-run with `-force`; `build.sh` already passes it.

### Confidence per image

| Image | Method | Expect |
|---|---|---|
| srv2022, srv2019, srv2016, win10-22h2, win10-ltsc | Autounattend on a CD, pvscsi from the Tools ISO, WinRM, `setup.ps1`, sysprep | Should work first time |
| ubuntu-lts, rocky | autoinstall (`cidata` CD) / kickstart (`OEMDRV` CD) | Should work first time |
| kali, debian13 | preseed over Packer's HTTP server | Works if the build VM can reach TN-BUILD01 on the HTTP port |
| win11-24h2, srv2025 | Autounattend, 4-partition GPT, LabConfig TPM bypass unless `win11_vtpm=true` | **Highest risk.** The new setup engine broke the earlier attempts. Have the manual path (§3) ready |
| pfsense, securityonion, vyos | `boot_command` keystrokes | Fragile: timings may need tuning on site. Security Onion builds only the base OS; `so-setup` runs at deploy |
| parrot | Calamares GUI installer | Mostly manual (§3) |
| derived (remnux, sift, svc-emulators, ca-host, usersim, cloudlog-emu, c2-server, precomp-host, detonation-host) | `vsphere-clone` of the base template, plus role scripts | Needs the base built first. Some installers need internet, and build over the mgmt network's limited egress |
| variants (win10-office, win11-analyst, win11-dev, ubuntu-webstack, your own) | `vsphere-clone` of a base + packages + scripts (§5) | Needs the base built first. Windows depends on vSphere guest customization working on that base |

## 3. The manual path (fallback for any image)

When Packer fights you, build the image by hand once. A golden image is built once and
cloned many times, so a manual build is a fine outcome.

### Windows (Server 2016/2019/2022/2025, Windows 10/11)

1. **New VM** in the vSphere Client on **esx01**, datastore `esx01-local`:
   - Guest OS: matching Windows version. Firmware: EFI, with Secure Boot on for Win11/2025.
   - 2 vCPU / 4 GB. Disk: 40 GB for servers, 64 GB for Win11. Thin provisioned.
   - SCSI controller: **VMware Paravirtual**. NIC: **vmxnet3** on `dPG-TN-BUILD`.
   - Win11: add a **vTPM** if a key provider exists (the discovery report says). Otherwise
     install with the LabConfig bypass: press Shift+F10 in setup, then
     `reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1`, and the
     same for `BypassSecureBootCheck`.
   - CD 1: the Windows ISO. CD 2: `[] /vmimages/tools-isoimages/windows.iso`.
2. **Install.** At "Where do you want to install Windows?", click **Load driver** and browse
   CD 2 to `Program Files\VMware\VMware Tools\Drivers\pvscsi\Win8\amd64`. The disk then
   appears. Choose the edition:
   - Servers: Standard (Desktop Experience).
   - Win10/11: Enterprise eval, or Pro on consumer media.
   - Key: skip it, or use the public GVLK.
3. **First login.** Run `setup64.exe` from CD 2 to install VMware Tools, then reboot.
4. **Baseline.** Run `infra/vsphere/packer/files/windows/setup.ps1` as Administrator.
   It enables WinRM and OpenSSH, sets the power plan, and installs the baseline apps
   (Chocolatey, from the depot when `depot_url` is set). Install Windows Updates now if
   the VM has internet.
5. **Generalize.**
   ```powershell
   C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\sysprep-unattend.xml
   ```
   Copy `files/windows/sysprep-unattend.xml` to `C:\` first. The VM powers off.
6. **Template.** Remove both CDs. Right-click → **Template → Convert to Template**, and
   **rename it to the catalogue id**. Optionally, right-click → **Clone to Library** →
   `TrueNorth-Templates`, using the same name.
7. **Register** it (§7).

### Linux (Ubuntu, Rocky, Debian, Kali, Parrot)

Fastest path: use a **cloud image** with no OS install. This is how `tmpl-ubuntu-2404` was
made:
```bash
govc import.ova -ds esx01-local -options import-opts.json ubuntu-24.04-server-cloudimg-amd64.ova   # by esx01 IP
```
Then mark it as a template **in vCenter**, never with `govc vm.markastemplate` against the
host. Debian, Rocky and Kali ship `qcow2` cloud images: convert them on TN-BUILD01 with
`qemu-img convert -O vmdk -o subformat=streamOptimized`, then import.

ISO path:
1. New VM on esx01: Linux guest type, EFI, 2 vCPU / 4 GB / 20–40 GB thin, vmxnet3 on
   `dPG-TN-BUILD`, ISO attached.
2. Install with a minimal / server profile and an SSH server.
3. Install the guest bits:
   - Debian/Ubuntu/Kali: `apt install -y open-vm-tools cloud-init` (Kali desktop:
     `open-vm-tools-desktop`).
   - Rocky: `dnf install -y open-vm-tools cloud-init`.
4. Copy `files/linux/99-vmware.cfg` to `/etc/cloud/cloud.cfg.d/`. This sets the VMware
   datasource, which TrueNorth's guestinfo IP config needs.
5. Run `files/linux/cleanup.sh`. It cleans cloud-init, truncates the machine-id, removes
   the SSH host keys and powers off.
6. Convert to a template and name it after the catalogue id. Then register it (§7).

### pfSense

1. New VM: FreeBSD 14 64-bit, BIOS, 2 vCPU / 2 GB / 20 GB.
   **Two vmxnet3 NICs**: NIC 1 (WAN) on `dPG-TN-BUILD` for the build, NIC 2 (LAN) on the
   same network.
2. Install with the defaults (ZFS or UFS) and reboot. Assign interfaces `vmx0` = WAN,
   `vmx1` = LAN.
3. In the web UI: System → Package Manager → install **Open-VM-Tools**. Enable SSH. Change
   no other configuration; each range applies its own rules at deploy (`http/pfsense/config.xml`
   is the bootstrap).
4. Shut down, convert to template `pfsense`, register.

At deploy, TrueNorth puts WAN on the range's uplink network and LAN on the range VLAN.
pfSense owns `.1`, and egress is denied by default.

### Security Onion 2.4

1. New VM: Oracle Linux 9 / RHEL 9 64-bit. **4 vCPU / 16 GB / 200 GB** (anything smaller
   fails SO's checks). Two vmxnet3 NICs: management and sniff.
2. Boot the SO ISO and **install the OS only**. When `so-setup` starts, exit it.
3. `dnf install -y open-vm-tools`. Shut down. Convert to template `securityonion` and register.
4. At deploy, the sniff NIC goes on the range's monitoring port group. TrueNorth enables
   promiscuous mode on port groups named monitor/span only. Run `so-setup` (standalone or
   eval) per range, or bake an eval install and snapshot it as a role snapshot.

### VyOS 1.4

1. New VM: Debian 12 64-bit, 1 vCPU / 2 GB / 8 GB, two vmxnet3 NICs.
2. Boot the ISO and log in as `vyos`/`vyos`. Run `install image` and accept the defaults.
   Reboot without the ISO.
3. VMware tools are built in. `configure; delete interfaces ethernet eth0 hw-id; delete interfaces ethernet eth1 hw-id; commit; save`,
   so that clones don't pin the template's MACs.
4. Shut down. Convert to template `vyos` and register.

## 4. Derived images

These are clones of a base with software layered on:

| Image | Base | Extras |
|---|---|---|
| remnux | ubuntu-lts | REMnux installer |
| sift | ubuntu-lts | SANS SIFT via `cast` |
| svc-emulators | ubuntu-lts | bind9, postfix, nginx, chrony (fake internet services) |
| ca-host | ubuntu-lts | step-ca |
| usersim | ubuntu-lts | GHOSTS server |
| cloudlog-emu | ubuntu-lts | log emulation |
| c2-server | ubuntu-lts | Sliver + Mythic. **Disabled** in the catalogue until instructor/Standards sign-off |
| precomp-host | win10-22h2 | staged artifacts |
| detonation-host | win10-22h2 | no sensor, no egress ever |

**With Packer:** `./build.sh remnux sift …`. These builds use `base_ubuntu_template` /
`base_win10_template`, so set them to the inventory template names. For example,
`base_ubuntu_template = "tmpl-ubuntu-2404"` uses the template that already exists.

**By hand:**
1. Clone the base template to a VM on `dPG-TN-BUILD`.
2. Run the matching `files/linux/roles/<name>.sh`.
3. Run `cleanup.sh`.
4. Convert to a template with the catalogue name and register it.

## 5. Custom images (variants)

A **variant** is a base template with extra software baked in: say a Windows 11 analyst
workstation, or an Ubuntu web server. Ranges have no egress, so software that has to be
on a host goes into the template; it cannot be installed at deploy. A variant becomes its
own template and its own golden image, so it shows up in the Range Designer's OS list.

Variants live in `infra/vsphere/packer/variants/`, one file each:

| Variant | Base | Contents |
|---|---|---|
| `win10-office` | win10-22h2 | Adobe Reader, Chrome, Firefox ESR, 7-Zip, VLC, Notepad++; Office LTSC from the depot if staged there |
| `win11-analyst` | win11-24h2 | Wireshark, Sysinternals, NetworkMiner, Autopsy, Ghidra, VS Code, Git, Python (4 vCPU / 8 GB / 80 GB) |
| `win11-dev` | win11-24h2 | VS Code, Git, Python, Node.js LTS, 7-Zip, Chrome |
| `ubuntu-webstack` | ubuntu-lts | nginx, PHP-FPM, MariaDB, with nginx wired to PHP-FPM |

### Write a variant

Copy the closest example to `variants/<name>.pkrvars.hcl`. `<name>` is lowercase letters,
digits and hyphens. It becomes the template name, the Content Library item and the
golden-image id.

```hcl
# variants/win11-soc.pkrvars.hcl
variant_name        = "win11-soc"
variant_os_family   = "windows"            # windows | linux
variant_base        = "win11-24h2"         # inventory template to clone; build it first
variant_version     = "11 24H2"
variant_description = "Windows 11 SOC seat: Wireshark, Sysinternals, Notepad++"
variant_os_aliases  = ["windows-11-soc"]   # extra designer/topology names (optional)

variant_choco_packages = ["wireshark", "sysinternals", "notepadplusplus@8.7.1"]  # id or id@version
variant_scripts        = ["my-extra-setup.ps1"]  # optional, under variants/scripts/
# variant_cpus = 4    variant_ram_mb = 8192    variant_disk_mb = 81920   (0 keeps the base's)
```

- **Keep each metadata line (`variant_name` to `variant_os_aliases`) on one line.**
  `build.sh` reads those lines to register the image.
- **Windows** packages come from Chocolatey. When `depot_url` is set, the depot's
  `/chocolatey` feed is the only source, so mirror the packages there first. Otherwise
  the community feed is used, through the build network's egress. Pin a version with
  `id@version`.
- **Linux** packages (`variant_apt_packages`) use apt on Ubuntu/Debian bases and dnf on
  Rocky.
- **Scripts** run as Administrator (`.ps1`) or root (`.sh`) after the packages, in the
  order you list them. `DEPOT_URL` and `VARIANT_NAME` are set in their environment.
  Anything specific to one range (users, flags, artifacts) is deploy-time and does not go here.
- A package that fails to install **fails the build**. A variant missing its software
  would otherwise only be noticed inside a range, where nothing can be fixed.

### Build it

```bash
cd infra/vsphere/packer
./build.sh list                  # bases, then variants with their base
./build.sh variant win11-soc     # one or more
./build.sh variants              # every file in variants/
```

`build.sh` checks that the base template exists in vCenter (when `govc` and `GOVC_URL` are
set), then runs `packer build -var-file=variants/<name>.pkrvars.hcl`. A Windows variant
clones the sysprepped base and gets WinRM back through vSphere guest customization.
That is the same Sysprep customization TrueNorth applies at deploy. Packer then installs
the packages, runs your scripts, reboots and sysprep-generalizes again, so the variant
is as clean a template as its base. A Linux variant SSHes in as the build user, installs,
and runs `cleanup.sh`. Logs: `logs/variant-<name>-<timestamp>.log`.

### Register it

With `TN_API_URL` and `TN_API_TOKEN` (a token with `infra:write`) exported, a successful
build registers the variant by itself. To register one built earlier, or by hand:

```bash
./build.sh register win11-soc
```

This calls `POST /api/golden-images`, which creates the image or updates it in place:

```json
{"catalogue_id": "win11-soc", "os_family": "windows", "version": "11 24H2",
 "role": "Windows 11 SOC seat: ...", "hypervisor": "vsphere", "template_name": "win11-soc",
 "os_aliases": ["windows-11-soc"], "build_status": "built", "enabled": true}
```

Variant images are marked as such. Re-importing `vm_iso_catalogue.csv` never deletes or
overwrites them, and a variant cannot reuse a catalogue image's id (409). To retire one,
`PATCH /api/golden-images/{id}` with `{"enabled": false}`.

### Choose it in the Range Designer

The designer's OS list comes from `GET /api/golden-images/alias-map`: every enabled image,
by name and by alias. Reload the designer after registering. `win11-soc` (and
`windows-11-soc`) then appear in a node's OS field. Ranges resolve the name to the
template at provision time. Check with:
`GET /api/golden-images/resolve?os=win11-soc&hypervisor=vsphere`.

## 6. Role snapshots — Exchange, SharePoint, SQL, AD CS (build sheet §0 "R")

These products can't be sysprepped after install and need a domain. The model:
1. Build a **golden domain** once, on an isolated range VLAN (or let TrueNorth provision a
   small range):
   - DC from `srv2022`, then promote it.
   - Members from `srv2022` / `srv2019`, then join them.
2. Install the products from the role media on `esx01-local/ISO`:
   - SQL 2022 → SharePoint 2019 on **srv2019** (2019 is not supported on Server 2022).
   - Exchange (prepare the schema from the DC first).
3. Shut the whole set down and snapshot every VM. The set is cloned as a unit into each
   range that needs it. Domain SIDs stay identical inside a range, which is fine because
   ranges are isolated.

This is a hands-on build. Expect most of a day per product, and do it after the basic
templates exist.

## 7. Register in TrueNorth

One time only:
```
POST /api/golden-images/import-catalogue?hypervisor=vsphere
```

For each image built:
```
PATCH /api/golden-images/{id}   {"template_name": "<name in vCenter>", "build_status": "built"}
```

`build.sh` does this automatically when `TN_API_URL` / `TN_API_TOKEN` are set. Variants
register through `POST` instead (§5). To check:
`GET /api/golden-images/resolve?os=windows-server-2022&hypervisor=vsphere` must return
your template.

## 8. Build order and time budget

| Order | Image | Why | Rough time |
|---:|---|---|---|
| 1 | ubuntu-lts | register the existing `tmpl-ubuntu-2404` | 5 min |
| 2 | srv2022 | used by every range template | 1–2 h |
| 3 | win11-24h2 | user workstations | 1–3 h (manual fallback likely) |
| 4 | pfsense | every range's gateway (upload the ISO first) | 30–60 min |
| 5 | kali | attacker seat | 1 h |
| 6 | securityonion | sensor (upload the ISO first) | 1 h |
| 7 | rocky, debian13 | Linux variety | 30 min each |
| 8 | win10-22h2, srv2019, srv2016 | after uploading their ISOs | 1–2 h each |
| 9 | srv2025, vyos, parrot | as needed | — |
| 10 | derived images and variants, then role snapshots | — | days |

With items 1–6 done, soc-training, red-team and small-enterprise ranges can deploy.
