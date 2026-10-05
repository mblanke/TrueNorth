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

`depot-uplink.sh on` refuses while range VMs exist (folder `truenorth/ranges`) unless
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
