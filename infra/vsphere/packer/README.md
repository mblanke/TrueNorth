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
roles.pkr.hcl                vsphere-clone of srv2022: srv2022-exchange2019, srv2022-sharepointse,
                             srv2022-mecm (server role images; see "Role images")
variants.pkr.hcl             vsphere-clone: variant-windows, variant-linux (custom images)
variants/<name>.pkrvars.hcl  one custom image each: base + packages + scripts
variants/scripts/            extra .ps1/.sh a variant can run
http/                        Linux seeds, all templatefiles (*.pkrtpl.hcl) rendered with the
                             generic build login and apt_proxy (local.linux_seed)
  ubuntu/user-data.pkrtpl.hcl    autoinstall (cd_content on a 'cidata' CD, with meta-data)
  rocky/ks.cfg.pkrtpl.hcl        kickstart (cd_content on an 'OEMDRV' CD)
  {kali,debian,parrot}/preseed.cfg.pkrtpl.hcl   preseed (http_content)
  pfsense/config.xml             pfSense bootstrap (vmx0/vmx1); applied at deploy
files/windows/               autounattend.pkrtpl.hcl, choco-bootstrap.ps1, setup.ps1, sysprep.ps1,
                             sysprep-unattend.xml, variant-packages.ps1, role-image.ps1
files/linux/                 99-vmware.cfg, apt-proxy.sh, cleanup.sh, roles/<name>.sh,
                             variant-packages.sh
build.sh                     build driver (order, logging, register-back)
lab.auto.pkrvars.hcl.example copy to lab.auto.pkrvars.hcl (git-ignored) and fill in
```

## Prerequisites

- **Packer >= 1.11** (tested with 1.16). `packer init .` once to fetch the vSphere plugin
  (needs internet on the host running Packer, never on the build network; at the work site
  `build-phase.sh prereq` runs it on TN-MGMT01, over its management NIC).
- **govc** (optional but recommended) for the one-time Content Library creation and for
  checking datastore ISO names.
- A build host that can reach vCenter/ESXi **and** can be reached back by the build VM
  (WinRM 5985 for Windows, SSH 22 for Linux, and Packer's HTTP server for the preseed
  builds). At the work site (ADR 0012) that host is **TN-MGMT01**: it builds on esx04
  from a separate checkout, with its callbacks on `ens224`, 10.30.31.20 on the isolated
  `dPG-TN-BUILD` (VLAN 31, 10.30.31.0/24, **no gateway, no internet**; TN-MGMT01 serves
  DHCP .100–.199). Builds reach TN-DEPOT01 on its BUILD address, 10.30.31.30 (Nexus 8081,
  apt-cacher-ng 3142); its MGMT address is 10.30.30.30, and the depot is the only machine
  with internet egress. See `install/lab/README.md`, "Work site (TN-MGMT01 build host)".
  _Superseded (noted 2026-10-19, #212):_ before ADR 0012 this said to build on a separate
  **TN-BUILD01** (10.30.31.10), "the only host with internet egress", and never on
  TN-MGMT01; that design also put the depot at 10.30.32.10 on `dPG-TN-SVC`.

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
| `build_network` | `dPG-TN-BUILD` (DHCP + reachability to the Packer host; no WAN egress, packages come from the depot) |
| `content_library`, `publish_to_library` | `TrueNorth-Templates`, `true` |
| `windows_admin_password` | build-time Administrator password (reset by sysprep) |
| `ssh_username` / `ssh_password` / `ssh_password_hash` | the one generic Linux build login (ADR 0012 decision 5), created by every Linux seed. The hash is `openssl passwd -6` of the password (`scripts/lab/pkrvars-work.py` writes it); defaults `ubuntu`/`ubuntu` |
| `win10_image_name` + `gvlk_win10`, `srv2019_image_name` + `gvlk_srv2019` | edition select for Visual Studio media (defaults: Windows 10 Enterprise / Server 2019 Standard Desktop Experience and their public GVLKs). Evaluation ISOs: `win10_eval = true` / `srv2019_eval = true` |
| `win11_vtpm` | `false` unless you have a vCenter key provider |
| `depot_url` | TN-DEPOT01 Nexus base, `http://<depot>:8081/repository`. Windows: Chocolatey is bootstrapped from the `chocolatey` .nupkg in `<depot_url>/chocolatey` (`choco-bootstrap.ps1`; Nexus serves no `install.ps1`) and every package comes from that feed. Vendor files: `<depot_url>/installers/...` (raw repository). Empty: offline |
| `apt_proxy` | apt-cacher-ng on the depot, `http://<depot>:3142`: the apt/dnf forward proxy for the Ubuntu autoinstall (`apt: proxy`), the Debian/Kali/Parrot preseeds (`mirror/http/proxy`), Rocky (`dnf proxy=`, repos on the http baseurl) and every derived/variant clone (`files/linux/apt-proxy.sh`). `cleanup.sh` removes it, so no template ships a proxy. Empty: offline |
| `iso_exchange2019`, `iso_sharepointse`, `iso_mecm`, `iso_sql2022` | media for the server role images |

