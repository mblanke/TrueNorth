# TrueNorth Range — vSphere Packer template library

Self-contained Packer library that builds the TrueNorth golden-image catalogue on the
lab's vSphere estate and publishes each template to a vCenter **Content Library** as an
OVF item named after its catalogue id, so the TrueNorth `vsphere_api` provisioner can
deploy it by name. Base builds also leave an **inventory template** behind so the derived
(`vsphere-clone`) images have something in inventory to clone from.

Everything lives in this one directory. Packer loads every `*.pkr.hcl` here together:
shared variables are declared once in `variables.pkr.hcl`, and every source has a unique
name, so you build one image at a time with `-only`.

## Layout

```
variables.pkr.hcl            all shared variables (declared once)
plugins.pkr.hcl              required_plugins (github.com/hashicorp/vsphere >= 1.4)
windows-common.pkr.hcl       locals: pvscsi driver paths, ISO dir
windows.pkr.hcl              srv2022, srv2025, srv2019, srv2016, win10-22h2, win10-ltsc,
                             win11-24h2
linux.pkr.hcl                ubuntu-lts, rocky, kali, debian13, parrot
appliances.pkr.hcl           pfsense, securityonion, vyos (keystroke builds)
derived.pkr.hcl              vsphere-clone: remnux, sift, svc-emulators, greyspace-host, ca-host, usersim,
                             cloudlog-emu, c2-server, precomp-host, detonation-host
variants.pkr.hcl             vsphere-clone: variant-windows, variant-linux (custom images)
variants/<name>.pkrvars.hcl  one custom image each: base + packages + scripts
variants/scripts/            extra .ps1/.sh a variant can run
http/                        network-served seeds (kali, debian, parrot preseed)
  ubuntu/{user-data,meta-data}   autoinstall (delivered as a 'cidata' CD)
  rocky/ks.cfg                   kickstart (delivered as an 'OEMDRV' CD)
  pfsense/config.xml             pfSense bootstrap (vmx0/vmx1); applied at deploy
files/windows/               autounattend.pkrtpl.hcl, setup.ps1, sysprep.ps1, sysprep-unattend.xml,
                             variant-packages.ps1
files/linux/                 99-vmware.cfg, cleanup.sh, roles/<name>.sh, variant-packages.sh
build.sh                     build driver (order, logging, register-back)
lab.auto.pkrvars.hcl.example copy to lab.auto.pkrvars.hcl (git-ignored) and fill in
```

## Prerequisites

- **Packer >= 1.11** (tested with 1.16). `packer init .` once to fetch the vSphere plugin
  (needs internet; the lab build host TN-BUILD01 has egress).
- **govc** (optional but recommended) for the one-time Content Library creation and for
  checking datastore ISO names.
- A build host that can reach vCenter/ESXi **and** can be reached back by the build VM
  (WinRM 5985 for Windows, SSH 22 for Linux, and Packer's HTTP server for the preseed
  builds). In the lab that host is **TN-BUILD01** on `dPG-TN-BUILD`. **Do not build on
  TN-MGMT01**, even though the deployment repo installed Packer there. The build sheet
  (§1, §8) keeps internet egress off the platform VM, and TN-BUILD01 is the only host
  allowed to have it.

## One-time setup

1. **Create the Content Library** (there is none yet). Use a local library on esx01 since
   the ISOs and templates live on `esx01-local`:

   ```sh
   export GOVC_URL=https://192.168.1.10 GOVC_USERNAME=administrator@vsphere.local \
          GOVC_PASSWORD=... GOVC_INSECURE=1
   govc library.create -ds esx01-local TrueNorth-Templates
   ```

2. **Copy the vars file** and fill in the secrets:

   ```sh
   cp lab.auto.pkrvars.hcl.example lab.auto.pkrvars.hcl
   $EDITOR lab.auto.pkrvars.hcl     # vcenter_password, windows_admin_password, ISO names
   ```

   The committed example already carries the lab discovery values (vCenter `192.168.1.10`,
   `DC-Lab`, build host `192.168.1.11` = esx01, `esx01-local`, ISOs in `ISO/`,
   `dPG-TN-BUILD`). Cross-check against `docs/deployment/vsphere-inventory.md` (produced by
   `scripts/vsphere-discover.py`) before a run.

