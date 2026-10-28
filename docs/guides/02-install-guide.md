# 02 — Install guide

**For:** the person installing TrueNorth (the owner or another team).
**Before you start:** every item on the [01 — Site prerequisites](01-site-prerequisites.md)
checklist is ticked.
**After this guide:** [03 — Admin first day](03-admin-first-day.md).

This guide takes you from a signed TrueNorth release to a running platform:

1. Build the Ubuntu VM by hand on vSphere (§1–§2).
2. Prepare vCenter for TrueNorth (§3).
3. Fetch and verify the signed release (§4).
4. Configure and run the installer (§5–§6).
5. Sign in for the first time (§7).

The main path assumes an **online** install: the TrueNorth VM can reach the internet
(directly or through the site proxy) during the install. For an air-gapped site, read
Appendix A first; it changes §4 and §5.

The installer is the Ansible package in `install/` of the release. It is the only supported
way to install: it gets ordering, secrets, identity and verification right in ways that are
easy to get wrong by hand. Its reference is `install/README.md`; this guide is the path
through it for one site.

Conventions: `<placeholders>`, **[DECISION]** and the glossary terms are as in guide 01.
`$` lines run as `tnadmin` on the TrueNorth VM unless a step says otherwise.

---

## 1. Create the VM in vSphere

In the vSphere Client: right-click management host 1 → **New Virtual Machine** →
**Create a new virtual machine**.

