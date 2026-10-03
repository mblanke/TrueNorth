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
| `prefetch.yml` / `tn_prefetch` | Online → warm from TN-BUILD01 → offline (always) → verdict |

Nexus repositories (`http://10.30.32.10:8081/repository/<name>/`):

| Name | Type | Notes |
|---|---|---|
| `chocolatey` | nuget group | **The feed ranges use.** Members: `chocolatey-hosted`, then `chocolatey-proxy` |
| `chocolatey-hosted` | nuget hosted | Filled by `prefetch.yml`; ALLOW_ONCE |
| `chocolatey-proxy` | nuget proxy (V2) | community.chocolatey.org |
| `pypi-proxy` | pypi proxy | pypi.org |
| `docker-proxy` | docker proxy, port 8082 | Docker Hub |
| `docker-hosted` | docker hosted, port 8083 | |
| `installers` | raw hosted | Vendor media (build sheet §6) |

Upload vendor media (from TN-BUILD01, credentials from the environment):

```bash
curl -fsS -u "tn-prefetch:$NEXUS_PASSWORD" --upload-file SQLServer2022-x64-ENU.iso \
  http://10.30.32.10:8081/repository/installers/sql/SQLServer2022-x64-ENU.iso
```

## Known limits — read the prefetch report

- **Chocolatey wrapper packages.** Many community packages (`googlechrome`,
  `adobereader`, `vscode`, …) download the vendor installer *at install time*. The
  `.nupkg` is in the depot but the install fails offline. `prefetch.yml` lists them as
  `NEEDS-INTERNALIZING` (report: `/var/tmp/tn-prefetch/choco-report.json` on
  TN-BUILD01). Fix: Chocolatey for Business internalizer, manual internalization (installer
  into `installers`, repack, upload to `chocolatey-hosted` with a higher version), or bake
  the app into the Windows template (build sheet §5.1).
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