The templates keep these build logins (ADR 0012 §5). Give the installer the same values
(`vault_template_windows_admin_password`, `tn_template_linux_user`,
`vault_template_linux_password`; install/README.md "Range credentials") so staff see them as
the "template default" login of machines with no declared account.

## Building

```sh
./build.sh list              # show templates in build order, then the variants
./build.sh all               # build every base + derived template (build-sheet order)
./build.sh srv2022 ubuntu-lts kali
./build.sh variant win11-analyst   # custom variants, see "Custom images (variants)"
./build.sh variants                # every variants/*.pkrvars.hcl
./build.sh register win11-analyst  # (re-)register a built variant with the API
./build.sh replicate srv2022       # copy a built template to every range host (below)
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

### Host-local copies (`replicate`)

The range hosts have local VMFS datastores only, so a clone from a template on another
host copies its disks across the network (20–40 GB per Windows VM). ADR 0012 decision 6
keeps a copy of each template on every range host:

```sh
export GOVC_URL=https://<vcenter> GOVC_USERNAME=... GOVC_PASSWORD=... GOVC_INSECURE=1
./build.sh replicate srv2022                 # esx02, esx03, esx04 (RANGE_HOSTS)
./build.sh replicate srv2022 esx03           # just one host
REPLICATE_AFTER_BUILD=1 ./build.sh srv2022   # build, register, then replicate
DRY_RUN=1 ./build.sh replicate srv2022       # print the govc changes only
```

For each host it clones the inventory template `<id>` powered off onto that host and its
datastore (`RANGE_DATASTORE_FMT`, default `%s-local`), as `<id>@<host>-<yyyymmdd>`, in the
template's own folder (or `TEMPLATE_FOLDER`), then `govc vm.markastemplate`. It is
idempotent: today's copy that exists is skipped, and one that was cloned but not yet
marked is marked. It refuses to run unless `GOVC_URL` is a vCenter (see the warning
below about ESXi). An unknown host or a template that is not built yet exits non-zero;
the other hosts are still copied.

The `vsphere_api` provisioner then clones, for each VM, the newest copy that resides on
the datastore of the host placement chose (`VSPHERE_PLACEMENT=spread`). The host's short
name is the first label of its vCenter name (`esx03.truenorth.lab` → `esx03`). With no
copy on that host it clones the plain `<id>` as before; it logs which one it used and
records it as `vms[].template` in the range's output. Copies older than the newest on a
host, that no undestroyed range records, are removed by the worker task
`worker.template_tasks.prune_template_copies`: a dry run by default (beat runs it daily at
03:30 UTC and reports), deleting only with `dry_run=false` or `TEMPLATE_COPY_PRUNE=delete`
on beat, and never while a range is provisioning.

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
| win10-22h2 | iso | **Solid** (once ISO uploaded) | VS "business editions" media: image `win10_image_name` (Windows 10 Enterprise) + public GVLK `gvlk_win10`. Evaluation media: `win10_eval = true` |
| win10-ltsc | iso | **Solid** (once ISO uploaded) | Enterprise LTSC 2021, 2 vCPU / 4 GB / 60 GB, classic engine. VL media use `gvlk_win10_ltsc`; eval media: set it to `""` and `win10_ltsc_image_name` to the eval image name. Catalogue row is `enabled=no` until built |
| ubuntu-lts | iso | **Solid** | 24.04.4 autoinstall via `cidata` CD. Or register `tmpl-ubuntu-2404` instead |
| srv2019 | iso | **Solid** (once ISO uploaded) | VS media, latest refresh: `srv2019_image_name` (Standard, Desktop Experience) + public GVLK `gvlk_srv2019`; some media name the image `Windows Server 2019 SERVERSTANDARD` (check with `dism /Get-WimInfo`). Evaluation media: `srv2019_eval = true` |
| srv2016 | iso | **Solid** (once ISO uploaded) | Eval media; classic engine |
| rocky | iso | **Solid** | Rocky 10 kickstart via `OEMDRV` CD |
| debian13 | iso | **Likely** | Debian installer preseed over Packer HTTP |
| kali | iso | **Likely** | Preseed over Packer HTTP; the `kali-linux-default` metapackage is online-only (best-effort, skipped offline) |
| srv2025 | iso | **Needs tuning** | New Setup engine; unattended UEFI partitioning is fragile (see below) |
| win11-24h2 | iso | **Needs tuning — highest risk** | New Setup engine + 64 GB + LabConfig bypass. See below |
| parrot | iso | **Fragile** | Live ISO usually installs via Calamares (GUI), which ignores preseed. Works only off the ISO's text/debian-installer entry; else treat as manual |
| pfsense | iso | **Fragile (keystroke)** | Blind `boot_command`; timing needs on-site tuning. config.xml applied at deploy |
| securityonion | iso | **Fragile (keystroke)** | Base OS install only; `so-setup` runs per range at deploy (standalone, airgap: `vmware-site-runbook.md` §7.2). Finish the OS install by hand the first time, then template **without** running setup. Check before templating: open-vm-tools is running (`systemctl is-active vmtoolsd`). The installer's user is the build login the worker uses (`VSPHERE_SO_INSTALL_USER`/`_PASSWORD`, documented in the wiki) and can `sudo`. Two NICs, management first |
| vyos | iso | **Fragile (keystroke)** | `install image` driven blind over the console |
| remnux, sift, svc-emulators, ca-host, usersim, cloudlog-emu | clone | **Likely** | Clone `ubuntu-lts`, run `files/linux/roles/<name>.sh` over SSH. Role installers have honest `TODO(offline)` / `TODO(deploy)` where media is online-only or range-specific |
| greyspace-host | clone | **Not run on vSphere yet** | Clone `ubuntu-lts`, Docker >= 27 + every Greyspace stack image pulled by digest (`files/linux/roles/greyspace-host.sh`, needs internet or the depot as apt proxy and registry mirror). Proven: the stack on Docker (CI `greyspace`), gs-core on vcsim; not a real vCenter |
| c2-server | clone | **Gated** | Catalogue `enabled=no`; needs instructor/Standards sign-off |
| c2-server-cs | — | **Not built here** | Cobalt Strike image: production only, needs licensed media, never built in the lab |
| precomp-host, detonation-host | clone | **Partial** | Clone `win10-22h2` and template only (`communicator="none"`); `files/windows/<name>.ps1` is applied at deploy via a guest-customization spec. detonation-host stays sterile (no sensor, no egress). The variant WinRM mechanism below could bake these too; not switched over yet |
| variants | clone | **Likely** | `variants.pkr.hcl`; see "Custom images (variants)". Windows variants depend on vSphere guest customization succeeding on the sysprepped base |
| srv2022-exchange2019, srv2022-sharepointse, srv2022-mecm | clone | **Not run yet; partial by design** | `roles.pkr.hcl`: prerequisites installed and media staged; the domain-dependent setup is a deploy-time step. See "Role images". Catalogue rows `enabled=no` |

All Linux images (bases, derived, variants) log in with the one generic build login
(`ssh_username` / `ssh_password`), which every seed creates from `ssh_password_hash`.

## Role images

`roles.pkr.hcl` builds the images `control-plane/api/app/windows_roles.py` names for its
"image" roles. Each clones `srv2022` (`base_srv2022_template`), gets WinRM back with the
same guest customization the Windows variants use, attaches its media as extra CD-ROMs,
and runs `files/windows/role-image.ps1` twice with a reboot in between, then sysprep.
`depot_url` is required. Files that are not on the product media come from the depot's raw
`installers` repository, `<depot_url>/installers/roles/<image>/`. Upload them once as the
`tn-prefetch` user, for example
`curl -u tn-prefetch --upload-file vcredist2012_x64.exe http://10.30.30.30:8081/repository/installers/roles/exchange2019/vcredist2012_x64.exe`.

