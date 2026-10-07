# docs/deployment

Facts about the vSphere estate TrueNorth is deployed onto, and the settings derived from
them. Generated files are overwritten on each run; edit the script, not the output.

| File | What it is |
|---|---|
| `vsphere-inventory.json` | Raw discovery output (`inventory`) plus the computed `analysis`. Keep a copy to diff against later (`--baseline`). |
| `vsphere-inventory.md` | Human tables: vCenter, licences, hosts, NICs, vSwitches/vDS, VLANs, datastores, ISO coverage, templates, Content Libraries, key providers, capacity, and the §17 readiness gate. |

What to collect and why: `truenorth-ai-vsphere-pack/truenorth-vsphere-discovery.md`.

## Run discovery

Discovery is **read-only**: it reads properties, queries licences, and lists files on
datastores. It never creates, changes or deletes anything (see the header of
`scripts/vsphere-discover.py` and its `ALLOWED_TASKS`). A read-only vCenter role is enough.

```bash
VSPHERE_PASSWORD=... .venv/bin/python scripts/vsphere-discover.py --host <vcenter> --user <user> --insecure
```

Useful options:

- `--range-vlans 100-199`: the VLAN block reserved and trunked for TrueNorth ranges. It
  becomes `VSPHERE_VLAN_POOL` if no in-use VLAN falls inside it; otherwise the script
  suggests a free block of 100 between 900 and 3999.
- `--iso-path "[esx01-local] ISO"`: search only that folder for ISOs (default: every datastore).
- `--baseline docs/deployment/vsphere-inventory.json`: add a "changes since baseline" section.
  Copy the old file somewhere else first, because the run overwrites it.
- `--out <dir>`: write somewhere other than `docs/deployment`.

If `VSPHERE_PASSWORD` is unset the script prompts for it. The password is never printed
or written. `--insecure` skips TLS verification, for a vCenter with a self-signed certificate.

On a fresh machine without the repo venv, install the two dependencies first:

```bash
pip install pyvmomi httpx
```

The script prints the recommended `VSPHERE_PLACEMENT`, `VSPHERE_RANGE_SWITCH_MODE` and
`VSPHERE_VLAN_POOL` values to stdout when it finishes.
