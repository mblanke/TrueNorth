# 01 — Site prerequisites

**For:** the site's infrastructure, network and directory teams, and whoever installs
TrueNorth. **Read before:** [02 — Install guide](02-install-guide.md).

This guide says what a site must have in place before TrueNorth is installed: hardware by
role, the network (VLANs, switch and firewall), DNS, the certificate authority, Active
Directory, and the accounts the site provides. The install itself starts in guide 02.

Conventions used in every guide:

- `<angle-brackets>` are placeholders. Replace them with your site's values. No guide
  holds a real address, password or key, and none should.
- **[DECISION]** marks a step where a person must choose. Record the choice in the site
  record (the checklist at the end of each guide is a good place).
- Words such as *range*, *range template*, *golden template* and *Student* are used as
  `docs/GLOSSARY.md` defines them.

---

## 1. What TrueNorth needs, by role

TrueNorth is one Ubuntu VM that runs the whole platform (web app, API, workers, identity,
databases, telemetry and monitoring) as Docker Compose services. It builds ranges by
cloning golden templates on vSphere hosts through vCenter. Everything else on this page
supports one of those two things.

| Role | What it does | Minimum | Notes |
|---|---|---|---|
| **Management host** (1, a second strongly recommended) | Runs the TrueNorth VM, vCenter and, if the site has no AD, the DC VM | ESXi 8.0, 16 physical cores, 256 GB RAM, 2 TB local NVMe | The TrueNorth VM alone starts at 32 vCPU / 128 GB / 1.5 TB (guide 02 §1). The second host is where backups are copied and where TrueNorth is rebuilt after a host loss |
| **Range hosts** (2 or more) | Run the ranges | ESXi 8.0, local datastore per host | Ranges are sized for realism, not to a VM cap; CPU is usually the limit (§1.3). Each host's local datastore holds a copy of every golden template |
| **vCenter** | The one API TrueNorth drives | vCenter Server 8.0 (VCSA) | One datacenter; range hosts in their own cluster |
| **GPU AI node** (optional) | Serves the language model the AI features use | Bare-metal Ubuntu 24.04, NVIDIA GPUs with enough memory for the chosen model at a 128K context | Without it, install with `tn_ai_enabled: false`: AI features show "not configured" (guide 02 Appendix C) |
| **Switching** | Carries management, vMotion, the range uplink and the range VLAN block | 25GbE to every ESXi host; a trunk that can carry the whole range VLAN block (§3) | Two switches, one host NIC to each, survive a switch loss |
| **Firewall** | Routes management, denies everything to and from range networks | A routed, zone-based firewall | §4 |
| **UPS** | Lets hosts and storage shut down cleanly | Runtime for an orderly shutdown of every host | Size it to the measured draw of the kit |

There is **no shared storage** in this design. Every host uses its own local datastore.
Consequences you must plan for:

- vSphere HA cannot restart a VM on another host. Losing the TrueNorth host means
  rebuilding the TrueNorth VM on the second management host and restoring the nightly
  backup ([06 — Operations runbook](06-operations-runbook.md) §3).
- Golden templates are copied to every range host's datastore (*host-local template
  copies*, `<id>@<host>-<yyyymmdd>`), and ranges clone the copy on the host they are placed
  on.
- vMotion between hosts copies disks over the network (shared-nothing), so give vMotion its
  own VLAN on the 25GbE fabric.

### 1.1 Worked example: the "DND DSG Mobile" 9U kit

The reference hardware. Treat it as a minimum concept, not a bill of materials: any kit that
fills the same roles works.

| Unit | Role in TrueNorth | Configuration |
|---|---|---|
| Dell R6625 "Hypervisor Plane" | Management host 1: runs the **TrueNorth VM** | 2× AMD EPYC 9654, 1 TB RAM, 15.36 TB NVMe, ESXi 8.0 |
| Dell R6625 "Control Plane" | Management host 2: vCenter, the backup target VM, the DC VM if the site has none, and the restore host | ESXi 8.0 |
| 4× Dell R6625 compute nodes | Range hosts | ESXi 8.0, one local datastore each |
| Dell R7725 GPU node | Optional AI node | Bare-metal Ubuntu 24.04, NVIDIA driver, vLLM and LiteLLM (guide 02 Appendix C) |
| 2× Dell S5248F-ON (SONiC) | 25GbE fabric: host uplinks, management, vMotion, range uplink, range VLAN block | One 25GbE port from each host to each switch; an inter-switch trunk (§3.2) |
| Dell E3248 (10G) | Out-of-band and management access: iDRAC and the management uplink | Access ports, no range VLANs |
| Palo Alto firewall | Routes the management networks; denies every range network | §4.2 |
| UPS | Orderly shutdown | — |

