# Golden images and template promotion

Pristine ranges start with pristine templates. Templates are controlled inputs.

## Lifecycle

```
build (Packer)  ->  accept (checks below)  ->  record provenance  ->  promote (tag)  ->  use
   packer/           human + canary          config/templates.yml    template_promote    ranges
```

1. **Build** with Packer (`packer/`). Inputs are pinned: ISO URL and `iso_checksum` (sha256), and
   package versions where the OS allows it. Output is a vCenter template named `tmpl-<alias>-<build_id>`.
2. **Accept**: the image must pass all checks below.
3. **Record** in `config/templates.yml`: `vcenter_name`, `build_id`, `build_date`, `source_sha256`, `validated: true`.
4. **Promote**: a human approves `TEMPLATES` for `promote`, then
   `ansible-playbook playbooks/template_promote.yml -e template_alias=<alias>`.
5. **Prove**: compile and build CANARY with the new template. Keep the previous template until the canary passes.
6. **Retire** the old template only after no range plan references it (`grep -r <old name> build/*/plan.json`).

## Image acceptance checks

| Check | Linux | Windows |
|---|---|---|
| Unique identity on clone | `/etc/machine-id` empty; SSH host keys removed (regenerate on first boot) | Sysprep generalize (`/generalize /oobe /shutdown`) |
| No credentials baked in | No authorized_keys except a documented break-glass; no saved tokens | No cached credentials or autologon |
| No history or logs | Shell history, `/var/log/*`, `/tmp` cleared | Event logs cleared, `C:\Windows\Temp` cleared |
| No DHCP leases or network config | `/var/lib/dhcp/*` removed; no static config | No static IPs |
| VMware Tools | Installed and running (`open-vm-tools`) | Installed and running |
| One NIC, type matches catalogue | vmxnet3 unless `nic_device_type` says otherwise | same |
| Hardware version | Pinned. Note: virtual hardware 22 (vSphere 9) does not run on ESX 8 hosts | same |
| Hardening baseline | CIS or STIG profile: TBD, confirm which | TBD |
| Guest ops account | Exists, matches the vault variable `guest_user_var` | same |

## Linked clones

`clone_mode: linked` is faster but needs the source to be a VM (not a template) with a snapshot
named `tn-clone-source`. Reset and teardown still work. Do not use linked clones until the source
VM workflow is documented here.