| Image | Media (CD-ROMs) | Depot files (`installers/roles/…`) | Installed at build | Left to deploy time (needs the range domain) |
|---|---|---|---|---|
| srv2022-exchange2019 | `iso_exchange2019` (CU15) | `exchange2019/vcredist2012_x64.exe`, `vcredist2013_x64.exe`, `rewrite_amd64_en-US.msi`; optional `ndp48-x86-x64-allos-enu.exe`, `UcmaRuntimeSetup.exe` | Exchange 2019 Windows features (Microsoft Learn, "Exchange Server prerequisites", Server 2022 Desktop Experience list), VC++ 2012/2013, URL Rewrite, UCMA 4.0 from `\UCMARedist`; media in `C:\TrueNorth\media\exchange2019` | `Setup.exe /PrepareSchema`, `/PrepareAD`, `/Mode:Install /Roles:Mailbox`: Exchange Setup needs a domain member and Enterprise/Schema Admin |
| srv2022-sharepointse | `iso_sharepointse` | `sharepointse/VC_redist.x64.exe` | VC++ 2015-2022, `prerequisiteinstaller.exe /unattended` (run twice); media in `C:\TrueNorth\media\sharepointse` | `setup.exe /config` with the site's product key (no keys in templates, ADR 0012 decision 3), then `psconfig` against the range's SQL Server |
| srv2022-mecm | `iso_mecm`, `iso_sql2022`, `iso_srv2022` (for .NET 3.5 `sources\sxs`) | `mecm/adk.zip`, `adkwinpe.zip` (`adksetup /layout` output, zipped); optional `msodbcsql18.msi`, `configmgr-prereqs.zip` (setupdl output) | .NET 3.5/4.8, BITS/IIS/RDC site-server features, ADK + WinPE add-on, SQL Server 2022 Database Engine as a SysPrep prepared image (`/ACTION=PrepareImage`); ConfigMgr media in `C:\TrueNorth\media\mecm` | SQL `/ACTION=CompleteImage`, the AD schema extension (`extadsch`) and System Management container, ConfigMgr `setup.exe /SCRIPT` |

