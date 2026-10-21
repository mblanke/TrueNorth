# TrueNorth lab — build and depot plane (TN-BUILD01, TN-DEPOT01)

Brings up the two VMs from runbook §4.1a
([`docs/deployment/vmware-site-runbook.md`](../../docs/deployment/vmware-site-runbook.md))
and fills the depot so ranges can install software with no internet:

| VM | Port group | IP | Role |
|---|---|---|---|
| TN-BUILD01 | `dPG-TN-BUILD` (VLAN 31) | 10.30.31.10 | Packer, govc, Ansible. The only host with standing internet egress |
| TN-DEPOT01 | `dPG-TN-SVC` (VLAN 32) | 10.30.32.10 | Nexus (Chocolatey feed, PyPI, Docker, raw installers) + apt-cacher-ng. What ranges reach |
| | uplink NIC on `dPG-TN-BUILD` | 10.30.31.11 | **Disconnected** except during an install/prefetch window |

This is separate from `install/` (which targets TN-MGMT01 only) and has its own
inventory, vault and `ansible.cfg`. Run everything below from the repo root unless
a `cd` says otherwise.

**The work site has no TN-BUILD01**: TN-MGMT01 builds the templates there (ADR 0012).
See [Work site (TN-MGMT01 build host)](#work-site-tn-mgmt01-build-host) at the end.

> **Which design is current (noted 2026-10-19, #212).** Everything above "Work site" is
> the earlier TN-BUILD01 design: the depot at 10.30.32.10 on `dPG-TN-SVC` (VLAN 32), an
> uplink NIC on `dPG-TN-BUILD` opened only for prefetch windows, and `dPG-TN-BUILD` with a
> UniFi gateway and internet egress for TN-BUILD01. ADR 0012 and the work-site kit
> (#186, #187) replaced it on the work vCenter: `dPG-TN-BUILD` (VLAN 31, 10.30.31.0/24)
> has **no gateway and no internet**, TN-MGMT01 serves its DHCP (.100–.199) on
> 10.30.31.20, and TN-DEPOT01 sits at **10.30.30.30** (MGMT) and **10.30.31.30**
> (BUILD) and is the only machine with internet egress (apt-cacher-ng 3142, Nexus 8081).
> Since #214 (2026-10-20) it also has a third NIC at **10.30.32.10** on the range uplink
> network, for range firewalls only; the "Values for `install/inventory`" below are the
> work site's again. The rest of the tooling above is kept because the scripts still
> exist; do not use it for the work site.

## How the depot gets packages (read this first)

**The depot fetches upstream itself.** A Nexus proxy repository and apt-cacher-ng both
make the outbound request on the depot; a client asking through them never touches the
internet. TN-BUILD01 having egress does not help a proxy on another VM. `dPG-TN-SVC`
has no internet by design, so the depot has a second NIC on `dPG-TN-BUILD` that is
connected only for a window:

```
             window only                      always
internet <── TN-DEPOT01 nic1 (10.30.31.11)    TN-DEPOT01 nic0 (10.30.32.10) <── range pfSense WANs
                                                    ^
                                                    └── TN-BUILD01 asks through it (prefetch)
```

Outside the window two independent controls hold: the NIC is disconnected in vCenter
(`scripts/lab/depot-uplink.sh off`), and the caches are offline (apt-cacher-ng
`Offlinemode: 1`, every Nexus proxy repository `blocked`). Replies from 10.30.32.10 are
policy-routed back out nic0 (table 32), so the uplink's default route never carries
range traffic.

The alternative (hosted repositories filled only by pushes from TN-BUILD01, depot never
online) was rejected for apt/dnf: the worker uses the depot as an HTTP **forward proxy**
(below), and only a caching forward proxy can serve that. Chocolatey gets both: the
proxy, and every catalogue package (with dependencies) **uploaded into
`chocolatey-hosted`** from TN-BUILD01, so the offline feed never depends on the proxy's
query cache.

### What the worker expects (`control-plane/worker/worker/provisioners/vsphere_guest.py`)

| Guest | Command the worker runs | Needs |
|---|---|---|
| Windows | `choco install <id> -y --source <TN_DEPOT_CHOCO_FEED>` | a NuGet v2 feed: Nexus group `chocolatey` |
| Ubuntu | `apt-get -o Acquire::http::Proxy=<TN_DEPOT_APT_PROXY> …` | an HTTP forward proxy: apt-cacher-ng |
| Rocky | `dnf --setopt=proxy=<TN_DEPOT_APT_PROXY> install …` | the same forward proxy |

The guest keeps its stock sources and sends full upstream URLs to the proxy. A Nexus
apt/yum proxy repository is not a forward proxy (it needs `sources.list` rewritten), so
it is not used.

**Values for `install/inventory/group_vars/all/main.yml`** (→ `.env.production`):

| Variable | Value |
|---|---|
| `tn_depot_url` | `http://10.30.32.10` |
| `tn_depot_choco_feed` | empty → `http://10.30.32.10:8081/repository/chocolatey/` (or set it explicitly) |
| `tn_depot_apt_proxy` | **`http://10.30.32.10:3142`** — must be set: empty falls back to `tn_depot_url`, port 80, where nothing listens |

pfSense WAN rule: allow zone nets → 10.30.32.10 tcp/8081 and tcp/3142.

## Prerequisites

Control node: `govc`, `jq`, `bash`, Python 3 with `pyvmomi==8.0.3.0.1` (for the two
settings govc cannot make: port-group security policy and removing the clone's inherited
OVF/vApp config), and Ansible:

```bash
python3 -m venv ~/.venvs/tnlab && ~/.venvs/tnlab/bin/pip install ansible-core pyvmomi==8.0.3.0.1
export PATH=~/.venvs/tnlab/bin:$PATH
(cd install/lab && ansible-galaxy collection install -r requirements.yml)
```

vCenter credentials come from the environment only and are never printed:

```bash
export GOVC_URL=https://192.168.1.10/sdk GOVC_USERNAME=administrator@vsphere.local GOVC_INSECURE=1
read -rs GOVC_PASSWORD && export GOVC_PASSWORD
```

## Order of operations

Every script is **plan-only by default**; add `--apply` to change vCenter. Every step is
idempotent.

```bash
# 1. Port groups (refuses if VLAN 31/32 is in use) - then do the printed UniFi checklist by hand
scripts/lab/create-portgroups.sh
scripts/lab/create-portgroups.sh --apply

# 2. VMs (esx02-04 with most free RAM, that host's local datastore; never esx01)
scripts/lab/deploy-mgmt-vm.sh TN-BUILD01 --ssh-key ~/.ssh/id_ed25519_lab.pub --apply
scripts/lab/deploy-mgmt-vm.sh TN-DEPOT01 --ssh-key ~/.ssh/id_ed25519_lab.pub --data-disk 1T --apply

# 3. Secrets
cd install/lab
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
$EDITOR inventory/group_vars/all/vault.yml          # two long random passwords
ansible-vault encrypt inventory/group_vars/all/vault.yml

# 4. Build host (pin tn_build_repo_ref in inventory/group_vars/all/main.yml first)
ansible-playbook build.yml
ansible-playbook build.yml                          # clean second run = idempotent

# 5. Depot: open the window, install, (6) prefetch, close the window
../../scripts/lab/depot-uplink.sh on --apply
ansible-playbook depot.yml --ask-vault-pass

# 6. Warm the depot with content/catalogue/software_catalogue.yaml
ansible-playbook prefetch.yml --ask-vault-pass
../../scripts/lab/depot-uplink.sh off --apply
../../scripts/lab/depot-uplink.sh status            # connected=false startConnected=false

# 7. Point TrueNorth at the depot: set the three tn_depot_* values above in
#    install/inventory/group_vars/all/main.yml, plus the uplink variables (runbook §4.1a),
#    then from install/:  ansible-playbook playbooks/30-config.yml --ask-vault-pass
#    and restart the worker.
```

`depot-uplink.sh on` refuses while range VMs exist (`TrueNorth/Ranges` or the earlier `truenorth/ranges`) unless
`--force`: during a window a range could pull from upstream through the depot.

Repeat steps 5–6 (`on` → `prefetch.yml` → `off`) whenever the catalogue changes or
before a course, to refresh the caches.

## What each piece does

| File | What |
|---|---|
| `scripts/lab/create-portgroups.sh` | `dPG-TN-BUILD`/31 and `dPG-TN-SVC`/32 on `vDS-10G`, security policy reject/reject/reject, VLAN-in-use check, UniFi checklist |
| `scripts/lab/deploy-mgmt-vm.sh` | Clone `tmpl-ubuntu-2404`, size it, NICs, data disk, cloud-init via `guestinfo` (static IP, `tnadmin` + your public key, no passwords), power on, wait for the IP |
| `scripts/lab/depot-uplink.sh` | Connect/disconnect the depot's uplink NIC (found by its port group, not its position) |
| `scripts/lab/vim_helper.py` | pyVmomi: port-group security policy; remove a clone's OVF/vApp config |
| `build.yml` / `tn_build` | Packer (HashiCorp apt repo, key fingerprint checked), govc (release tarball, checksum-verified), ansible-core, git, jq, xorriso/genisoimage, qemu-utils, docker.io; TrueNorth checkout at a ref; `packer init` |
| `depot.yml` / `tn_depot` | Docker + Nexus OSS on `/srv/depot`; admin password from the vault; anonymous read; repos; upload-only `tn-prefetch` user; apt-cacher-ng |
| `prefetch.yml` / `tn_prefetch` | Online → warm from TN-BUILD01 (community packages, plus `content/choco` internalized by `scripts/lab/choco-internalize.py`) → offline (always) → verdict |

Nexus repositories (`http://10.30.32.10:8081/repository/<name>/`):

| Name | Type | Notes |
|---|---|---|
| `chocolatey` | nuget group | **The feed ranges use.** Members: `chocolatey-hosted`, then `chocolatey-proxy` |
| `chocolatey-hosted` | nuget hosted | Filled by `prefetch.yml`; ALLOW_ONCE |
| `chocolatey-proxy` | nuget proxy (V2) | community.chocolatey.org |
| `pypi-proxy` | pypi proxy | pypi.org |
| `docker-proxy` | docker proxy, port 8082 | Docker Hub |
| `docker-hosted` | docker hosted, port 8083 | |
| `installers` | raw hosted | Vendor media (build sheet §6); `<id>/<version>/` for offline Chocolatey packages |

Upload vendor media (from TN-BUILD01, credentials from the environment):

```bash
curl -fsS -u "tn-prefetch:$NEXUS_PASSWORD" --upload-file SQLServer2022-x64-ENU.iso \
  http://10.30.32.10:8081/repository/installers/sql/SQLServer2022-x64-ENU.iso
```

## Offline Chocolatey packages

Many community packages are *wrappers*: their `chocolateyInstall.ps1` downloads the
vendor installer at install time, so they fail in a range with no internet even though
the `.nupkg` is in the depot. `prefetch.yml` flags them `NEEDS-INTERNALIZING` in its
report. We internalize them ourselves, for free, from `content/choco/`:

| File | What |
|---|---|
| `content/choco/<id>.yaml` | One definition per package: `name` (the community id, so nothing else changes), pinned vendor `version`, `revision`, `type` (msi/exe/zip), `silent_args`, `valid_exit_codes`, `software_name`, `installers` (`url`, `sha256`, optional `file`, `arch: any\|x64`), optional `uninstall.silent_args` |
| `scripts/lab/choco-internalize.py` | Run by `tn_prefetch` on TN-BUILD01. Per definition: download, check sha256, upload to `installers/<id>/<version>/<file>`, generate the `.nupkg` (nuspec + `tools/chocolateyInstall.ps1` with `Install-ChocolateyPackage` and the checksum), push to `chocolatey-hosted` |

The generated install script downloads from `$env:TN_DEPOT_URL` (as in the platform
`.env`, e.g. `http://10.30.32.10`; port 8081 assumed) and falls back to the depot URL
baked in at build time (`tn_depot_nexus_url`). Nothing is fetched from the vendor in a
range. Covered today: `googlechrome`, `firefoxesr`, `adobereader`, `vscode`,
`powershell-core`, `vcredist140` (which also makes `wireshark` offline: its dependency),
`sysinternals`. The other catalogue packages embed their installers.

**Why ranges get our build and not the community one.** The package version is the
vendor version plus a fix segment, `<version>.<revision>` (`1.140.0` → `1.140.0.1`); a
4-segment vendor version folds the revision into its last segment
(`155.0.8059.26` → `155.0.8059.2601`, NuGet allows four). That sorts above the community
package of the same release, which earlier prefetch runs may already have put in
`chocolatey-hosted` (ALLOW_ONCE: it stays). Before pushing, the script lists the hosted
versions and **refuses** the package if any sorts above ours. The community prefetch is
given the internalized ids (`--skip`) and never fetches them, as packages or as
dependencies, so no newer wrapper reaches hosted or the proxy cache later; offline the
proxy is blocked anyway and the group lists hosted first. A newer community version
upstream is only reported (`NEWER-UPSTREAM`): time to update the definition.

**Add a package**

1. Read the community package's install script (`choco download <id>` or open the
   `.nupkg` from `https://community.chocolatey.org/api/v2/package/<id>`): it shows the
   vendor URL, the checksum and the silent arguments.
2. Write `content/choco/<id>.yaml` (copy a similar one). Prefer a *versioned* vendor URL.
   Use the community package's `version` numbering so ours sorts above it. Fill `sha256`
   from the vendor's published hashes, or leave it `""` with a TODO comment.
3. In `content/catalogue/software_catalogue.yaml` set `offline: true` on the entry
   (the id must be a catalogue Chocolatey id; `tests/api/test_software_catalogue_route.py`
   checks both).
4. Check locally: `python3 scripts/lab/choco-internalize.py --pack-only --baked-url
   http://10.30.32.10:8081 --workdir /tmp/tn-choco` builds every package with no network;
   `pytest tests/scripts/test_choco_internalize.py`.
5. Run the prefetch window (`depot-uplink.sh on` → `prefetch.yml` → `off`). A blank
   `sha256` is computed on TN-BUILD01 and printed as `RECORDED sha256 <file>=<hash>`
   (and in `/var/tmp/tn-prefetch/choco-internalize-report.json`): copy it into the
   definition and commit. (Run by hand with `--write-back` to edit the definition in the
   TN-BUILD01 checkout instead.)

**Update a version**

1. Change `version` (and the version in the URL, when it has one), set `revision: 1`,
   and put the new `sha256` (or `""`). For a floating URL (`googlechrome`,
   `sysinternals`: always the latest file) the old checksum stops matching the day the
   vendor ships: the package then fails with `sha256 mismatch`; that is the signal.
2. Same vendor version but a changed definition (silent args, uninstall)? Bump
   `revision` instead: `chocolatey-hosted` is ALLOW_ONCE and never replaces a version.
3. Run the prefetch window. The old version stays in hosted (harmless: ours sorts higher).

By hand on TN-BUILD01 (one package):

```bash
cd /opt/truenorth && export NEXUS_USER=tn-prefetch; read -rs NEXUS_PASSWORD; export NEXUS_PASSWORD
python3 scripts/lab/choco-internalize.py --nexus http://10.30.32.10:8081 --only vscode \
  --workdir /var/tmp/tn-prefetch/internalize --report /tmp/vscode.json
```

TN-BUILD01's own egress is used for the vendor downloads; the depot's uplink is not
needed for this step (it is for the rest of `prefetch.yml`).

## Known limits — read the prefetch report

- **Chocolatey wrapper packages** not yet in `content/choco/` still download the vendor
  installer at install time and fail offline. `prefetch.yml` lists them as
  `NEEDS-INTERNALIZING` (report: `/var/tmp/tn-prefetch/choco-report.json` on
  TN-BUILD01). Fix: add a definition (above), or bake the app into the Windows template
  (build sheet §5.1). The Range Designer marks catalogue entries without
  `offline: true` with a warning, and the worker logs one when it installs them.
- **Ubuntu `firefox-esr` and `code`** are not in the Ubuntu archive (Mozilla / Microsoft
  repos). They fail in prefetch and in ranges until those repos are added to the template
  *over plain HTTP* (apt-cacher-ng refuses CONNECT tunnels by design).
- **Rocky.** Stock repos use an HTTPS mirrorlist, which a forward proxy can only tunnel.
  The Rocky template must use the `http://dl.rockylinux.org` `baseurl` (exactly what
  prefetch uses), or its guests miss the cache. `p7zip`, `putty`, `vlc` need EPEL
  (`tn_prefetch_rocky_epel: true`, plus EPEL with an HTTP baseurl in the template).
- **Nexus version.** `sonatype/nexus3:3.70.4` is the last OSS line. 3.77+ (Community
  Edition) needs its EULA accepted and has usage caps; the role refuses to accept it unless
  a human sets `tn_depot_nexus_accept_eula: true`.
- **Nexus admin on 8081** is reachable from ranges (anonymous is read-only). Use a long
  random `vault_nexus_admin_password`.

## Work site (TN-MGMT01 build host)

The work vCenter (192.168.1.10, `DC-Lab` / `CL-Lab`, esx01-04, local VMFS only) has no
TN-BUILD01. ADR 0012 decides how its golden templates are built:
- **TN-MGMT01** (10.30.30.20) builds them on **esx04**, next to itself. Packer's HTTP, WinRM
  and SSH callbacks reach its second NIC `ens224`, 10.30.31.20 on `dPG-TN-BUILD` (VLAN 31,
  isolated, no gateway). `tn-build-dhcp.service` (dnsmasq) hands out 10.30.31.100-199.
- **TN-DEPOT01** has three NICs: 10.30.30.30 on `dPG-TN-MGMT` (gateway .1, DNS
  10.30.30.10), 10.30.31.30 on `dPG-TN-BUILD`, and 10.30.32.10 on the range uplink network
  (`dPG-TN-SVC`, VLAN 32; #214), where range firewall WANs reach it. It runs apt-cacher-ng
  on 3142 and the Nexus Chocolatey feed on 8081 (`tn_depot`). It never routes between its
  NICs, and on the range NIC its host firewall admits only TCP 8081/3142 from 10.30.32.0/24
  ([Ranges reach the depot](#ranges-reach-the-depot-214)).
- The ISOs are copied from `[esx01-local] ISO/` to `[esx04-local] ISO/`, keeping the same
  sub-folders.
- After each build, the template is copied to esx02-04 (`build.sh replicate`). A test clone
  on each host must take a customized name and IP. Only then is the template marked
  `built` in TrueNorth (decision 9).

**ADR 0012 was accepted on 2026-10-19** (owner sign-off). Until then it was `proposed`,
and this section said to run no build phase before sign-off. `status`, `prereq` and
`--dry-run` still change nothing that a build depends on.

| Piece | What |
|---|---|
| `install/lab/work-site/` | Inventory (TN-MGMT01 as `build`, TN-DEPOT01 as `depot`), group_vars, `ansible.cfg` (`roles_path = ../roles`), `vault.yml.example`. Run the shared playbooks from it: `ansible-playbook ../build.yml` |
| `install/lab/work-site/isos.conf` | The ISO list: Packer variable → path under `ISO/`. `PENDING` lines are skipped until filled in |
| `scripts/lab/pkrvars-work.py` | Writes `infra/vsphere/packer/lab.auto.pkrvars.hcl`: vCenter from `.env.production`, generic template credentials, esx04 / esx04-local / dPG-TN-BUILD / 10.30.31.20, ISO names, `--depot`. Mode 0600, root, never printed |
| `scripts/lab/iso-sync-esx04.sh` | Server-side copy esx01-local → esx04-local (`govc datastore.cp`), skips files of the same size; `--stream` copies through the host instead |
| `scripts/lab/build-phase.sh` | The phases below. y/N before every vCenter change unless `--yes`; one approval per build phase |
| `scripts/lab/test-clone.sh` | Clone `<id>` (the host's newest `<id>@<host>-<date>` copy) on each range host, customize name + IP, wait for Tools to report them, destroy |
| `scripts/register_golden_image_inprocess.py` | Marks the image `built` with its template name (`PATCH /golden-images/{id}`), or creates a variant, in-process as an administrator (no token) |
| `scripts/lab/deploy-mgmt-vm.sh TN-DEPOT01 --site work` | The depot on esx04, cloned from `ubuntu-lts`: MGMT and BUILD NICs addressed by cloud-init, the range uplink NIC (`--range-pg`, default `dPG-TN-SVC`) added connected but unaddressed |
| `scripts/lab/create-portgroups.sh --site work` | Creates only `dPG-TN-SVC` (VLAN 32, the range uplink network) on `vDS-10G`, with the reject/reject/reject security policy, and prints the work-site UniFi checklist |
| `install/lab/depot.yml` (`tn_depot`) | Also: forwarding off (sysctl, dockerd `ip-forward: false`, Nexus on host networking), then on the range NIC the `nft` table `inet tn_depot_range` and only then 10.30.32.10/24 |

`tn_build` is reused on TN-MGMT01 for only what is missing: govc, xorriso/genisoimage,
openssl, and Packer if `packer version` fails. It leaves Docker alone
(`tn_build_manage_docker_group: false`, no `docker.io`). The kit goes in its own checkout,
`/srv/truenorth/template-kit`, never in the installed app's `/srv/truenorth/app`.

### Operator sequence

```bash
# ── 1. Mac: TN-MGMT01 tooling and the kit checkout ─────────────────────
cd install/lab/work-site
ansible-galaxy collection install -r ../requirements.yml
$EDITOR inventory/group_vars/all/main.yml       # tn_work_admin_user: your sudo login on TN-MGMT01
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
$EDITOR inventory/group_vars/all/vault.yml      # two long random Nexus passwords
ansible-vault encrypt inventory/group_vars/all/vault.yml
ansible-playbook ../build.yml                   # govc, xorriso; rsync of this checkout
ansible-playbook ../build.yml                   # second run: no changes

# ── 2. TN-MGMT01: Packer variables (offline bases: no depot yet) ───────
ssh <you>@10.30.30.20
cd /srv/truenorth/template-kit
# Generic template credentials (ADR 0012 decision 5): TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD,
# TN_TEMPLATE_LINUX_USER, TN_TEMPLATE_LINUX_PASSWORD come from .env.production when the
# installer wrote them there. Otherwise you are prompted (passwords hidden).
sudo python3 scripts/lab/pkrvars-work.py
#   Any Linux user/password works: every Linux seed is a Packer templatefile, and the vars
#   file carries ssh_password_hash (openssl passwd -6). Nothing in the checkout is rewritten.

# ── 3. TN-MGMT01: prerequisites ───────────────────────────────────────
sudo scripts/lab/build-phase.sh status
sudo scripts/lab/build-phase.sh prereq          # packer init/validate, ISO copy (y/N),
                                                # DRS should-rule TN-MGMT01 -> esx04 (y/N)

# ── 4. Depot ──────────────────────────────────────────────────────────
#   First the range uplink port group, if the work site has none yet (TODO(site), #214):
scripts/lab/create-portgroups.sh --site work           # plan; then --apply, then its UniFi checklist
sudo scripts/lab/build-phase.sh depot --ssh-key /home/<you>/.ssh/id_ed25519_lab.pub --admin <admin upn>
#   builds + replicates + tests + registers ubuntu-lts if it is missing, deploys TN-DEPOT01
#   with three NICs (y/N), and step 3/5 checks the range NIC (adds or connects it after a
#   y/N each, prints its MAC). Another port group name: --range-pg <name>. Then, from the Mac:
#   (export TN_DEPOT_BUILD_READ_PASSWORD first, without printing it: "Ranges reach the depot", step 2)
cd install/lab/work-site && ansible-playbook ../depot.yml --ask-vault-pass -e tn_depot_range_mac=<MAC from step 3/5>
#   back on TN-MGMT01: re-run the depot phase; step 5/5 checks the depot on 10.30.31.30.
sudo scripts/lab/build-phase.sh depot --admin <admin upn>

# ── 5. Offline bases (still no depot_url) ─────────────────────────────
sudo scripts/lab/build-phase.sh base --admin <admin upn>
#   srv2022 ubuntu-lts win11-24h2 kali rocky debian13 parrot srv2025, plus win10-22h2 and
#   srv2019 once their ISOs are in (below)

# ── 6. Appliances: one heads-up each, watch the console in vCenter ────
sudo scripts/lab/build-phase.sh appliances --admin <admin upn>   # pfsense vyos securityonion

# ── 7. Depot-dependent images ─────────────────────────────────────────
sudo python3 scripts/lab/pkrvars-work.py --depot   # depot_url = http://10.30.31.30:8081/repository,
                                                   # apt_proxy = http://10.30.31.30:3142
sudo scripts/lab/build-phase.sh derived  --admin <admin upn>
sudo scripts/lab/build-phase.sh roles    --admin <admin upn>
sudo scripts/lab/build-phase.sh variants --admin <admin upn>
sudo scripts/lab/build-phase.sh status
```

**Pending media** (Visual Studio downloads): Windows 10 22H2 business editions, Server 2019
(latest refresh), Exchange 2019 CU15 (`ExchangeServer2019-x64-cu15.iso`), SharePoint
Subscription Edition, MECM. Put each file on `[esx01-local] ISO/<folder>/`, replace its
`PENDING` in `isos.conf` with the path, then:

```bash
sudo scripts/lab/build-phase.sh prereq                   # copies it to esx04-local
sudo python3 scripts/lab/pkrvars-work.py [--depot]       # writes iso_win10 / iso_srv2019 / iso_exchange2019 ...
sudo scripts/lab/build-phase.sh base --only "win10-22h2 srv2019" --admin <admin upn>
sudo scripts/lab/build-phase.sh roles --admin <admin upn>   # precomp-host, detonation-host clone win10-22h2;
                                                            # srv2022-exchange2019/-sharepointse/-mecm clone srv2022
```

The server role images also need their vendor prerequisites in the depot's raw
`installers` repository (`infra/vsphere/packer/README.md`, "Role images"). They install
prerequisites and stage the media only; Exchange, SharePoint and ConfigMgr setup need the
range's domain and stay a deploy-time step, so their catalogue rows stay `enabled=no`.
`iso_sql2022` (`enu_sql_server_2022_enterprise_edition_x64_dvd_aa36de9e.iso`) is on esx01
but its folder is not recorded yet: fill its `PENDING` line before building `srv2022-mecm`.

Every phase can be re-run. Existing templates are not rebuilt unless `--rebuild`, and
replicate skips today's copies. Registration skips images that are already built. A test
clone that fails is kept for diagnosis, and the script prints its `govc vm.destroy`
command. Use `--dry-run` to see what a phase would do. `--no-test` builds and replicates
but does not register.

### Ranges reach the depot (#214)

Owner decision 2026-10-20: TN-DEPOT01 gets a third NIC on the range uplink network at
**10.30.32.10**. Range firewalls keep their `TN_DEPOT` rule (pfSense/OPNsense alias and
floating WAN pass, VyOS address-group), and ranges never get a path to the management
network. The provisioner derives that rule from the installer's `TN_DEPOT_URL` and
`TN_DEPOT_PORTS`; nothing in the worker changed.

| Where | Setting | Value |
|---|---|---|
| vCenter | range uplink port group | `dPG-TN-SVC`, VLAN 32 (**TODO(site)**: not created yet; `--range-pg` / `TN_RANGE_UPLINK_PG` if another name is chosen) |
| UniFi | VLAN 32 | 10.30.32.0/24, gateway .1, no DHCP; drop 10.30.32.0/24 → internet, MGMT, BUILD, 192.168.1.0/24 |
| TN-DEPOT01 | NIC 2 | renamed `tnrange0` by its MAC (`tn_depot_range_mac`, never guessed), 10.30.32.10/24, no gateway, no DNS, netplan `activation-mode: manual` (`tn_depot_range_*` in `work-site/inventory/group_vars/all/main.yml`) |
| TN-DEPOT01 | host firewall (`nft` table `inet tn_depot_range`) | in: only TCP 8081, 3142 to 10.30.32.10, matched on `tnrange0` **and** on source 10.30.32.0/24 on any NIC; out: nothing new towards 10.30.32.0/24; forward: nothing. While the caches are online: nothing at all from the range network |
| TN-DEPOT01 | link control | only `tn-depot-range-link.service` brings `tnrange0` up (`Requires=` the firewall unit, checks the live table first); `tn-depot-range-guard.timer` re-checks every minute and sets it DOWN if the table is gone (e.g. Ubuntu's `nftables.service` flushed the ruleset; a drop-in re-loads it after that unit) or forwarding is on |
| TN-DEPOT01 | forwarding | off: `net.ipv4.ip_forward=0`, IPv6 forwarding 0, dockerd `"ip-forward": false` |
| TN-DEPOT01 | apt-cacher-ng | `ForceManaged: 1` (only Remap-ed repositories; not an open proxy, no SSRF to 10.30.30.0/24), `AllowUserPorts: 80`; maintenance pages behind `AdminAuth`, generated on the depot (`sudo grep AdminAuth /etc/apt-cacher-ng/security.conf`), never printed |
| TN-DEPOT01 | Nexus | anonymous: read/browse on the `chocolatey` group feed and on `installers/<choco id>/**` (content selector `tn-installers-choco`, generated from `content/choco/*.yaml`: what the in-house packages download without credentials); `installers/roles/**` and `installers/office/**` only as `tn-build-read` (Packer) |
| TN-MGMT01 | `/srv/truenorth/config/depot-build.env` | `TN_DEPOT_BUILD_READ_PASSWORD` (root, 0600): generated by `build-phase.sh depot`, given to `depot.yml` in the environment and to Packer by `pkrvars-work.py --depot` (`depot_user` / `depot_password`, sensitive). Never printed |
| installer | `tn_depot_url` (`TN_DEPOT_URL`) | `http://10.30.32.10` — scheme and IP only: the worker appends `:8081/repository/chocolatey/`, and the firewalls' `TN_DEPOT` alias is this host |
| installer | `tn_depot_choco_feed` | `http://10.30.32.10:8081/repository/chocolatey/` |
| installer | `tn_depot_apt_proxy` | `http://10.30.32.10:3142` |
| installer | `tn_depot_ports` (`TN_DEPOT_PORTS`) | `8081,3142` |
| installer | `tn_vsphere_range_uplink_pool` / `_gateway` / `_prefix` | `10.30.32.100-10.30.32.199` / `10.30.32.1` / `24` (unchanged) |
| installer | `tn_vsphere_range_uplink_network` | empty until step 4 below, then `dPG-TN-SVC` |
| Packer (builds) | `depot_url` / `apt_proxy` | `http://10.30.31.30:8081/repository` / `http://10.30.31.30:3142` — builds use the BUILD NIC, never 10.30.32.10 |

**Every online/offline switch briefly bounces the range NIC (fail closed).** Going online
(`prefetch.yml`, `depot.yml -e tn_depot_upstream_online=true`) and back offline reload
the range firewall (`tn-depot-range-fw.service`). `tn-depot-range-link.service`
`Requires=` it, so systemd restarts it too: `tnrange0` goes DOWN, the guard re-checks the
live table, and only then brings it UP again. That is a few seconds without the depot for
the range network, on top of the window itself, in which the range network gets nothing.
A range install running at that moment fails and has to be retried; run windows when no
range is deploying (the guard refuses while range VMs exist anyway). Any `depot.yml` run
that changes the rules or units does the same.

Operator steps, in order:

```bash
# 1. vCenter + UniFi: the range uplink network (skip if it exists)
scripts/lab/create-portgroups.sh --site work            # plan: refuses if VLAN 32 is taken
scripts/lab/create-portgroups.sh --site work --apply    # then do the printed UniFi checklist;
                                                        # tag VLAN 32 on esx02-04's host ports

# 2. TN-MGMT01: the depot's range NIC (y/N before each vCenter change), the Packer read
#    secret (generated into /srv/truenorth/config/depot-build.env, not shown), then depot.yml
sudo scripts/lab/build-phase.sh depot --admin <admin upn>
#   BEFORE depot.yml, on TN-DEPOT01:  ip -br link   -> the range NIC (ens…, no address) is DOWN
#   step 3/5 prints "MAC xx:xx:..."; from the Mac (the secret goes into the environment,
#   never onto the screen):
export TN_DEPOT_BUILD_READ_PASSWORD="$(ssh <you>@10.30.30.20 sudo sed -n 's/^TN_DEPOT_BUILD_READ_PASSWORD=//p' /srv/truenorth/config/depot-build.env)"
cd install/lab/work-site && ansible-playbook ../depot.yml --ask-vault-pass -e tn_depot_range_mac=<MAC>
#   depot.yml fails unless: forwarding reads 0 from the kernel, the nft table is loaded,
#   tnrange0 has that MAC and 10.30.32.10 and not the default route, the guard passes,
#   Nexus and apt-cacher-ng answer on 10.30.32.10, and anonymous cannot read installers.

# 3. TN-DEPOT01 (ssh tnadmin@10.30.30.30): read it back
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding    # both 0
sudo nft list table inet tn_depot_range                     # from_range, input, forward, output
ip -br link; ip -br addr; ip route                          # tnrange0 UP with 10.30.32.10; no default route via it
systemctl status tn-depot-range-link tn-depot-range-guard.timer
#   fail-closed check: the guard downs the NIC when the table goes, and the drop-in/re-run
#   restores it:
sudo nft delete table inet tn_depot_range; sleep 70; ip -br link show tnrange0   # DOWN
sudo systemctl restart tn-depot-range-fw tn-depot-range-link; ip -br link show tnrange0   # UP
#   apt-cacher-ng maintenance actions need AdminAuth (expect 401 without credentials):
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:3142/acng-report.html?doCount=Count+Data'
#   and it is not an open proxy (expect 403, not a page from TN-MGMT01):
curl -s -o /dev/null -w '%{http_code}\n' -x http://10.30.31.30:3142 http://10.30.30.20/

# 4. install/inventory/group_vars/all/main.yml: tn_vsphere_range_uplink_network: dPG-TN-SVC
#    (the other tn_depot_* values are already set), then from install/:
ansible-playbook playbooks/30-config.yml --ask-vault-pass   # and restart the workers
```

**Only verifiable on site** (with a test range whose edge firewall has its WAN on
`dPG-TN-SVC`; runbook §7):
- from the range firewall: `10.30.32.10` tcp/8081 and tcp/3142 answer; tcp/22, 8082, 8083
  and ping do not; 10.30.30.30, 10.30.31.30 and anything on 10.30.30.0/24 do not;
- a Windows range VM installs a Chocolatey package through the depot
  (`provisioner_output.vms[].software` says `ok`), and a Linux one an apt package;
- the nft counters on the depot (`sudo nft list table inet tn_depot_range`) count the
  probes that were dropped;
- after a reboot of TN-DEPOT01 the table is loaded before the NIC comes up
  (`systemctl status tn-depot-range-fw`, `journalctl -b -u tn-depot-range-link`), the NIC
  is named `tnrange0` (udev applied `set-name`), and forwarding is still 0;
- `prefetch.yml` (or `depot.yml -e tn_depot_upstream_online=true`) refuses while a range VM
  exists, and during a window the range firewall gets no answer from 10.30.32.10 at all;
- with `ForceManaged`, apt (archive/security.ubuntu.com) and dnf (dl.rockylinux.org)
  still go through apt-cacher-ng; any other host a range installs from needs a Remap line
  (`tn_depot_acng_remaps`);
- Packer role images and the Office variant fetch from `installers/roles` and
  `installers/office` as `tn-build-read`, and anonymous gets 401 there;
- after prefetch, every in-house Chocolatey package installs on a range VM with no
  credentials (anonymous HEAD `installers/<id>/<version>/<file>` is 200:
  `depot.yml -e tn_depot_require_choco_installers=true` checks all of them);
- a new `content/choco/<id>.yaml` is only readable anonymously after the next `depot.yml`
  run regenerates the content selector.

What CI and the Mac proved instead: the values agree everywhere (contract tests), the
provisioner renders a `TN_DEPOT` alias of exactly 10.30.32.10 with ports 8081 and 3142 for
pfSense and VyOS from the installer's values (fakes), the guard script fails closed
(stubbed nft/ip/sysctl), and build-phase's NIC step and secret handling behave (stub
govc). The netns test (`test_netns_range_side_reaches_only_the_depot_ports`) loads the
rendered table into a real kernel and probes it from a "range WAN" namespace; it needs
Linux, root, nft and ip, so CI skips it (no such lane). Run it as root on Linux, e.g.
`docker run --rm --privileged -v $PWD:/repo:ro ubuntu:24.04` with nftables, iproute2 and
python3-pytest/-jinja2/-yaml, then
`python3 -m pytest --noconftest -p no:cacheprovider tests/contracts/test_work_site_depot_range_nic.py -k "netns or syntax"`.
None of it ran against the work vCenter or TN-DEPOT01.

### vCenter folders and tags

One VM-folder tree, `TrueNorth`, for everything TrueNorth puts in vCenter:

| Folder | What |
|---|---|
| `TrueNorth/Templates/` | Golden templates `<id>` and their host copies `<id>@<host>-<yyyymmdd>`, flat (the kit's `vsphere_folder`) |
| `TrueNorth/Templates/_retired/` | Old TrueNorth templates such as `kali-linux`, tagged `tn-type=retired`: moved here by `vcenter-organize.sh`, never deleted by it (deleting is the owner's call, ADR 0012 decision 2). Never a clone source: the provisioner, `build.sh` and `test-clone.sh` skip it |
| `TrueNorth/Ranges/<range-name>-<key>/` | One folder per range; `<key>` is the first 12 hex digits of the range id (`VSPHERE_RANGE_FOLDER`, installer `tn_vsphere_range_folder`, default `TrueNorth/Ranges` since 2026-10-20) |

The worker finds a range's folder by its `-<key>` suffix, never by its name, so a renamed
range keeps its folder. An earlier build's folder, named exactly `<id8>` (under
`truenorth/ranges`, or moved), is still found on destroy and by the MAC pre-check, but never
reused for a new build: every range sharing that id8 would match it, and in such a folder a
VM annotated for another range stays that range's. `VSPHERE_RANGE_FOLDER` empty or `/` is
refused when the provisioner starts and in teardown (it would make the datacenter's VM root
the range root). Templates and copies are found by name anywhere except `_retired`
(provisioner, `build.sh replicate`, `test-clone.sh`, `prune_template_copies`), so moving
them between folders changes nothing.

Four vCenter tag categories, single-cardinality, on VMs:

| Category | Values | Set by |
|---|---|---|
| `tn-type` | `golden-template`, `template-copy`, `range-vm`, `test-clone`, `depot`, `mgmt`, `retired` | kit (`build.sh`, `test-clone.sh`), worker (range VMs), `vcenter-organize.sh` |
| `tn-os` | OS alias: `windows-server-2022`, `ubuntu-2404`, `pfsense`, ... (catalogue `os_aliases_in_ranges`, first entry) | same |
| `tn-template` | catalogue id the VM came from: `ubuntu-lts`, `srv2022`, ... | same |
| `tn-range` | `<range-name>-<key>`; one tag per range, deleted when the range is destroyed | worker, `vcenter-organize.sh` |

Tagging is best effort everywhere and bounded in the worker: each call times out after
`VSPHERE_TAG_TIMEOUT` (5 s), the first failure or timeout stops tagging for the rest of
that build with one warning, and nothing is tagged in the build budget's last two minutes.
A tag never fails a build, replicate, test clone or range. The vCenter tagging privileges
(`InventoryService.Tagging.*`) are in svc-truenorth's role. `VSPHERE_TAGS=false` (worker)
or `TN_TAGS=0` (kit) turns it off. In vCenter: Inventory > Tags & Custom Attributes, or
filter a VM list by tag. In TrueNorth: **Infrastructure > Virtual machines**, Load VMs,
then Golden templates / Host copies / Range VMs / Other (by `tn-type`; untagged VMs are
Other). A range VM is listed only for its own tenant (by its `tn-range` key) or the
platform administrator; the list is one page of up to 2000.

**Tidying an existing vCenter** (once, after the templates are built; safe to re-run):

```bash
# On TN-MGMT01, from the template-kit checkout. Read-only first:
sudo scripts/lab/vcenter-organize.sh --dry-run
# Then (prints the plan again, asks y/N):
sudo scripts/lab/vcenter-organize.sh
```

It reads vCenter credentials from `/srv/truenorth/config/.env.production` (never printed)
and, read-only from the installed app's database (`docker compose run ... api`,
`scripts/lab/tn_ranges_inprocess.py`), the ranges (ids, names, states, VM names and
templates only) and the template names TrueNorth registered. Only TrueNorth's VMs under the
old `truenorth/` tree move: catalogue templates and their copies to `TrueNorth/Templates`;
templates TrueNorth registered, or named with `--retire NAME`, to `_retired` (never a
powered-on VM); range VMs (a TrueNorth range annotation, or `<id8>-*` in that range's
folder) with their folder, renamed `<range-name>-<key>` when exactly one range has that
id8 (a folder holding anything else is not moved whole: its range VMs move one by one).
Everything else there is reported and left. An old `truenorth` folder is removed only if
this run emptied it, no step of the run failed, and the property collector reports no
children the moment before. It then tags golden templates, copies, retired templates, test
clones (`tn-testclone-*`), range VMs, TN-DEPOT01 and TN-MGMT01. Nothing else is touched
(WIN11-TMPL, the VCSA, vCLS agents). Moving a VM between folders does not touch the VM.

It refuses to apply unless every TrueNorth range is settled (ready, running, stopped,
failed, destroyed), re-reading the ranges itself in apply and once more just before the
first change; without the range list (`--no-db` to proceed anyway: folders keep their
names); and unless both the env file and the running `worker-provision` container
(`docker compose exec ... printenv VSPHERE_RANGE_FOLDER`) say `TrueNorth/Ranges` (otherwise
new ranges would land in the old folder again; upgrade TrueNorth and restart the worker
first, or `--force-folder`). The depot's online-window guard (`range_guard.yml`,
`depot-uplink.sh`) counts range VMs under the worker's `VSPHERE_RANGE_FOLDER` and the earlier
`truenorth/ranges`, and refuses (unless forced) when neither can be looked up or one cannot
be listed.

### Known gaps (read before the derived, roles and variants phases)

- **The depot must hold the `chocolatey` package.** Windows builds and variants bootstrap
  Chocolatey from the `.nupkg` in `<depot_url>/chocolatey` (Nexus serves no `install.ps1`).
  `prefetch.yml` caches it (`tn_prefetch_choco_extra`), but the kit does not run prefetch
  here (below), so cache it once through `chocolatey-proxy` during an online window. The
  depot phase's step 4/4 warns when the feed has no `chocolatey` package.
- **apt-cacher-ng serves only what it has cached and only http:// repositories.** With
  `apt_proxy` set, apt and dnf go through it, but the depot is offline by default
  (`Offlinemode: 1`). Debian, Kali and Parrot packages are not in the prefetch list, and
  https sources (Docker's repo for greyspace-host, the REMnux and SIFT installers) cannot
  use it because it refuses CONNECT. Those steps need an online window or depot-staged media.
- **Prefetch** (`prefetch.yml`) runs its containers and vendor downloads on the build
  host, so here it would need internet egress from TN-MGMT01. The kit does not run it.
  The depot reaches upstream through dPG-TN-MGMT's gateway, and `depot-uplink.sh` does not
  apply here.
- **VLAN 31 reaches only esx04.** Test clones on esx02/esx03 sit on an unconnected
  `dPG-TN-BUILD`. That is fine for the check: Tools reports the customized static IP
  (10.30.31.241-243) whether or not anything answers.
- **Proven on vcsim only.** ISO sync, the DRS rule, replicate and test clones (customize
  mode) ran against vcsim, and the generated vars file passed `packer validate` (1.16.1).
  None of it has run against the work vCenter yet.