3. **Upload the missing ISOs.** These are referenced by templates but are NOT on the
   datastore yet; their `iso_*` defaults are `MUST-UPLOAD-FIRST_…` so a build fails loudly
   until you upload and set the real name: **Windows 10, Server 2019, Server 2016, pfSense,
   Security Onion, VyOS**.

## Filling `lab.auto.pkrvars.hcl`

The variables you must set per site:

| Variable | What |
|---|---|
| `vcenter_server`, `vcenter_username`, `vcenter_password` | vCenter SSO (password is a secret — never commit) |
| `vsphere_host` | **esx01** — the host that owns the ISO datastore. Required; the ISOs are host-local, so a cluster-scheduled build on another host would not see them |
| `vsphere_datastore`, `iso_datastore` | `esx01-local` |
| `iso_folder` | `ISO` |
| `iso_*` | exact ISO filenames on the datastore |
| `vmtools_iso_path` | VMware Tools `windows.iso` (ESXi default path works) |
| `build_network` | `dPG-TN-BUILD` (DHCP + reachability to the Packer host + WAN egress) |
| `content_library`, `publish_to_library` | `TrueNorth-Templates`, `true` |
| `windows_admin_password` | build-time Administrator password (reset by sysprep) |
| `ssh_username` / `ssh_password` | build-time Linux user (`ubuntu`/`ubuntu`; matches the seed) |
| `win11_vtpm` | `false` unless you have a vCenter key provider |
| `depot_url` | optional TN-DEPOT01 base URL to pull baseline apps from |

## Building

```sh
./build.sh list              # show templates in build order, then the variants
./build.sh all               # build every base + derived template (build-sheet order)
./build.sh srv2022 ubuntu-lts kali
./build.sh variant win11-analyst   # custom variants, see "Custom images (variants)"
./build.sh variants                # every variants/*.pkrvars.hcl
./build.sh register win11-analyst  # (re-)register a built variant with the API
```

`build.sh` runs `packer init` and `packer validate` once, then for each name runs
`packer build -force -only='*.<builder>.<name>' .`, logging to `logs/<name>-<ts>.log`.
The `*.` prefix matters: every `build {}` block here is named, and Packer 1.16 matches
nothing for a bare `-only=vsphere-iso.srv2022` (it warns and exits 0 having built
nothing). By hand, use `-only='*.vsphere-iso.srv2022'` or `-only=windows.vsphere-iso.srv2022`.

**Build order** (from the build sheet):
`srv2022 → win10-22h2 → ubuntu-lts → pfsense → securityonion → kali → win11-24h2 →
srv2019 → srv2016 → rocky → vyos`, then `srv2025 → debian13 → parrot → win10-ltsc`, then
the derived images, then any variants (`./build.sh variants`, not part of `all`). Build
the Linux/Windows bases before their derived clones. The derived builds clone
`base_ubuntu_template` / `base_win10_template` from inventory, and each variant clones
its own `variant_base`.

### Register-back

After a successful build, if `TN_API_URL` and `TN_API_TOKEN` are set, `build.sh` looks the
image up by `catalogue_id` via `GET $TN_API_URL/golden-images?hypervisor=vsphere` and
`PATCH`es it to `{build_status:"built", template_name:"<name>"}`. The API sits behind nginx
at `/api/`, so:

```sh
export TN_API_URL=https://<platform-host>/api
export TN_API_TOKEN=<bearer token for a user with infra:write (admin / range_ops)>
```

The PATCH route is `infra:write`-gated; a read-only token will 403.

## Content Library vs inventory template (the derived-build decision)

`vsphere-clone` requires its `template` to resolve in **vCenter inventory** — it cannot
clone straight from a Content Library item. So every **base** build sets BOTH
`convert_to_template = true` (keeps an inventory template) AND, when
`publish_to_library = true`, a `content_library_destination` (publishes the OVF). This is a
valid combination for the vSphere plugin: the inventory template is the clone source for
the derived images, and the Content Library OVF is what the deploy provisioner consumes.
The deploy provisioner can use either, so keeping both costs only datastore space.

> The existing cloud-image template **`tmpl-ubuntu-2404`** (already built in the deployment
> repo from the Ubuntu cloud OVA, no OS install) can be registered as `ubuntu-lts` right
> away and set as `base_ubuntu_template` — the fastest Linux path. The ISO-based
> `ubuntu-lts` build here is the from-scratch alternative. Never run
> `govc vm.markastemplate` against a vCenter-managed ESXi host (it desyncs inventory and
> creates ghost VMs); mark templates via vCenter only.

