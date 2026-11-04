# Guest access models

Range networks are isolated, so the control node usually cannot reach guests over IP.

| Model | How Ansible reaches the guest | Status |
|---|---|---|
| `tools` (default) | vCenter guest operations through VMware Tools: upload a script, run it, delete it | Implemented |
| `prebaked` | Nothing to configure; power on and wait for Tools | Implemented |
| `ssh` / `winrm` | Over a temporary build network the control node can reach | **Not implemented**; the task fails with an explanation |

## tools model

- Credentials: the vault variable named by the template's `guest_user_var`.
- NICs are matched by MAC address (read from vCenter after cloning), never by order or name.
- Scripts: `playbooks/roles/range_guest_config/files/<os_family>/common.(sh|ps1)`, then `<role>.(sh|ps1)` if present.
- Environment contract: see `files/README.md`.
- Windows guests are rebooted after configuration so the rename takes effect.
- After configuration every guest is shut down cleanly and snapshotted powered off as `tn-baseline`.

## If you implement ssh/winrm

The build network must be removed from every VM before the baseline snapshot. Otherwise the
range is not isolated and the baseline carries a NIC that breaks reset. Record the design in
this file, add tests, and get the change reviewed before use.