| Setting | Value |
|---|---|
| Name | `<tn-vm-name>` |
| Compute resource | Management host 1 (not a range host) |
| Storage | Management host 1's local datastore |
| Compatibility | ESXi 8.0 and later |
| Guest OS | Linux, **Ubuntu Linux (64-bit)** |
| CPU | 32 vCPU; cores per socket 16 (two sockets, matching the host's two NUMA nodes) |
| Memory | 128 GB |
| Hard disk 1 (system) | 200 GB, thin provisioned |
| Hard disk 2 (data) | Add **New Hard Disk**: 1,300 GB, thick provision lazy zeroed |
| SCSI controller | VMware Paravirtual |
| Network adapter 1 | VMXNET3, on the management port group (`<vlan-mgmt>`), connect at power on |
| CD/DVD drive 1 | **Datastore ISO file**: `ubuntu-24.04.<x>-live-server-amd64.iso`, connect at power on |
| Boot options | EFI, Secure Boot on |

Upload the Ubuntu Server 24.04 LTS ISO (the newest 24.04 point release) to a datastore
first if it is not there. The 32 vCPU / 128 GB / 1.5 TB size is the starting point for a
site of the reference kit's scale; it can grow later.

Power the VM on and open its web console.

### 1.1 Ubuntu Server install choices

The Ubuntu installer asks, in order:

| Screen | Choose |
|---|---|
| Language, keyboard | Your choice |
| Type of install | **Ubuntu Server** (not the minimized one) |
| Network | Select the one interface (usually `ens192`) → **Edit IPv4** → **Manual**: subnet `<mgmt-subnet>/24`, address `<tn-ip>`, gateway `<mgmt-gw>`, name servers `<dns1>,<dns2>` (the site DNS, normally the DCs), search domains `<ad-domain>` |
| Proxy | The site proxy, if internet access goes through one (`http://<proxy-host>:<port>`); otherwise empty |
| Ubuntu archive mirror | The default, or the site's mirror |
| Storage | **Use an entire disk**: pick the **200 GB** disk (not the 1.3 TB one); keep **Set up this disk as an LVM group** ticked; no encryption. The data disk is set up after the install (§1.2) |
| Profile | Your name: as you like. Server name: `<tn-vm-name>`. Username: **`tnadmin`**. A strong password (you need it for `sudo`) |
| Ubuntu Pro | Skip |
| SSH | **Install OpenSSH server**: ticked. Import SSH key: as you like |
| Featured server snaps | **Select none.** In particular not `docker`: the installer installs Docker itself and stops if it finds another Docker |

Let the install finish, choose **Reboot Now**, and press Enter when asked to remove the
installation medium. Then sign in as `tnadmin` (console or SSH).

### 1.2 Disks: grow the system volume, mount the data disk

The installer's LVM layout uses only part of the system disk. Grow it to the whole disk:

```bash
sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
df -h /
```

Find the data disk. It is the 1.3 TB disk with no partitions (on a Paravirtual controller,
usually `/dev/sdb`):

```bash
lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS
```

**Check the name before the next block**: it erases that disk. Replace `/dev/sdb` if yours
differs.

```bash
DATA_DISK=/dev/sdb
sudo pvcreate "$DATA_DISK"
sudo vgcreate tn-data "$DATA_DISK"
sudo lvcreate -n srv -l 100%FREE tn-data
sudo mkfs.ext4 -L tn-srv /dev/tn-data/srv
sudo mkdir -p /srv/truenorth
echo 'LABEL=tn-srv /srv/truenorth ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /srv/truenorth
df -h /srv/truenorth
```

`df` must show about 1.2 TB on `/srv/truenorth`. Everything TrueNorth stores (databases,
telemetry, objects, backups) lives there; Docker's images live on the system disk.

### 1.3 Updates, time and name resolution

```bash
sudo apt-get update
sudo apt-get -y full-upgrade
sudo apt-get install -y chrony git jq python3-venv dnsutils ldap-utils
```

Point the clock at the site's time source (**[DECISION]** normally the DCs):

```bash
echo "server <ntp-server> iburst" | sudo tee /etc/chrony/sources.d/site.sources
sudo chronyc reload sources
sleep 30
chronyc sources
timedatectl show -p NTPSynchronized --value
```

The last command must print `yes`. If it prints `no`, wait a minute and run it again; if it
stays `no`, the VM cannot reach the time source (check the firewall for UDP 123).

Check name resolution, forward and reverse, through the site DNS:

```bash
dig +short <tn-fqdn>
dig +short -x <tn-ip>
dig +short <vcenter-fqdn>
dig +short <dc-fqdn>
```

Each must answer. A missing answer is a DNS record to add (guide 01 §5.1) before going on.

Reboot once, so the upgraded kernel is the one running:

```bash
sudo reboot
```

---

## 2. Prepare the control node

The installer runs **on the TrueNorth VM itself** (Ansible's local connection). Nothing
else needs Ansible.

```bash
python3 -m venv ~/.venvs/tn-install
~/.venvs/tn-install/bin/pip install "ansible-core==2.21.4"
echo 'export PATH="$HOME/.venvs/tn-install/bin:$PATH"' >> ~/.bashrc
echo 'export EDITOR=nano' >> ~/.bashrc
export PATH="$HOME/.venvs/tn-install/bin:$PATH" EDITOR=nano
ansible --version | head -1
```

`EDITOR` is the editor `ansible-vault` opens (§5.2).

---

## 3. Prepare vCenter

### 3.1 Install govc

`govc` is VMware's command-line client; this guide uses it for vCenter work. Install the
pinned release, checksum-verified:

```bash
GOVC_VERSION=0.56.0
cd /tmp
curl -fsSLO "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/govc_Linux_x86_64.tar.gz"
curl -fsSLO "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/checksums.txt"
grep ' govc_Linux_x86_64.tar.gz$' checksums.txt | sha256sum -c -
sudo tar -xzf govc_Linux_x86_64.tar.gz -C /usr/local/bin govc
govc version
```

Point it at vCenter as the vCenter administrator, for this shell only:

```bash
export GOVC_URL=https://<vcenter-fqdn>
export GOVC_USERNAME='<vcenter-admin>'
read -rsp "vCenter administrator password: " GOVC_PASSWORD; echo; export GOVC_PASSWORD
govc about
```

If vCenter still serves its own self-signed (VMCA) certificate that this VM does not trust,
`govc about` fails on the certificate. Either trust the VMCA root on this VM, or add
`export GOVC_INSECURE=1` for this shell (and set `tn_vsphere_verify_ssl: false` in §5.3,
with a note in the site record to fix it).

### 3.2 The TrueNorth-Provision role

TrueNorth's service account needs a custom role at the **datacenter**, with propagation.
Granting write access to a cluster is a human decision: **[DECISION]** confirm with the
vCenter owner before running this.

```bash
govc role.create TrueNorth-Provision \
  System.Anonymous System.View System.Read \
  Datastore.AllocateSpace Datastore.Browse \
  Network.Assign \
  DVPortgroup.Create DVPortgroup.Modify DVPortgroup.Delete DVPortgroup.PolicyOp \
  DVSwitch.Modify DVSwitch.PortSetting \
  Folder.Create Folder.Delete \
  Resource.AssignVMToPool \
  InventoryService.Tagging.AttachTag InventoryService.Tagging.ObjectAttachable \
  VirtualMachine.Inventory.Create VirtualMachine.Inventory.CreateFromExisting VirtualMachine.Inventory.Delete \
  VirtualMachine.Provisioning.Clone VirtualMachine.Provisioning.CloneTemplate \
  VirtualMachine.Provisioning.DeployTemplate VirtualMachine.Provisioning.Customize \
  VirtualMachine.Provisioning.ReadCustSpecs \
  VirtualMachine.Config.AddNewDisk VirtualMachine.Config.AddExistingDisk VirtualMachine.Config.AddRemoveDevice \
  VirtualMachine.Config.AdvancedConfig VirtualMachine.Config.Annotation VirtualMachine.Config.CPUCount \
  VirtualMachine.Config.Memory VirtualMachine.Config.EditDevice VirtualMachine.Config.Settings \
  VirtualMachine.Config.Resource VirtualMachine.Config.DiskExtend \
  VirtualMachine.Interact.PowerOn VirtualMachine.Interact.PowerOff VirtualMachine.Interact.Reset \
  VirtualMachine.Interact.ConsoleInteract VirtualMachine.Interact.DeviceConnection \
  VirtualMachine.Interact.GuestControl VirtualMachine.Interact.SetCDMedia \
  VirtualMachine.State.CreateSnapshot VirtualMachine.State.RevertToSnapshot \
  VirtualMachine.State.RemoveSnapshot VirtualMachine.State.RenameSnapshot \
  VirtualMachine.GuestOperations.Execute VirtualMachine.GuestOperations.Query VirtualMachine.GuestOperations.Modify \
  ContentLibrary.DownloadSession ContentLibrary.ReadStorage

govc permissions.set -principal '<svc-truenorth>' -role TrueNorth-Provision -propagate=true '/<datacenter>'
govc permissions.ls '/<datacenter>' | grep TrueNorth-Provision
```

This list is derived from what the provisioner calls. The reference lab's role was made by
a PowerCLI script in the separate deployment repository and holds more privileges; if a
range build later fails with a 403, the error names the missing privilege: add it with
`govc role.update -a TrueNorth-Provision <privilege>`. Add `Cryptographer.*` privileges
only if you build Windows 11 templates with a vTPM.

### 3.3 Folders and the range cluster

TrueNorth puts range VMs under the VM folder `TrueNorth/Ranges` (it creates the per-range
folders itself). Create the parents once:

```bash
govc folder.create '/<datacenter>/vm/TrueNorth'
govc folder.create '/<datacenter>/vm/TrueNorth/Ranges'
govc folder.create '/<datacenter>/vm/TrueNorth/Templates'
```

Check the range cluster and its hosts are what you will put in the inventory:

```bash
govc find '/<datacenter>/host' -type h
```

---

## 4. Fetch and verify the signed release

**[DECISION]** The release to install: `<vX.Y.Z>` (the newest release on
https://github.com/mblanke/TrueNorth/releases, unless the owner names another). A tag with a
suffix (`-rc…`) is a pre-release, for test sites.

### 4.1 The installer: a checkout at the release tag

```bash
TN_RELEASE=<vX.Y.Z>
git clone https://github.com/mblanke/TrueNorth.git ~/truenorth
git -C ~/truenorth checkout "$TN_RELEASE"
git -C ~/truenorth log -1 --format='%H %s'
```

The installer and the application it installs are always the same revision: keep this
checkout at the tag. Never edit tracked files in it; your site's files (§5) are ignored by
git.

### 4.2 The release files and their signatures

```bash
mkdir -p ~/release/"$TN_RELEASE" && cd ~/release/"$TN_RELEASE"
for f in release-manifest.json release-manifest.json.sigstore.json SHA256SUMS SHA256SUMS.sigstore.json; do
  curl -fsSLO "https://github.com/mblanke/TrueNorth/releases/download/${TN_RELEASE}/${f}"
done
ls -l
```

Verify them yourself with the same pinned cosign the installer uses (it verifies them again
before it trusts any image digest):

```bash
curl -fsSLo ~/cosign https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-amd64
echo "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71  $HOME/cosign" | sha256sum -c -
chmod +x ~/cosign

ID="https://github.com/mblanke/TrueNorth/.github/workflows/release.yml@refs/tags/${TN_RELEASE}"
ISS=https://token.actions.githubusercontent.com
for f in release-manifest.json SHA256SUMS; do
  ~/cosign verify-blob --bundle "$f.sigstore.json" \
    --certificate-identity "$ID" --certificate-oidc-issuer "$ISS" "$f"
done
sha256sum -c --ignore-missing SHA256SUMS
```

Expected: `Verified OK` twice, then `OK` for each file you downloaded. Then confirm the
checkout is the commit the release was built from:

```bash
test "$(jq -r .git_sha release-manifest.json)" = "$(git -C ~/truenorth rev-parse HEAD)" && echo "commit matches"
```

If any check fails, **stop**: do not install that release. Report it to the owner.

---

## 5. Configure the installer

All site configuration lives in four places under `~/truenorth/install/`, none of them
tracked by git:

| File | Holds |
|---|---|
| `inventory/site.yml` | This site's settings (§5.3) |
| `inventory/group_vars/all/vault.yml` | Secrets, encrypted with ansible-vault (§5.2) |
| `files/corp-root-ca.cer`, `files/tls/truenorth.crt`, `files/tls/truenorth.key` | The site CA and TrueNorth's certificate (§5.4) |
| `files/backup-escrow.pub` | The backup escrow public key (§5.1) |

Keep a copy of all four off the VM (06 §3.1): rebuilding after a host loss needs them.

### 5.1 The backup escrow key

Every nightly backup encrypts TrueNorth's secrets to this key. Make it **on a trusted
machine that is not the TrueNorth VM**, keep the private half offline with two custodians,
and copy only the public half to the VM:

```bash
# On the trusted machine:
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out backup-escrow.key
openssl pkey -in backup-escrow.key -pubout -out backup-escrow.pub
```

Copy `backup-escrow.pub` to `~/truenorth/install/files/backup-escrow.pub` on the VM (for
example with `scp`). `backup-escrow.key` never goes on the VM except during a rebuild
(06 §3.5).

### 5.2 Secrets: the vault

```bash
cd ~/truenorth/install
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
nano inventory/group_vars/all/vault.yml
```

Set these, and leave every other value empty (empty values are generated on the VM, once,
and kept):

| Key | Value |
|---|---|
| `vault_ldap_bind_password` | The AD bind account's password |
| `vault_vsphere_password` | `<svc-truenorth>`'s vCenter password |
| `vault_template_windows_admin_password` | The golden templates' Windows build password, from the build record of the test site where the templates were made (06 §4). Required: range builds check that no VM still accepts it |
| `vault_template_linux_password` | The golden templates' Linux build password, from the same record |
| `vault_openai_api_key` | Only with AI: the LiteLLM master key (Appendix C). Otherwise empty |

Then encrypt it. **[DECISION]** the vault password; store it with the escrow key's
custodians.

```bash
ansible-vault encrypt inventory/group_vars/all/vault.yml
head -1 inventory/group_vars/all/vault.yml     # must start with $ANSIBLE_VAULT
```

To change a value later: `ansible-vault edit inventory/group_vars/all/vault.yml`.

The installer's sign-in check (the claim contract) also needs a non-privileged AD account
(`<smoke-upn>`, guide 01 §7). Keep its password in its own vaulted file:

```bash
cd ~/truenorth/install
ansible-vault create smoke.yml
```

In the editor that opens, write the two lines below, save and quit:

```yaml
tn_smoke_username: <smoke-upn>
tn_smoke_password: <smoke-account-password>
```

### 5.3 The site inventory

Create `~/truenorth/install/inventory/site.yml`. The block below is the reference kit's
shape; replace every placeholder, and read each **[DECISION]**. The installer's defaults
(`inventory/group_vars/all/main.yml`) are commented and cover everything not listed here.
Anything you set here, under the host, overrides them.

> Always run the installer with `-i inventory/site.yml`. Without it, Ansible reads
> `inventory/hosts.yml`, which describes a different site.

```yaml
all:
  children:
    platform:
      hosts:
        <tn-vm-name>:
          # ── How Ansible reaches this host: it runs on it ──────────────────────
          ansible_connection: local
          ansible_host: <tn-ip>
          ansible_user: tnadmin
          ansible_python_interpreter: /usr/bin/python3

          # ── Which release ────────────────────────────────────────────────────
          tn_image_source: release
          tn_release_version: <vX.Y.Z>
          tn_release_dir: /home/tnadmin/release/<vX.Y.Z>

          # ── This deployment ──────────────────────────────────────────────────
          tn_domain_fqdn: <tn-fqdn>
          tn_tenant_name: "<organisation name>"
          # [DECISION] A stable, unique, lowercase id for this deployment, and its
          # classification. Never reuse another deployment's id.
          tn_enclave_id: <enclave-id>
          tn_classification_level: unclassified
          # [DECISION] The site's accredited sign-in notice, if it differs from the default.
          # tn_login_banner_text: >-
          #   <notice text>

          # ── TLS: the site CA's certificate (§5.4) ──────────────────────────────
          tn_tls_mode: provided

          # ── Active Directory (read-only) ─────────────────────────────────────
          tn_ldap_enabled: true
          tn_domain: <ad-domain>
          tn_dc_host: <dc-fqdn>
          tn_dc_ip: <dc-ip>
          tn_dns_servers: ["<dc-ip>"]
          tn_ldap_base_dn: "<domain-dn>"
          tn_ldap_users_dn: "OU=Users,OU=TrueNorth,<domain-dn>"
          tn_ldap_groups_dn: "OU=Groups,OU=TrueNorth,<domain-dn>"
          tn_ldap_bind_dn: "CN=TrueNorth Keycloak Bind,OU=ServiceAccounts,OU=TrueNorth,<domain-dn>"
          # The local break-glass administrator: a Keycloak account OUTSIDE the AD
          # domain, which still signs in when AD, LDAPS or the bind account fails.
          tn_keycloak_local_admin: true
          tn_bootstrap_admin_upn: tnadmin@<tn-vm-name>.local
          # The nightly AD status sync starts as a dry run (06 §9).
          tn_ad_status_sync_dry_run: true

          # ── vCenter and range placement ──────────────────────────────────────
          tn_provisioner_backend: vsphere_api
          tn_vcenter_host: <vcenter-fqdn>
          tn_vcenter_user: <svc-truenorth>
          tn_vsphere_verify_ssl: true
          tn_fail_on_missing_vsphere_privs: true
          tn_vsphere_datacenter: <datacenter>
          tn_vsphere_cluster: <range-cluster>
          tn_vsphere_datastore: <one-range-host>-local
          tn_vsphere_placement: spread
          tn_vsphere_exclude_hosts:
            - <management-host-1-name-in-vcenter>
            - <management-host-2-name-in-vcenter>
          tn_vsphere_mgmt_network: <management-port-group>
          tn_vsphere_range_switch_mode: vds
          tn_vsphere_range_dvs: <range-vds>
          tn_vsphere_vlan_pool: "<pool-start>-<pool-end>"
          tn_vsphere_reserved_vlans: "<site-vlans, e.g. 10,11,20,30,40>"
          # [DECISION] This install's own slice of VMware's manual MAC range. Every
          # TrueNorth install sharing a vCenter needs a different, non-overlapping slice.
          tn_vsphere_mac_pool: "00:50:56:3c:00:00-00:50:56:3d:ff:ff"
          tn_range_reserved_cidrs:
            - <mgmt-subnet>/24
            - <oob-subnet>/24
            - <vmotion-subnet>/24
            - <ai-subnet>/24
            - <uplink-subnet>/24

          # ── Range isolation evidence (06 §6) ──────────────────────────────────
          tn_range_isolation_check: warn
          tn_range_isolation_probe_targets:
            - <mgmt-gw>:443
            - <dc-ip>:53
            - <vcenter-ip>:443
            - <tn-ip>:443
            - <a-range-host-mgmt-ip>:443
          tn_range_isolation_external_name: example.com

          # ── Depot and range uplink. Without a depot (guide 01 §3.3): ──────────
          tn_vsphere_range_uplink_network: ""
          tn_depot_url: ""
          tn_depot_choco_feed: ""
          tn_depot_apt_proxy: ""
          tn_range_isolation_exceptions: []
          # With a depot, instead:
          # tn_vsphere_range_uplink_network: <range-uplink-port-group>
          # tn_vsphere_range_uplink_pool: "<uplink-first>-<uplink-last>"
          # tn_vsphere_range_uplink_gateway: <uplink-gw>
          # tn_vsphere_range_uplink_prefix: 24
          # tn_depot_url: "http://<depot-range-ip>"
          # tn_depot_choco_feed: "http://<depot-range-ip>:8081/repository/chocolatey/"
          # tn_depot_apt_proxy: "http://<depot-range-ip>:3142"
          # tn_range_isolation_exceptions: ["<depot-range-ip>:8081", "<depot-range-ip>:3142"]

          # ── AI. Without a GPU node: ───────────────────────────────────────────
          tn_ai_enabled: false
          # With one (Appendix C), instead:
          # tn_ai_enabled: true
          # tn_ai_base_url: http://<ai-node-fqdn>:4000/v1
          # tn_ai_default_model: agent

          # ── Backups (06 §3) ──────────────────────────────────────────────────
          tn_backup_escrow_pubkey: /home/tnadmin/truenorth/install/files/backup-escrow.pub

          # ── Content feeds (06 §7) ────────────────────────────────────────────
          tn_feeds_mode: online
          # tn_feeds_http_proxy: http://<proxy-host>:<port>

          # ── Email is parked (§6.4): leave tn_smtp_host unset. ─────────────────
```

Notes on a few of these:

- `tn_vsphere_exclude_hosts` and the cluster: range VMs must never land on a management
  host. Use the host names exactly as vCenter shows them (`govc find '/<datacenter>/host' -type h`).
- `tn_vsphere_verify_ssl: true` needs vCenter's certificate to chain to a CA this VM
  trusts (the site CA in `corp-root-ca.cer`, or VMCA's root added to the trust store). If
  it does not yet, set `false` for now and record it as an open item.
- `tn_range_isolation_probe_targets`: site addresses no range may reach, as `host:port`
  (TCP and ICMP are both tested). List the management gateway, a DC, vCenter, the
  TrueNorth VM and one ESXi management address at least.

### 5.4 The site CA and TrueNorth's certificate

```bash
cd ~/truenorth/install
cp /path/to/corp-root-ca.cer files/corp-root-ca.cer
mkdir -p files/tls
cp /path/to/truenorth.crt files/tls/truenorth.crt
cp /path/to/truenorth.key files/tls/truenorth.key
chmod 600 files/tls/truenorth.key
openssl x509 -in files/tls/truenorth.crt -noout -subject -ext subjectAltName -enddate
openssl x509 -in files/tls/truenorth.crt -noout -modulus | sha256sum
openssl rsa -in files/tls/truenorth.key -noout -modulus | sha256sum
```

The subject alternative name must include `DNS:<tn-fqdn>`, and the last two lines must print
the same hash (the key belongs to the certificate). Remove the copies from wherever you
brought them in.

Check that LDAPS to the DC works with that CA before the installer tries it:

```bash
openssl s_client -connect <dc-fqdn>:636 -CAfile files/corp-root-ca.cer </dev/null 2>/dev/null | grep 'Verify return code'
```

Expected: `Verify return code: 0 (ok)`.

---

## 6. Run the installer

### 6.1 Collections, Docker, and a dry run

```bash
cd ~/truenorth/install
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory/site.yml playbooks/00-docker.yml -K --ask-vault-pass
```

`-K` asks for `tnadmin`'s sudo password, `--ask-vault-pass` for the vault password.
`00-docker` installs Docker Engine from Docker's own repository at pinned versions, holds
them, and adds `tnadmin` to the `docker` group. Then a dry run of everything, which changes
nothing:

```bash
ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass --check
```

`--check` stops at the first stage that needs an earlier stage's real work (for example a
missing directory). What matters is that **preflight** (`00-preflight`) passes: it checks
the vault, the OS, Docker, disk, time, DNS forward and reverse through AD, LDAPS, vCenter,
the release and the escrow, and names the fix for anything wrong. Fix each failure and
re-run until preflight is clean.

### 6.2 The install

```bash
ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
```

It takes a while: it pulls every image by digest from the release (several GB), migrates
the database, configures Keycloak and AD federation, seeds the tenant and the bootstrap
administrator, registers vCenter, and ends with the smoke test (`95-smoke-test`: every
container healthy, readiness of database, Redis and OpenSearch, and a real sign-in whose
token must carry the AD groups) and the validation report (`99-validate`).

The seed also decides which tenant is the **platform tenant** (`PLATFORM_TENANT_ID`: its
admins are the platform administrators). Unless you set `tn_platform_tenant_id`, it is the
bootstrap tenant on `tn_tenant_slug`, picked on the first run only, recorded in
`/srv/truenorth/state/platform_tenant_id` and never re-picked by slug. A later run stops,
naming both ids, if a different tenant has taken that slug. This guide installs with
Compose, which needs nothing more. A Kubernetes install (the Helm chart) is not run by the
installer: set `config.platformTenantId` to the operator's tenant id **before a second
tenant exists**, or nobody is the platform administrator (the api logs an error at startup
and refuses platform actions).

Read the report at the end. Expected on this site's first run:

| Line | Expected | Meaning |
|---|---|---|
| `AI` | `disabled` (or `enabled`, all models listed) | Appendix C |
| `alerts` | `NOWHERE (null receiver)` with `WARNING: ALERTS ARE NOT DELIVERED` | Email is parked (§6.4). Read firing alerts on the VM instead (06 §1) |
| vCenter privileges | provisioning allowed | §3.2. If not, the run fails because `tn_fail_on_missing_vsphere_privs: true` |
| ai models / discovery | as configured | |

### 6.3 Prove it: run it again

```bash
ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
```

A clean second run, changing nothing of substance and passing the smoke test again, is the
proof the install is repeatable. Keep both runs' output with the site record.

### 6.4 Email is parked

No SMTP relay is configured (`tn_smtp_host` unset). TrueNorth works without it, but nothing
is emailed:

- Students added to a class roll call get no onboarding email: tell them to sign in at
  `https://<tn-fqdn>/` themselves.
- No class reminders, seat-forfeit notices or calendar invites; no emails to class leads
  when the AD status sync deactivates someone.
- Alerts go nowhere: Alertmanager has no receiver.

Every administrator sees a persistent banner, *"Email is not configured: onboarding emails,
reminders and calendar invites are not being sent"*, and the Classes page warns when
Students are added. Both are expected. When a relay is chosen, set the `tn_smtp_*`
variables (`install/README.md` "Email (SMTP)") and alert email (`tn_alert_email_enabled`,
`tn_alert_email_to`), then re-run `site.yml` and prove delivery with
`ansible-playbook -i inventory/site.yml playbooks/alert-test.yml --ask-vault-pass`.

---

## 7. First sign-in

### 7.1 The break-glass administrator

1. Read its generated password on the VM:

   ```bash
   sudo cat /srv/truenorth/config/secrets/bootstrap_admin_password; echo
   ```

   Put it in the site's password safe. Never delete or edit that file: the installer reads
   it on every run.
2. From a workstation that resolves `<tn-fqdn>` through the site DNS, open
   `https://<tn-fqdn>/`. **Always by name**: by IP address the page stays empty.
3. Tick the notice and sign in as `tnadmin@<tn-vm-name>.local` with that password.
4. **Mobile Authenticator Setup** opens: scan the QR code with an authenticator app, type
   the code. TrueNorth may ask for one more code straight away; that is expected.
5. If a **Rules of use** page appears, read it and agree.

Each sign-in with this account raises the critical alert `BreakGlassAccountUsed` and an
audit row. That is its purpose: use it to admit the first AD administrator, then keep it for
recovery only.

### 7.2 The first AD administrator

1. The person named as `<first-admin-upn>` (in `TN-Platform-Admins`) signs in with their AD
   account. They land on the registration form (not an error page): they fill it in and
   submit.
2. The break-glass administrator opens **Approvals** in the left menu, finds the request,
   chooses the role **admin**, and approves. Approval is what creates the TrueNorth account
   and grants the role; the AD group only suggested it.
3. The new administrator signs out and in again, and enrols their authenticator.
4. The break-glass administrator signs out.

### 7.3 Look around before handing over

As the AD administrator:

- **Admin → System Health**: every service healthy.
- **Infrastructure**: the connection *Range vCenter* is listed with its discovered hosts.
  If its test failed, the page says why (certificate, password, reachability).
- **Infrastructure → Golden images**: empty until templates are ported (06 §4). No range
  can be built until then.
- **AI Orchestrator**: "AI is not configured" (without AI), or the *Platform AI* backend.

The install is done. Next: port the golden templates (06 §4), then
[03 — Admin first day](03-admin-first-day.md).

---

## Appendix A — Air-gapped installation

An air-gapped site installs from a **transfer kit**: everything the online path downloads,
carried in on approved media. Build the kit on a connected Ubuntu 24.04 x86-64 machine
(so wheels and binaries match the target), then check it and carry it across.

### A.1 What the kit holds

| Item | How to make it | Used for |
|---|---|---|
| Ubuntu Server 24.04 ISO | Ubuntu's site, with its `SHA256SUMS` | §1 |
| An Ubuntu package mirror, or the site's | **[DECISION]** an internal mirror of `archive.ubuntu.com` / `security.ubuntu.com` for noble | §1.3 and the installer's own package installs |
| Docker Engine | Either an internal mirror of `https://download.docker.com/linux/ubuntu` (set `tn_docker_apt_repo_url` to it; the same key signs it), or Docker's `.deb` files at the versions in `install/inventory/group_vars/all/main.yml` (`tn_docker_version`, …), installed by hand with `tn_install_docker: false` and the containerd image store enabled in `/etc/docker/daemon.json` | §6.1 |
| The TrueNorth checkout at the tag | `git clone https://github.com/mblanke/TrueNorth.git && git -C TrueNorth checkout <vX.Y.Z>`, then `tar -czf truenorth-<vX.Y.Z>.tar.gz TrueNorth` | §4.1 |
| The release files | §4.2's four files, verified there | §4.2 |
| A Sigstore trusted root | After §4.2's online verify, `find ~/.sigstore -name trusted_root.json` | Offline signature check |
| cosign | `~/cosign` from §4.2 | Offline signature check |
| govc | §3.1's tarball and `checksums.txt` | §3 |
| ansible-core wheels | `python3 -m pip download "ansible-core==2.21.4" -d wheels` | §2 |
| Ansible collections | `ansible-galaxy collection download -r ~/truenorth/install/requirements.yml -p collections` | §6.1 |
| Images | `install/README.md` "Air-gapped installs": every image the release runs, saved **by tag from Docker's containerd image store** (the classic store loses the digest and the installer then refuses the image) | §6 |
| Feed bundles and key | 06 §7.2 | Content feeds |
| AI node | NVIDIA driver packages, container images, model weights (Appendix C) | Optional |

Make a `SHA256SUMS` of the whole kit (`find kit -type f ! -name SHA256SUMS -exec sha256sum {} + > kit/SHA256SUMS`),
and check it again on the far side (`cd kit && sha256sum -c SHA256SUMS`).

### A.2 What changes in this guide

- §1.3: point apt at the internal mirror; point chrony at the DC (there is no other time).
- §2: `~/.venvs/tn-install/bin/pip install --no-index --find-links /path/to/kit/wheels "ansible-core==2.21.4"`.
- §3.1: install govc from the kit's tarball (same `sha256sum -c` check).
- §4: unpack the checkout to `~/truenorth`; copy the release files to `~/release/<vX.Y.Z>`;
  run §4.2's checks with `--trusted-root /path/to/kit/trusted_root.json --offline=true` added
  to each `~/cosign verify-blob` line (the installer does the same).
- §5.3: add to the host's settings in `site.yml`:

  ```yaml
          tn_app_source_mode: local
          tn_app_local_path: /home/tnadmin/truenorth
          tn_release_trusted_root: /path/to/kit/trusted_root.json
          tn_cosign_path: /home/tnadmin/cosign
          tn_image_archive_dir: /path/to/kit/images
          tn_feeds_mode: bundle
          tn_feeds_bundle_public_key: "<feed-bundle-public-key-base64>"
          # One of:
          # tn_docker_apt_repo_url: https://<internal-docker-mirror>/linux/ubuntu
          # tn_install_docker: false
  ```

- §6.1: `ansible-galaxy collection install -r /path/to/kit/collections/requirements.yml`.
- AI (Appendix C) and feeds (06 §7.2) use their offline paths.

The offline signature check (`--offline` with a trusted root) is implemented but has not yet
been exercised against a real release: the first air-gapped install is that test. Record
the result.

---

## Appendix B — A minimal AD DC for a site without one

For a site with no AD, DNS or CA of its own. It builds one Windows Server VM that is the
domain controller, DNS server, time source and enterprise root CA. **[DECISION]** whether a
site-built DC is acceptable to the site's security authority; the alternative is installing
without AD (`tn_ldap_enabled: false`, local accounts only; `install/README.md` "Without
Active Directory").

### B.1 The VM

On management host 2: Windows Server 2022 or 2025 Standard (Desktop Experience), 2 vCPU,
8 GB, 80 GB, VMXNET3 on the management port group, static IP `<dc-ip>`, DNS pointing at
itself (`127.0.0.1`) after promotion. Name it `<dc-short-name>`, install VMware Tools,
apply updates, and activate Windows per the site's licensing.

### B.2 Promote it (PowerShell as Administrator)

```powershell
Install-WindowsFeature AD-Domain-Services, DNS -IncludeManagementTools
Install-ADDSForest -DomainName "<ad-domain>" -DomainNetbiosName "<NETBIOS>" -InstallDns `
  -SafeModeAdministratorPassword (Read-Host -AsSecureString "DSRM password") -Force
```

The server reboots. Sign in as `<NETBIOS>\Administrator`, then:

```powershell
# Time: this DC is the domain's time source
w32tm /config /manualpeerlist:"<upstream-ntp>" /syncfromflags:manual /reliable:yes /update
Restart-Service w32time
# DNS: forwarders for other names (online sites), reverse zone, records for TrueNorth and vCenter
Add-DnsServerForwarder -IPAddress <upstream-dns>
Add-DnsServerPrimaryZone -NetworkId "<mgmt-subnet>/24" -ReplicationScope Domain
Add-DnsServerResourceRecordA -ZoneName "<ad-domain>" -Name "<tn-host-label>" -IPv4Address <tn-ip> -CreatePtr
Add-DnsServerResourceRecordA -ZoneName "<ad-domain>" -Name "<vcenter-host-label>" -IPv4Address <vcenter-ip> -CreatePtr
```

Add an `A` record with `-CreatePtr` the same way for every ESXi host. On an air-gapped site
there is no upstream: leave out the forwarder, and point `w32tm` at the site's time source
(a GPS clock, or the host hardware clock as a last resort).

### B.3 The enterprise root CA

```powershell
Install-WindowsFeature ADCS-Cert-Authority -IncludeManagementTools
Install-AdcsCertificationAuthority -CAType EnterpriseRootCa -CACommonName "<ca-name>" `
  -KeyLength 4096 -HashAlgorithmName SHA256 -ValidityPeriod Years -ValidityPeriodUnits 10 -Force
certutil -ca.cert C:\corp-root-ca.cer
```

The DC enrols its own domain controller certificate automatically, which turns LDAPS on.
Restart the DC once, then check from the TrueNorth VM (§5.4's `openssl s_client` line).
Copy `C:\corp-root-ca.cer` to the TrueNorth VM as `install/files/corp-root-ca.cer`.

### B.4 OUs, groups and accounts

```powershell
$Domain = (Get-ADDomain).DistinguishedName
New-ADOrganizationalUnit -Name "TrueNorth" -Path $Domain
foreach ($ou in "Users", "Groups", "ServiceAccounts") {
  New-ADOrganizationalUnit -Name $ou -Path "OU=TrueNorth,$Domain"
}
foreach ($g in "TN-Platform-Admins", "TN-Range-Operators", "TN-Instructors", "TN-Content-Authors", "TN-Students", "TN-Observers") {
  New-ADGroup -Name $g -GroupScope Global -GroupCategory Security -Path "OU=Groups,OU=TrueNorth,$Domain"
}
New-ADUser -Name "TrueNorth Keycloak Bind" -SamAccountName "svc-tn-bind" `
  -UserPrincipalName "svc-tn-bind@<ad-domain>" -Path "OU=ServiceAccounts,OU=TrueNorth,$Domain" `
  -AccountPassword (Read-Host -AsSecureString "Bind account password") `
  -Enabled $true -PasswordNeverExpires $true -CannotChangePassword $true
```

Each person, for example the first administrator:

```powershell
New-ADUser -Name "<Given Surname>" -GivenName "<Given>" -Surname "<Surname>" `
  -SamAccountName "<sam>" -UserPrincipalName "<sam>@<ad-domain>" -EmailAddress "<sam>@<ad-domain>" `
  -Path "OU=Users,OU=TrueNorth,$Domain" -AccountPassword (Read-Host -AsSecureString "Password") `
  -Enabled $true -ChangePasswordAtLogon $false
Add-ADGroupMember -Identity "TN-Platform-Admins" -Members "<sam>"
```

Make `<smoke-upn>` the same way, in `TN-Observers` only.

### B.5 TrueNorth's certificate from this CA

On the TrueNorth VM, make the key and request:

```bash
cd ~/truenorth/install && mkdir -p files/tls
openssl req -new -newkey rsa:3072 -nodes -keyout files/tls/truenorth.key -out truenorth.csr \
  -subj "/CN=<tn-fqdn>" -addext "subjectAltName=DNS:<tn-fqdn>"
chmod 600 files/tls/truenorth.key
```

Copy `truenorth.csr` to the DC (`C:\truenorth.csr`) and issue it from the Web Server
template:

```powershell
certreq -submit -attrib "CertificateTemplate:WebServer" C:\truenorth.csr C:\truenorth.cer
```

Pick the CA when asked. Copy `C:\truenorth.cer` back and convert it to PEM if it is DER:

```bash
openssl x509 -in truenorth.cer -out files/tls/truenorth.crt 2>/dev/null \
  || openssl x509 -inform DER -in truenorth.cer -out files/tls/truenorth.crt
```

Then continue at §5.4's checks. In `site.yml`, the DC values are this VM's
(`tn_dc_host`, `tn_dc_ip`, `tn_dns_servers`), and the DNs follow `$Domain`.

---

## Appendix C — Add a GPU AI node

**[DECISION]** Whether this site has AI. If not, `tn_ai_enabled: false` (already in §5.3)
and every AI feature shows "AI is not configured"; nothing else is affected. You can add the
node later and re-run the installer.

The node is bare-metal Ubuntu, not a VM: vLLM serves the model, LiteLLM puts an
OpenAI-compatible API with a key in front of it, and TrueNorth's AI orchestrator calls
LiteLLM.

**Model rule (owner):** no model from a Chinese developer, with the single exception of GLM.
**[DECISION]** the coder model, `<coder-model>` (its Hugging Face id `<coder-model-repo>`),
served with a **128K** context (`--max-model-len 131072`). Check before you download that the
model's weights, at that context, fit the node's GPU memory.

### C.1 Ubuntu and the NVIDIA driver

Install Ubuntu Server 24.04 on the R7725 as in §1.1 (bare metal: no VM settings; same
choices; the AI VLAN address `<ai-node-ip>`; hostname `<ai-node-name>`). Then:

```bash
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get install -y ubuntu-drivers-common chrony
echo "server <ntp-server> iburst" | sudo tee /etc/chrony/sources.d/site.sources
sudo ubuntu-drivers list --gpgpu
```

Install the newest `-server` driver branch in that list that supports your GPUs
(**[DECISION]** the branch, e.g. `nvidia:<branch>-server`), then reboot:

```bash
sudo ubuntu-drivers install --gpgpu nvidia:<branch>-server
sudo reboot
```

After the reboot:

```bash
nvidia-smi -L                      # every GPU listed
cat /proc/driver/nvidia/version    # the kernel module's version
```

If GPU behaviour later looks wrong, compare `/proc/driver/nvidia/version`,
`modinfo nvidia | grep ^version` and `nvidia-smi -L`: all three must agree. After a
kernel or driver update, reboot before trusting them.

### C.2 Docker and the NVIDIA container toolkit

```bash
sudo apt-get install -y docker.io docker-compose-v2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
```

The last command must list the GPUs from inside a container.

### C.3 The model weights

```bash
sudo mkdir -p /srv/models && sudo chown "$USER" /srv/models
python3 -m venv ~/.venvs/hf
~/.venvs/hf/bin/pip install "huggingface_hub[cli]"
~/.venvs/hf/bin/hf download <coder-model-repo> --local-dir /srv/models/<coder-model-dir>
du -sh /srv/models/<coder-model-dir>
```

A gated model needs `~/.venvs/hf/bin/hf auth login` first, with the site's Hugging Face
token. Air-gapped: download on a connected machine and carry `/srv/models/<coder-model-dir>`
in the transfer kit.

### C.4 vLLM and LiteLLM

**[DECISION]** the image versions: pin a vLLM release (`vllm/vllm-openai:<vllm-tag>`) and a
LiteLLM release (`ghcr.io/berriai/litellm:<litellm-tag>`) that are at least two weeks old
and support your model, and record them.

```bash
sudo mkdir -p /opt/ai && cd /opt/ai
echo "LITELLM_MASTER_KEY=sk-$(openssl rand -hex 24)" | sudo tee .env >/dev/null
sudo chmod 600 .env
```

`/opt/ai/compose.yaml` (`sudo nano /opt/ai/compose.yaml`):

```yaml
services:
  vllm:
    image: vllm/vllm-openai:<vllm-tag>
    restart: unless-stopped
    ipc: host
    volumes:
      - /srv/models:/models:ro
    command:
      - --model=/models/<coder-model-dir>
      - --served-model-name=<coder-model>
      - --tensor-parallel-size=<gpu-count>
      - --max-model-len=131072
      - --gpu-memory-utilization=0.90
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
  litellm:
    image: ghcr.io/berriai/litellm:<litellm-tag>
    restart: unless-stopped
    depends_on: [vllm]
    ports:
      - "4000:4000"
    env_file: .env
    volumes:
      - ./litellm.yaml:/app/config.yaml:ro
    command: ["--config", "/app/config.yaml", "--port", "4000"]
```

`/opt/ai/litellm.yaml` (`sudo nano /opt/ai/litellm.yaml`): the alias `agent` is the model id
TrueNorth asks for.

```yaml
model_list:
  - model_name: agent
    litellm_params:
      model: hosted_vllm/<coder-model>
      api_base: http://vllm:8000/v1
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

Start and test (loading the model takes minutes; watch `sudo docker compose logs -f vllm`
until it reports the server is up):

```bash
cd /opt/ai
sudo docker compose up -d
KEY=$(sudo sed -n 's/^LITELLM_MASTER_KEY=//p' .env)
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $KEY" | jq -r '.data[].id'
curl -s http://localhost:4000/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "agent", "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 256}' \
  | jq -r '.choices[0].message.content'
```

Use `max_tokens` of 200 or more when testing: a reasoning model spends a small budget on its
reasoning and returns empty content.

Allow the TrueNorth VM to the node on TCP 4000 (guide 01 §4.2), and only that.

Operating notes, learned the hard way:

- `litellm.yaml` is a bind mount: after editing it, `sudo docker compose restart litellm`.
  `docker compose up -d` alone does not reload it.
- After a reboot, check `sudo docker compose ps`. A container that was stopped cleanly
  before the reboot stays stopped despite `restart: unless-stopped`; `sudo docker compose up -d`
  brings it back.
- If a service shows `Up` but nothing answers on its port, check `sudo docker port <container>`.
  A container restarted with `docker start` can come back without its port mapping:
  `sudo docker compose up -d --force-recreate <service>` restores it.

### C.5 Wire TrueNorth to it

On the TrueNorth VM:

```bash
cd ~/truenorth/install
ansible-vault edit inventory/group_vars/all/vault.yml     # vault_openai_api_key: the LITELLM_MASTER_KEY value
nano inventory/site.yml                                  # tn_ai_enabled: true, tn_ai_base_url, tn_ai_default_model: agent
ansible-playbook -i inventory/site.yml site.yml -K --ask-vault-pass -e @smoke.yml
```

Preflight probes the endpoint; the installer registers it as the AI backend *Platform AI*;
`99-validate` fails if the endpoint does not list `agent`. The AI Orchestrator page then
shows the backend and its node.

---

## Appendix D — Build golden templates from ISO (reference)

Golden templates are normally **built and proven at a test site, then ported** to each
site ([06 — Operations runbook](06-operations-runbook.md) §4). Build from ISO at a site only
when no proven build exists. This appendix is a map of the build kit, not a substitute for
its documentation:

| Read | For |
|---|---|
| `docs/deployment/vm-build-guide.md` | Every image: media, the Packer path, the manual fallback, build order and time |
| `infra/vsphere/packer/README.md` | Packer details, build records, replication, register-back |
| `install/lab/README.md`, "Work site" | The build network, the depot and the phase-by-phase sequence |
| `docs/vm-build-sheet.md` | What each template and role contains, and why |
| ADR 0012, ADR 0013 | The rules: no keys in templates, host-local copies, promoted builds |

The sequence (`scripts/lab/build-phase.sh`, run from a template-kit checkout on the
machine that builds):

1. A build network with no gateway and no internet, and the depot on it (ADR 0012 §4, §7).
2. `build-phase.sh prereq`: Packer, ISOs, checks.
3. `build-phase.sh base`, `appliances`, `derived`, `roles`, `variants`: each template is
   built, writes its **build record** (`infra/vsphere/packer/logs/<id>.build.json`: build id,
   checksum, ISO checksum), is **replicated** to every range host (`build.sh replicate`),
   test-cloned, and registered.
4. An administrator **promotes** each build on Infrastructure → Golden images.

**Windows licensing (ADR 0012 decision 3).** Templates carry **no product keys**: they use
media marked "No key required" or the public KMS client (GVLK) keys, never evaluation
editions. Activation is applied **at deploy** from a KMS host (and optionally a MAK) that
the site provides. That deploy-time activation setting is to be built when the production
KMS server exists; until then ranges run unactivated (fully functional, with a watermark).

---

## Checklist — install

**VM (§1)**

- [ ] VM `<tn-vm-name>` on management host 1: 32 vCPU / 128 GB / 200 GB + 1.3 TB, VMXNET3 on management, EFI
- [ ] Ubuntu Server 24.04, user `tnadmin`, OpenSSH, **no snaps**
- [ ] System volume grown; data disk mounted at `/srv/truenorth` (≈1.2 TB, in `/etc/fstab`)
- [ ] Upgraded and rebooted; chrony synchronised (`NTPSynchronized=yes`)
- [ ] `dig` answers for `<tn-fqdn>` (forward and reverse), vCenter, the DC

**Control node and vCenter (§2–§3)**

- [ ] ansible-core 2.21.4 in `~/.venvs/tn-install`
- [ ] govc 0.56.0 installed (checksum checked)
- [ ] Role `TrueNorth-Provision` created and assigned to `<svc-truenorth>` at the datacenter (decision recorded)
- [ ] VM folders `TrueNorth/Ranges` and `TrueNorth/Templates` exist

**Release (§4)**

- [ ] Release `<vX.Y.Z>` chosen: ________
- [ ] Checkout at the tag; four release files downloaded
- [ ] cosign `Verified OK` ×2; `sha256sum -c` OK; `git_sha` matches the checkout

**Configuration (§5)**

- [ ] Escrow key pair made off the VM; public half in `install/files/`; private half offline
- [ ] Vault: bind, vCenter and both template build passwords set; encrypted; vault password stored
- [ ] `smoke.yml` vaulted with `<smoke-upn>`
- [ ] `inventory/site.yml` written; every **[DECISION]** recorded
- [ ] `corp-root-ca.cer`, `truenorth.crt`, `truenorth.key` in place; SAN and key match checked
- [ ] LDAPS to the DC verifies (`Verify return code: 0`)
- [ ] Copy of `site.yml`, the encrypted vault, `smoke.yml` and `files/` stored off the VM

**Install (§6)**

- [ ] `00-docker` done; `--check` run with preflight clean
- [ ] `site.yml` run 1 passed (smoke test and validate)
- [ ] `site.yml` run 2 passed; both outputs kept
- [ ] Email parked: banner seen; Students told to sign in themselves

**First sign-in (§7)**

- [ ] Break-glass password in the password safe; signed in by name; authenticator enrolled
- [ ] First AD administrator registered, approved as admin, authenticator enrolled
- [ ] System Health green; Range vCenter listed with its hosts
- [ ] Next: port golden templates (06 §4), then guide 03