## Per-image notes and confidence

| Image | Builder | Confidence | Notes |
|---|---|---|---|
| srv2022 | iso | **Solid** | Classic Setup engine; honours Autounattend reliably. GVLK edition-select key. Build first |
| win10-22h2 | iso | **Solid** | Eval media (upload ISO first). No product key |
| win10-ltsc | iso | **Solid** (once ISO uploaded) | Enterprise LTSC 2021, 2 vCPU / 4 GB / 60 GB, classic engine. VL media use `gvlk_win10_ltsc`; eval media: set it to `""` and `win10_ltsc_image_name` to the eval image name. Catalogue row is `enabled=no` until built |
| ubuntu-lts | iso | **Solid** | 24.04.4 autoinstall via `cidata` CD. Or register `tmpl-ubuntu-2404` instead |
| srv2019 / srv2016 | iso | **Solid** (once ISO uploaded) | Eval media; classic engine |
| rocky | iso | **Solid** | Rocky 10 kickstart via `OEMDRV` CD |
| debian13 | iso | **Likely** | Debian installer preseed over Packer HTTP |
| kali | iso | **Likely** | Preseed over Packer HTTP; the `kali-linux-default` metapackage is online-only (best-effort, skipped offline) |
| srv2025 | iso | **Needs tuning** | New Setup engine; unattended UEFI partitioning is fragile (see below) |
| win11-24h2 | iso | **Needs tuning — highest risk** | New Setup engine + 64 GB + LabConfig bypass. See below |
| parrot | iso | **Fragile** | Live ISO usually installs via Calamares (GUI), which ignores preseed. Works only off the ISO's text/debian-installer entry; else treat as manual |
| pfsense | iso | **Fragile (keystroke)** | Blind `boot_command`; timing needs on-site tuning. config.xml applied at deploy |
| securityonion | iso | **Fragile (keystroke)** | Base OS install only; `so-setup` deferred to deploy. Finish the OS install by hand the first time, then template |
| vyos | iso | **Fragile (keystroke)** | `install image` driven blind over the console |
| remnux, sift, svc-emulators, ca-host, usersim, cloudlog-emu | clone | **Likely** | Clone `ubuntu-lts`, run `files/linux/roles/<name>.sh` over SSH. Role installers have honest `TODO(offline)` / `TODO(deploy)` where media is online-only or range-specific |
| greyspace-host | clone | **Not run on vSphere yet** | Clone `ubuntu-lts`, Docker >= 27 + every Greyspace stack image pulled by digest (`files/linux/roles/greyspace-host.sh`, needs internet or the depot as apt proxy and registry mirror). Proven: the stack on Docker (CI `greyspace`), gs-core on vcsim; not a real vCenter |
| c2-server | clone | **Gated** | Catalogue `enabled=no`; needs instructor/Standards sign-off |
| c2-server-cs | — | **Not built here** | Cobalt Strike image: production only, needs licensed media, never built in the lab |
| precomp-host, detonation-host | clone | **Partial** | Clone `win10-22h2` and template only (`communicator="none"`); `files/windows/<name>.ps1` is applied at deploy via a guest-customization spec. detonation-host stays sterile (no sensor, no egress). The variant WinRM mechanism below could bake these too; not switched over yet |
| variants | clone | **Likely** | `variants.pkr.hcl`; see "Custom images (variants)". Windows variants depend on vSphere guest customization succeeding on the sysprepped base |

## Custom images (variants)

A variant is a base template plus packages and scripts, baked into its own template and
registered as its own golden image so the Range Designer offers it. One file per variant,
`variants/<name>.pkrvars.hcl`, sets the `variant_*` variables (declared, with defaults, in
`variables.pkr.hcl`). Two generic sources in `variants.pkr.hcl` consume them:
`variant-windows` and `variant-linux`. Shipped examples: `win10-office`,
`win11-analyst`, `win11-dev`, `ubuntu-webstack`. How to write, build, register and pick
one: `docs/deployment/vm-build-guide.md` §5.

```sh
./build.sh variant win11-analyst
# by hand:
packer build -force -var-file=variants/win11-analyst.pkrvars.hcl \
  -only=variants.vsphere-clone.variant-windows .
```

