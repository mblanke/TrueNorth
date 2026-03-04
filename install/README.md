# TrueNorth Range -- Ansible Install Package

Automated deployment of a TrueNorth Range kit using Ansible.

## COTE Naming Convention

All hosts use: `cote-<env>-<site>-<kit>-<rack>-<role>-<idx>`

| Segment | Example | Description |
|---------|---------|-------------|
| env | prod | Environment (prod, dev, staging) |
| site | ott | Site code (3-letter) |
| kit | alpha | Kit identifier |
| rack | r01 | Rack number |
| role | hv | Role (hv, nas, tor, fw, cp) |
| idx | 01 | Instance index |

## VLAN Layout

| VLAN | ID | Subnet | Purpose |
|------|-----|--------|---------|
| Management | 10 | 10.0.10.0/24 | OOB management |
| Provisioning | 20 | 10.0.20.0/24 | PXE / provisioning |
| Blue Team | 30 | 10.0.30.0/24 | Blue exercise traffic |
| Red Team | 40 | 10.0.40.0/24 | Red exercise traffic |
| Storage | 50 | 10.0.50.0/24 | NFS / iSCSI |
| Services | 60 | 10.0.60.0/24 | Infrastructure services |

## Quick Start

```bash
# 1. Install Ansible
pip install ansible

# 2. Edit inventory
vim inventory.yml

# 3. Run preflight checks
ansible-playbook playbooks/00-preflight.yml

# 4. Full deployment
ansible-playbook site.yml

# 5. Validate
ansible-playbook playbooks/09-validate.yml
```

## Directory Structure

```
install/
  ansible.cfg          # Ansible configuration
  inventory.yml        # Host inventory (COTE naming)
  site.yml             # Master playbook
  group_vars/
    all.yml            # Global variables
    hypervisors.yml    # Proxmox-specific vars
    storage.yml        # Storage-specific vars
  playbooks/
    00-preflight.yml   # Preflight checks
    01-common.yml      # Base packages, NTP, DNS
    02-networking.yml  # VLANs, bridges, MTU
    03-storage.yml     # NFS mounts, Proxmox storage
    04-proxmox.yml     # PVE install, cluster
    05-control-plane.yml # API, web deployment
    06-security.yml    # SSH hardening, firewall
    07-templates.yml   # VM template uploads
    08-smoke-test.yml  # Service health checks
    09-validate.yml    # End-to-end validation
  roles/
    common/            # Base OS configuration
    networking/        # VLAN & bridge setup
    proxmox/           # Proxmox VE deployment
    storage/           # NFS/iSCSI configuration
    control_plane/     # TrueNorth API/web
    security/          # Hardening & firewall
```