TrueNorth's VM starts at **32 vCPU / 128 GB RAM / 1.5 TB** on the Hypervisor Plane host.
That leaves room on that host to grow the VM; do not place range VMs on either management
host (the installer's `tn_vsphere_exclude_hosts`, guide 02 §5.3).

> `docs/dell-netapp-mobile-kit-inventory.md` is an earlier planning inventory (NetApp arrays,
> a different firewall model, 15U cases). Where it disagrees with this table, this table is
> the owner's current design.

**[DECISION]** Where vCenter runs. The recommended layout puts vCenter on the Control Plane
host and TrueNorth on the Hypervisor Plane host, so that losing either host leaves the other
service running.

### 1.2 Management VMs at a glance

| VM | Host | Size | Built from |
|---|---|---|---|
| `<tn-vm-name>` (TrueNorth) | Management host 1 | 32 vCPU / 128 GB / 200 GB system + 1.3 TB data | Guide 02 §1 (by hand, from the Ubuntu ISO) |
| vCenter (VCSA) | Management host 2 (recommended) | VMware's "small" size or larger | VMware's installer |
| `<backup-vm-name>` (backup target) | Management host 2 | 2 vCPU / 4 GB / 40 GB system + a data disk at least 2× `tn_backup_max_total_gb` | Ubuntu 24.04, built as 06 §3.2 says |
| `<dc-vm-name>` (only without a site AD) | Management host 2 | 2 vCPU / 8 GB / 80 GB | Guide 02 Appendix B |
| Depot (optional) | Management host 1 or 2 | 4 vCPU / 16 GB / 100 GB + data disk | `install/lab/depot.yml`; only if ranges install software at deploy time (§3.3) |

### 1.3 Sizing range hosts

Range sizes come from the shipped range templates and are deliberately realistic; capacity
is governed by quotas, not a VM cap. CPU is normally the bottleneck. The provisioner
allows `tn_vsphere_max_vcpu_per_thread` (default 4) vCPUs per hardware thread on a host.
To see how many of each shipped range fit on your range hosts, run this on the TrueNorth
VM after the install (the management VMs are on their own hosts, hence `0` for them):

```bash
cd /srv/truenorth/app
python3 scripts/range-capacity.py --hosts <range-host-count> \
  --threads-per-host <hardware-threads-per-range-host> --ram-per-host-gb <ram-gb-per-range-host> \
  --mgmt-vcpu 0 --mgmt-ram-gb 0 --datastore-gb <total-local-datastore-gb-of-all-range-hosts>
```

Watch CPU ready time in vCenter once Students are working: above about 10 %, they will
feel it.

---

## 2. Network design

TrueNorth's isolation model is **`vlan_trunk_no_l3`**:

- A reserved block of VLANs (the *range VLAN pool*, for example **2000–2999**) is trunked to
  every range host's range port group switch (the range vDS).
- **No device has a layer-3 interface on any VLAN in the pool**: no switch virtual interface
  (SVI), no firewall sub-interface, no DHCP, no gateway. A range's only exit is the edge
  firewall TrueNorth deploys inside the range.
- The site firewall **denies** traffic to and from the range networks, with one permitted
  exception: range firewalls reaching the depot, if the site runs one (§3.3).
- TrueNorth proves isolation per range zone (range certification S4): once a range is ready,
  a VM in each range network probes site addresses that must be unreachable. This is
  `tn_range_isolation_check`, which starts at `warn` and moves to `enforce` once the network
  team signs the model off (06 §6).

### 2.1 Reference VLAN plan

**[DECISION]** The VLAN numbers and subnets are examples. Choose values that do not clash
with the site's, and give the same numbers to the switch, the firewall, vCenter and the
installer.

| VLAN | Name | Subnet | Layer 3 | Who is on it |
|---:|---|---|---|---|
| `<vlan-mgmt>` (e.g. 10) | Management | `<mgmt-subnet>/24` | Firewall, gateway `<mgmt-gw>` | ESXi management, vCenter, the TrueNorth VM, the DC, the backup target, the depot's management NIC |
| `<vlan-oob>` (e.g. 11) | Out-of-band | `<oob-subnet>/24` | Firewall, admin access only | iDRAC (E3248 access ports) |
| `<vlan-vmotion>` (e.g. 20) | vMotion | `<vmotion-subnet>/24` | **None** | ESXi vMotion vmkernel ports |
| `<vlan-ai>` (e.g. 30, optional) | AI | `<ai-subnet>/24` | Firewall | The GPU AI node |
| `<vlan-uplink>` (e.g. 40, optional) | Range uplink | `<uplink-subnet>/24` | Firewall (deny-all, §4.2) | Range edge firewalls' WAN NICs; the depot's range NIC |
| `<pool-start>`–`<pool-end>` (e.g. 2000–2999) | Range VLAN pool | Chosen per range template | **None, anywhere** | Range VMs only, on port groups TrueNorth creates (`tn-<range8>-v<vlan>`) |

Rules:

- The pool must not contain any of the site's own VLANs. If it does, list them in
  `tn_vsphere_reserved_vlans` so no range is ever given one. TrueNorth also reads the range
  vDS before every allocation and keeps off any VLAN something else already carries.
- No range template subnet may overlap a site subnet: the installer's
  `tn_range_reserved_cidrs` lists the management, out-of-band, AI and uplink subnets, and
  TrueNorth refuses templates that overlap them.
- **MAC learning must be off on the pool VLANs used for remote port mirroring** (RSPAN),
  or sensors in SOC ranges miss traffic from other hosts. Which pool VLAN becomes the RSPAN
  VLAN is only known after a build, so the simplest rule is to turn learning off for the
  whole pool. If the switch cannot do that, set `tn_vsphere_placement: per-range-host`
  (06 §8).

### 2.2 vSphere networking

| Object | Setting |
|---|---|
| Range vDS (`tn_vsphere_range_dvs`, e.g. `<range-vds>`) | Uplinks on the 25GbE ports of every range host; MTU 9000 if the switches carry it end to end, else 1500. TrueNorth creates and deletes the per-range port groups on it |
| Management port group (`tn_vsphere_mgmt_network`) | VLAN `<vlan-mgmt>`. TrueNorth **refuses** to attach a range VM to it |
| vMotion | A vmkernel port per host on `<vlan-vmotion>` |
| Range uplink port group (`tn_vsphere_range_uplink_network`, optional) | VLAN `<vlan-uplink>`. Created by you, never by TrueNorth |
| Port group security | TrueNorth sets range port groups itself: promiscuous mode, forged transmits and MAC changes off, except promiscuous on the monitoring port group of a SOC range |

Name each host's local datastore `<host-short-name>-local` (for example `rh01-local`). The
template tools assume that pattern (`RANGE_DATASTORE_FMT`, 06 §4); another pattern works if
you pass it every time.

---

## 3. Switch configuration

### 3.1 Generic checklist (any switch)

- [ ] Every range host's 25GbE ports are **trunks** carrying `<vlan-vmotion>`,
      `<vlan-uplink>` (if used) and the whole range pool `<pool-start>`–`<pool-end>`.
- [ ] Management VLAN on the host ports that carry ESXi management.
- [ ] The inter-switch link carries every VLAN above.
- [ ] The firewall's trunk carries **only** the routed VLANs: management, out-of-band, AI,
      range uplink. It does **not** carry the range pool.
- [ ] **No SVI / IP interface on any pool VLAN**, and no DHCP relay or helper on them.
- [ ] MAC learning off on the pool VLANs (§2.1), or a decision to use per-range-host
      placement.
- [ ] Storm control or broadcast limits on host ports, so one misbehaving range cannot
      flood the fabric.
- [ ] Configuration saved and backed up.

### 3.2 Dell S5248F-ON with SONiC (reference kit)

The commands below are the community SONiC Click CLI, run on each switch over SSH as the
admin user. Port names (`Ethernet0`, …) depend on the platform's port map: list them first
with `show interfaces status` and substitute yours. **Not yet run on the kit's switches**:
the network team should dry-run them on one switch and record the SONiC version they used.
On Dell Enterprise SONiC (the Klish `sonic-cli`), the equivalent is a VLAN range on the
trunk (`switchport trunk allowed Vlan add <pool-start>-<pool-end>`); confirm the exact form
for your release.

```bash
# Variables for this switch. [DECISION] port names and VLAN ids.
HOST_PORTS="Ethernet0 Ethernet1 Ethernet2 Ethernet3 Ethernet4 Ethernet5"   # one per ESXi host
ISL_PORTS="Ethernet48 Ethernet52"                                           # to the other S5248F
FW_PORT="Ethernet56"                                                        # to the firewall
SITE_VLANS="10 11 20 30 40"                                                 # mgmt oob vmotion ai uplink
POOL_START=2000
POOL_END=2999

# Inter-switch port channel
sudo config portchannel add PortChannel1
for p in $ISL_PORTS; do sudo config portchannel member add PortChannel1 "$p"; done

# Site VLANs: tagged on host ports and the ISL. The firewall gets the routed ones only.
for v in $SITE_VLANS; do
  sudo config vlan add "$v"
  for p in $HOST_PORTS PortChannel1; do sudo config vlan member add "$v" "$p"; done
done
for v in 10 11 30 40; do sudo config vlan member add "$v" "$FW_PORT"; done

# The range VLAN pool: tagged on host ports and the ISL, never on the firewall port,
# and NO IP interface. This loop is slow (one call per VLAN and port); let it finish.
for v in $(seq "$POOL_START" "$POOL_END"); do
  sudo config vlan add "$v"
  for p in $HOST_PORTS PortChannel1; do sudo config vlan member add "$v" "$p"; done
done

sudo config save -y
```

Check, on each switch:

```bash
show vlan brief | head -40          # pool VLANs list host ports and PortChannel1 only
show ip interfaces                  # must list no Vlan<pool> interface
show interfaces portchannel         # PortChannel1 up, both members selected
```

### 3.3 The depot (optional) and the range uplink

The depot is a package mirror VM (Nexus with a Chocolatey feed, and apt-cacher-ng) that
range VMs install software from at deploy time. It is optional:

- **Without a depot**, leave `tn_vsphere_range_uplink_network` empty. Ranges then get no
  uplink NIC at all and are fully isolated; the `services` a range template lists for a VM
  are skipped with a warning. Bake software into golden templates instead.
- **With a depot**, the depot gets a NIC on the range uplink VLAN, range edge firewalls get
  their WAN NIC there, and the only permitted connection is range firewall → depot on TCP
  `8081` (Nexus) and `3142` (apt-cacher-ng). The depot's own host firewall enforces the same
  (`install/lab/roles/tn_depot`). Building the depot is described in `install/lab/README.md`.

---

## 4. Firewall configuration

### 4.1 Generic checklist (any firewall)

- [ ] Default deny between zones.
- [ ] **Range uplink zone: deny everything** in and out, logged. If the depot is not on the
      uplink VLAN itself (it is routed elsewhere), one rule above the deny: uplink subnet →
      depot address, TCP 8081 and 3142 only.
- [ ] No interface, rule, NAT or route for any VLAN in the range pool.
- [ ] Management zone → internet: only what an online install needs (§4.3), ideally through
      the site proxy.
- [ ] Management zone → AI zone: the TrueNorth VM to the GPU node on TCP `4000` (LiteLLM).
- [ ] Out-of-band zone reachable from the administrators' network only.
- [ ] No management-plane access (ping, SSH, web) on the range uplink interface.
- [ ] Rule hits logged, so a range trying to escape shows in the firewall log.

### 4.2 Palo Alto (reference kit)

PAN-OS CLI in configuration mode (`configure`). Interface names, addresses and the router
name are examples; on PAN-OS with the Advanced Routing Engine, attach interfaces to a
`logical-router` instead of the `virtual-router`. **Not yet run on the kit's firewall**: the
network team should review it before commit.

```text
configure
# Trunk to the switch: one layer-3 sub-interface per routed VLAN. No range pool VLAN here.
set network interface ethernet ethernet1/1 layer3
set network interface ethernet ethernet1/1 layer3 units ethernet1/1.10 tag 10 ip <mgmt-gw>/24
set network interface ethernet ethernet1/1 layer3 units ethernet1/1.11 tag 11 ip <oob-gw>/24
set network interface ethernet ethernet1/1 layer3 units ethernet1/1.30 tag 30 ip <ai-gw>/24
set network interface ethernet ethernet1/1 layer3 units ethernet1/1.40 tag 40 ip <uplink-gw>/24

set zone TN-MGMT network layer3 ethernet1/1.10
set zone TN-OOB network layer3 ethernet1/1.11
set zone TN-AI network layer3 ethernet1/1.30
set zone TN-RANGE-UPLINK network layer3 ethernet1/1.40
set network virtual-router default interface [ ethernet1/1.10 ethernet1/1.11 ethernet1/1.30 ethernet1/1.40 ]

# Objects
set address tn-platform ip-netmask <tn-ip>/32
set address tn-ai-node ip-netmask <ai-node-ip>/32
set service tn-litellm protocol tcp port 4000

# The TrueNorth VM may reach the AI node's LiteLLM port.
set rulebase security rules tn-platform-to-ai from TN-MGMT to TN-AI source tn-platform destination tn-ai-node source-user any category any application any service tn-litellm action allow log-end yes

# Nothing leaves or enters the range uplink zone. Keep these two at the top.
set rulebase security rules tn-range-uplink-out-deny from TN-RANGE-UPLINK to any source any destination any source-user any category any application any service any action deny log-end yes
set rulebase security rules tn-range-uplink-in-deny from any to TN-RANGE-UPLINK source any destination any source-user any category any application any service any action deny log-end yes
move rulebase security rules tn-range-uplink-in-deny top
move rulebase security rules tn-range-uplink-out-deny top
commit
```

The depot's range NIC sits on the uplink VLAN itself, so range firewall → depot traffic
never crosses the Palo Alto and needs no allow rule. If your depot is routed instead, add
one rule above the two denies: from `TN-RANGE-UPLINK` to the depot's zone, destination the
depot address, services TCP 8081 and 3142, action allow.

Add the management rules your site already uses (administrators to management, management
to the internet for the online install, NTP, DNS). The flows TrueNorth needs are listed in
§4.3.

### 4.3 Flows TrueNorth needs

| From | To | Port | Why |
|---|---|---|---|
| TrueNorth VM | vCenter | TCP 443 | Provisioning, discovery |
| TrueNorth VM | Every ESXi host's management address | TCP 443 | Guest file transfer during range builds |
| TrueNorth VM | DC(s) | TCP 636 (LDAPS), TCP/UDP 53 (DNS), UDP 123 (NTP) | Directory sign-in, name resolution, time |
| TrueNorth VM | GPU AI node | TCP 4000 | AI features (optional) |
| TrueNorth VM | Backup target VM | TCP 22 | Nightly copy of the backups (06 §3) |
| Administrators, instructors, Students | TrueNorth VM | TCP 443 (TCP 80 redirects) | The web app; also TCP 8443 only if Moodle is enabled |
| Administrators | TrueNorth VM, backup target, GPU node | TCP 22 | Administration |
| TrueNorth VM, **online install only** | `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`, `tuf-repo-cdn.sigstore.dev` (signature check), `ghcr.io`, `pkg-containers.githubusercontent.com`, `download.docker.com`, `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`, `quay.io`, `cdn01.quay.io`, `gcr.io`, `archive.ubuntu.com`, `security.ubuntu.com`, `pypi.org`, `files.pythonhosted.org`, `galaxy.ansible.com` | TCP 443 (and 80 for Ubuntu's mirrors) | Release files, images, Docker, Ubuntu and Python packages, Ansible collections |
| GPU AI node, **online install only** | `nvidia.github.io`, `huggingface.co` (and its CDN), `registry-1.docker.io`, `ghcr.io`, Ubuntu's mirrors | TCP 443 | Driver toolkit, model weights, vLLM and LiteLLM images |
| TrueNorth VM (worker), **online feeds only** | `endoflife.date`, `www.cisa.gov`, `services.nvd.nist.gov` | TCP 443 | Daily content feeds (06 §7). GET requests only; no site data is sent |

An air-gapped site needs none of the internet flows: guide 02 Appendix A.

---

## 5. DNS, time and certificates

### 5.1 DNS

The site DNS (normally AD-integrated) must hold, before the install:

- [ ] `A` and `PTR`: `<tn-fqdn>` → `<tn-ip>`. TrueNorth is reached **by name only**; opening
      it by IP address loads an empty page, because sign-in redirects are bound to the name.
- [ ] `A` and `PTR` for vCenter (`<vcenter-fqdn>`) and every ESXi host.
- [ ] `A` for each DC (`<dc-fqdn>`), and the AD SRV records (AD creates these).
- [ ] Forwarders for internet names, for an online install.
- [ ] Workstations that use TrueNorth resolve `<tn-fqdn>` through the same DNS.

### 5.2 Time

Every ESXi host, vCenter, the DCs and the TrueNorth VM synchronise with the same NTP
source (normally the DC holding the PDC emulator role, or the site's NTP servers). The
installer refuses to continue on an unsynchronised clock: token validation and Kerberos both
fail on skew, and the errors look like password problems.

### 5.3 Certificate authority

The site CA (for example AD CS) provides:

- [ ] Its **root CA certificate**, exported as DER or Base64 (the installer accepts both).
      Name the file `corp-root-ca.cer`. TrueNorth trusts it for LDAPS to the DCs and for
      its own certificate.
- [ ] A **server certificate for `<tn-fqdn>`**: Subject Alternative Name `DNS:<tn-fqdn>`,
      Extended Key Usage server authentication, RSA 2048 or larger. Delivered as two PEM
      files: `truenorth.crt` (the certificate, then any intermediate CA certificates: the
      full chain, root excluded) and `truenorth.key` (the private key, unencrypted).
- [ ] Each DC's **LDAPS certificate** must name the DC's FQDN (`<dc-fqdn>`); TrueNorth
      verifies the host name.

The installer can run with a self-signed certificate for a first bring-up
(`tn_tls_mode: selfsigned`), but browsers warn and it is not for Students.

---

## 6. Active Directory

TrueNorth joins the site's AD **read-only**: Keycloak federates AD users over LDAPS, and
TrueNorth never writes to the directory. A site without AD can install with local accounts
(`tn_ldap_enabled: false`) or stand up a minimal DC first (guide 02 Appendix B).

### 6.1 What the directory must hold

**[DECISION]** The OU layout. TrueNorth needs one OU for its users, one for its groups and
one for its service accounts. The example below mirrors the reference lab; your DNs go into
the installer's `tn_ldap_*` variables (guide 02 §5.3).

| Object | Example DN | Notes |
|---|---|---|
| Users OU | `OU=Users,OU=TrueNorth,<domain-dn>` | `tn_ldap_users_dn`. Every person who will sign in, directly in this OU or below it |
| Groups OU | `OU=Groups,OU=TrueNorth,<domain-dn>` | `tn_ldap_groups_dn`. Holds the six groups below |
| Service accounts OU | `OU=ServiceAccounts,OU=TrueNorth,<domain-dn>` | Holds the bind account |

The six security groups (global, security). The installer **fails** unless all six are
found in the groups OU:

| Group | TrueNorth role it suggests |
|---|---|
| `TN-Platform-Admins` | admin |
| `TN-Range-Operators` | range_ops |
| `TN-Instructors` | instructor |
| `TN-Content-Authors` | instructor |
| `TN-Students` | student |
| `TN-Observers` | observer |

Rules the directory team must know:

- **Membership must be direct.** Nested groups are not expanded: a user in a group that is
  itself a member of `TN-Students` is not a Student to TrueNorth.
- **A group suggests a role; an approval grants it.** An administrator approves each person
  in TrueNorth and chooses the role. Removing someone from a TN-* group does not remove
  their TrueNorth role; disabling their AD account does (they cannot sign in at once, and
  the nightly AD status sync deactivates the TrueNorth account, 06 §9).
- Every account that signs in needs a `mail` attribute that is unique across the directory.
- Administrators, instructors, range operators and observers sign in with a password **and**
  an authenticator-app code (TOTP). They need a phone or desktop authenticator app.
  Students sign in with a password only.

### 6.2 LDAPS

- [ ] Each DC serves LDAPS on TCP 636 with a certificate from the site CA naming the DC's FQDN.
- [ ] From the TrueNorth VM's subnet, TCP 636 to the DC is allowed.

---

## 7. Accounts the site provides

Names and roles only. Passwords and keys go into the installer's encrypted vault on the day
(guide 02 §5.2) and never into a document, ticket or chat.

| Account | Where | Role / rights | Used by |
|---|---|---|---|
| `<svc-truenorth>` (e.g. `svc-truenorth@vsphere.local`) | vCenter | Custom role `TrueNorth-Provision`, assigned at the **datacenter**, propagate on (guide 02 §3.2) | TrueNorth's workers, to build ranges |
| `<vcenter-admin>` | vCenter | Administrator | The installer of the day: creating the role, porting golden templates |
| `TrueNorth Keycloak Bind` (`<bind-dn>`) | AD, service accounts OU | Domain user, read-only, password does not expire (or a rotation plan) | Keycloak's LDAPS federation |
| `<first-admin-upn>` | AD, users OU, in `TN-Platform-Admins` | Named person | The first AD administrator, approved by the local break-glass account after the install |
| `<smoke-upn>` | AD, users OU, in `TN-Observers` only, no authenticator enrolled | Non-privileged test account | The installer's sign-in check (proves AD claims arrive) |
| `tnadmin` | The TrueNorth VM (local Linux user) | sudo | The person running the installer |
| Local break-glass administrator `<breakglass-upn>` | Keycloak (created by the installer, **outside** the AD domain, e.g. `tnadmin@<tn-vm-name>.local`) | TrueNorth platform administrator | Recovery when AD, LDAPS or the bind account fails |
| `<bkp-user>` | The backup target VM (local Linux user) | Writes to the backup directory only | The nightly copy of backups |

Keys the site generates and keeps **offline**, never on the TrueNorth VM:

| Key | Purpose | Guide |
|---|---|---|
| Backup escrow key pair (RSA 4096) | The public half encrypts every backup's secrets; the private half is needed to rebuild after a host loss | 02 §5.1 |
| Feed bundle signing key (Ed25519) | Air-gapped sites only: signs the content feed bundles | 06 §7 |
| Ansible vault password | Opens the installer's secrets file | 02 §5.2 |

---

## Checklist — site prerequisites

Print this page and tick it off with the site's teams before install day.

**Hardware**

- [ ] Management host 1 (TrueNorth) on ESXi 8.0, with room for 32 vCPU / 128 GB / 1.5 TB
- [ ] Management host 2 (vCenter, backup target, restore host) on ESXi 8.0
- [ ] Range hosts on ESXi 8.0, each with one local datastore named `<host>-local`
- [ ] vCenter 8.0: one datacenter, a range cluster, management hosts outside it
- [ ] GPU AI node racked, or decision recorded: install without AI
- [ ] UPS sized and tested

**Network**

- [ ] VLAN plan recorded: management, out-of-band, vMotion, (AI), (range uplink), range pool `____–____`
- [ ] Range pool trunked to every range host and the ISL; not to the firewall
- [ ] No layer-3 interface on any pool VLAN (switch `show ip interfaces` checked)
- [ ] MAC learning off on the pool, or per-range-host placement decided
- [ ] Range vDS created on the range hosts; management port group name recorded
- [ ] Firewall: range uplink zone denied both ways and logged; no pool VLAN on the firewall
- [ ] Firewall: TrueNorth → vCenter, ESXi hosts, DCs, (AI node), backup target allowed
- [ ] Online install: internet flows of §4.3 allowed, or air-gapped transfer kit planned
- [ ] Depot: built and on the uplink VLAN, or decision recorded: no depot

**DNS, time, certificates**

- [ ] `A` + `PTR` for `<tn-fqdn>`, vCenter, every ESXi host
- [ ] All hosts and VMs on the same NTP source
- [ ] `corp-root-ca.cer` exported
- [ ] `truenorth.crt` (full chain, PEM) and `truenorth.key` (PEM) issued for `<tn-fqdn>`
- [ ] DC LDAPS certificates name the DCs' FQDNs

**Active Directory**

- [ ] Users, groups and service-accounts OUs created; DNs recorded
- [ ] Six TN-* groups created in the groups OU
- [ ] Bind account created (read-only)
- [ ] First AD administrator in `TN-Platform-Admins` (direct membership)
- [ ] Smoke-test account in `TN-Observers` only
- [ ] Every account that will sign in has a unique `mail`

**Accounts and keys**

- [ ] vCenter service account created; vCenter administrator available on the day
- [ ] Backup escrow key pair generated; private half stored offline with two custodians
- [ ] Vault password chosen and stored with the escrow key's custodians