**How a Windows variant gets WinRM back.** Every Windows base ends in
`sysprep /generalize /oobe /shutdown`. That disables the built-in Administrator on client
SKUs and leaves no password Packer knows, so a plain clone has nothing Packer can log in
to (this is why precomp-host/detonation-host are clone-only). `variant-windows` attaches a
vSphere **guest-customization spec** to the clone (`customize { windows_options { … } }`):
- `admin_password` = `windows_admin_password`, with `auto_logon` once. Naming
  Administrator for auto-logon re-enables the account.
- `run_once_command_list` (GuiRunOnce). At that logon it sets the network profile to Private,
  runs `winrm quickconfig`, and enables Basic and unencrypted auth plus a 5985 firewall rule.
  These match the base Autounattend's FirstLogonCommands.

Packer's WinRM communicator retries until the last command opens the listener. This is
the same Sysprep customization the deploy provisioner
(`control-plane/worker/worker/provisioners/vsphere_api.py`) applies to every Windows
clone of these templates, so any base that deploys can also be a variant base. It needs
VMware Tools in the base (setup.ps1 installs it). After the packages and scripts, a
`windows-restart` clears pending reboots. `shutdown_command` then runs `sysprep.ps1`, so
the variant is generalized again and deploys exactly like a base. Two alternatives were
rejected:
- A guestinfo-gated first-boot hook needs every Windows base rebuilt and leaves the
  password in the VMX extraConfig.
- `windows_sysprep_text` only duplicates what `windows_options` generates.

**Linux variants** clone the base and SSH in as `ssh_username`, the same way the Linux
derived images do. They install `variant_apt_packages` (apt, or dnf on Rocky) as root, run
the scripts, then `cleanup.sh`.

**Registration.** After a successful variant build, or on `./build.sh register <name>`,
`build.sh` reads the one-line metadata keys from the variant file and calls
`POST $TN_API_URL/golden-images`. That call creates or updates the image, marked as a
variant, so catalogue re-imports leave it alone. It needs `jq` and a token with
`infra:write`.

### Windows 11 / Server 2025 — highest-risk builds

The lab's lessons-learned: Win11 24H2+ and Server 2025 use the **new Windows Setup engine**
whose unattended UEFI disk path is fragile — a 3-partition layout hit a BFSVC `bootmgfw`
copy failure, and a 1-partition layout left no ESP ("error selecting this partition").
`files/windows/autounattend.pkrtpl.hcl` uses the proven **4-partition** layout (WinRE 300 MB
/ EFI 100 MB FAT32 / MSR 16 MB / Windows) that the widely used community Windows Packer
templates use. This is reliable on the classic engine (2022/2019/2016/10) and is the
best-effort automated path for the new engine.

**If an automated Win11/2025 build still fails at partitioning**, use the documented manual
fallback: install the OS once by hand into a VM (same specs), enable WinRM/OpenSSH,
run `files/windows/setup.ps1`, then `files/windows/sysprep.ps1` to generalize, and capture
that VM as the template (mark via vCenter). This is the method of record in the deployment
repo for Windows. The full step-by-step lives in `docs/deployment/vm-build-guide.md`.

## Network requirements

`build_network` must have **DHCP** and be able to reach the Packer host for:
- HTTP-served seeds (**kali, debian, parrot**) on `http_port_min..max` (8800–8899);
- **WinRM** (5985) callbacks from Windows builds;
- **SSH** (22) callbacks from Linux builds and the derived clones.

In the lab, `dPG-TN-BUILD` (proposed VLAN 31) meets these requirements. It also has the WAN
egress that `packer init` and the online package steps need. Its firewall rules are in
`docs/deployment/vmware-site-runbook.md` §4. Do not build on:
- `dPG-TN-MGMT`, which holds the platform
- `dPG-TN-SVC`, where the depot is and which has no egress
- the range port groups, which have no egress

## Windows product keys (deferred)

The lab's Server 2022/2025 and Windows 11 ISOs are retail/volume media, so the Autounattend
passes Microsoft's **public** KMS client setup key (GVLK) to pick the edition. Activation is
paused. KMS (TN-KMS01) is future integration and testing. Until then, templates and clones
run unactivated. This departs from the build sheet's "no keys in any template" rule and is
recorded as an open item in `docs/vm-build-sheet.md` §8.

## Validation

`packer init . && packer fmt -check -recursive . && packer validate .` should all pass with
a filled var file. CI (`.github/workflows/packer-build.yml`, job `vsphere-validate`) runs
`init` + `fmt -check` + `validate -syntax-only` on every change, with no secrets.
