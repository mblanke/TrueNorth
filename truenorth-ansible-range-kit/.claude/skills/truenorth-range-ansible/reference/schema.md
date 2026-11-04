# Range definition schema

File: `ranges/<ID>.yml`. The file name must equal `range.id`.

```yaml
range:
  id: R-0001              # ^[A-Z][A-Z0-9]{1,11}(-[0-9]{1,4})?$  e.g. CANARY, EXCOURSE, R-0001
  owner: <name>           # TBD blocks the build
  exercise: <name>
  expires: 2026-12-31     # an expired range cannot be built
  students: 1             # 1..site.max_students; >1 creates <ID>-S01..Snn instances
  description: <text>

use:                      # blueprints, composed in order
  - blueprint: ad_forest  # blueprints/ad_forest.yml
    as: corp              # alias, ^[a-z][a-z0-9]{0,9}$, namespaces names: corp-lan, corp-dc01
    params: {workstations: 3}

networks:                 # extra range-level networks (not namespaced)
  - name: span            # ^[a-z][a-z0-9]{0,15}$
    gateway: false        # default true: reserves .1 for a router
    security: {promiscuous: true}   # default all false

vms:                      # extra range-level VMs (not namespaced)
  - name: ids01           # ^[a-z][a-z0-9]{0,15}$; use {nn} with count for numbered sets
    count: 1
    template: ubuntu2404  # alias in config/templates.yml
    role: sensor          # selects files/<os_family>/<role>.(sh|ps1) if present
    cpu: 4
    memory_gb: 8
    disk_gb: 100          # grows the template disk; never shrinks it
    guest_config: tools   # tools (default) | prebaked | ssh | winrm
    host: auto            # default host offset for all nics; 1 = take the gateway
    nics:
      - span                       # network name; inside a blueprint, local names
      - {network: "@corp-lan", host: 50}   # '@' = fully qualified network
    vars: {dns: 10.200.0.10}       # becomes TN_VAR_DNS in the guest; never secrets

validation:
  probe_vm: red-op01      # required; linux; guest access
  checks: [isolation, connectivity, services]   # isolation is always added
  services:
    - {vm: corp-dc01, port: 389}

integrations:             # see integrations.md
  range_activity: {enabled: false, profile: TBD}
  greyspace:      {enabled: false, profile: TBD}
  arc2:           {enabled: false, profile: TBD}

power_after_build: poweredon   # poweredon | poweredoff (validated powered on, then shut down)
source_scenario: {...}         # written by `tools/range scenario`; do not hand-edit
```

## Generated names

| Object | Pattern | Example |
|---|---|---|
| Instance | `<ID>` or `<ID>-Snn` | `EXCOURSE-S03` |
| Portgroup | `TN-<instance>-<network>` | `TN-EXCOURSE-S03-corp-lan` |
| VM | `<instance>-<vm>` | `EXCOURSE-S03-corp-dc01` |
| Guest hostname | `<vm>` | `corp-dc01` (Windows: 15 characters max) |
| Folder | `<folder_root>/<ID>[/<instance>]` | `TrueNorth/Ranges/EXCOURSE/EXCOURSE-S03` |
| Resource pool | `<resource_pool_root>-<ID>` | `TrueNorth-Ranges-EXCOURSE` |
| Tags | `truenorth-range:<ID>`, `truenorth-instance:<instance>` | |

## Addressing

- Instance slot k = VLAN block k (`vlans_per_slot` VLANs) and CIDR block k (`/slot_prefix`) of the pools.
- A slot is skipped if it holds a reserved VLAN, overlaps a reserved CIDR, or collides with
  `discovery/live-network.json`.
- Network i of an instance gets VLAN `block[i]` and subnet i (`/network_prefix`) of the slot block.
- `.1` is the gateway on networks with `gateway: true`. Auto hosts start at `first_host_offset` (.10).
- Explicit `host:` values are reserved before auto assignment and must not collide.
- A VM gets one default route: the gateway of its first NIC on a gateway network. Other NICs get none.