`C:\TrueNorth\role-image.json` in each image lists what was installed and the deploy-time
commands. TrueNorth does not run those steps yet, so the catalogue rows stay `enabled=no`:
an enabled row resolves before it is built, and the worker would report the role as
installed. There is no Packer source for `srv2022-sql2022` or `srv2019-sharepoint2019` yet
(their ISOs are listed in `install/lab/work-site/isos.conf`).

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

At the work site, `dPG-TN-BUILD` (VLAN 31, 10.30.31.0/24) meets these requirements:
TN-MGMT01 serves its DHCP (.100–.199) and the callbacks on 10.30.31.20. It has **no gateway
and no internet** (ADR 0012). Package steps go through TN-DEPOT01 on 10.30.31.30
(`depot_url`, `apt_proxy`); `packer init` runs on TN-MGMT01 over its management NIC, not
on the build network. Addresses and egress: `docs/deployment/vmware-site-runbook.md` §4.1a.
_Superseded (noted 2026-10-19, #212):_ this paragraph used to say `dPG-TN-BUILD` had the
WAN egress that the online package steps need, and that the depot sat on `dPG-TN-SVC`;
both belong to the pre-ADR 0012 TN-BUILD01 design. Do not build on:
- `dPG-TN-MGMT`, which holds the platform
- `dPG-TN-SVC`, if it exists (range WANs in the earlier design; no egress)
- the range port groups, which have no egress

## Windows product keys (deferred)

The lab's Server 2022/2025 and Windows 11 ISOs are retail/volume media, and Windows 10 22H2
and Server 2019 come from Visual Studio business media, so the Autounattend passes
Microsoft's **public** KMS client setup key (GVLK) to pick the edition. The GVLKs in
`variables.pkr.hcl` are from Microsoft Learn, "Key Management Services (KMS) client
activation and product keys"
(<https://learn.microsoft.com/windows-server/get-started/kms-client-activation-keys>,
checked 2026-10-19). Activation is
paused. KMS (TN-KMS01) is future integration and testing. Until then, templates and clones
run unactivated. This departs from the build sheet's "no keys in any template" rule and is
recorded as an open item in `docs/vm-build-sheet.md` §8.

## Validation

`packer init . && packer fmt -check -recursive . && packer validate .` should all pass with
a filled var file. CI (`.github/workflows/ci.yml`, job `packer-vsphere`, Packer 1.16.0) runs
`packer init`, `packer fmt -check -recursive` and `packer validate -syntax-only` on every
push and pull request to `main`, with no secrets and no vCenter, so variable values,
ISO names and the builds themselves are not checked there. (`packer-build.yml` is the
older, manually dispatched Proxmox workflow; it does not touch this directory.)
`tests/contracts/test_packer_replicate.py` runs `build.sh replicate` against a stub govc.
