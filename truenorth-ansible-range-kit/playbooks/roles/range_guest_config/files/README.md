# Guest configuration scripts

The `tools` access model uploads and runs scripts through VMware Tools, so it works
on isolated range networks.

For each VM the role runs:

1. `<os_family>/common.(sh|ps1)`: hostname and static addressing, with NICs matched by MAC.
2. `<os_family>/<role>.(sh|ps1)`, if that file exists (for example `windows/domain_controller.ps1`).

Both receive the same environment:

| Variable | Example |
|---|---|
| `TN_HOSTNAME` | `dc01` (short name, no range prefix) |
| `TN_ROLE` | `domain_controller` |
| `TN_INSTANCE` | `EXCOURSE-S02` |
| `TN_NIC_COUNT` | `2` |
| `TN_NIC_<n>` | `00:50:56:aa:bb:cc\|10.200.0.10/24\|10.200.0.1` |
| `TN_VAR_<KEY>` | `TN_VAR_DOMAIN=corp.local` from the definition's `vars:` |

Rules for role scripts:
- Must be idempotent. A second run must change nothing.
- Must exit non-zero on failure. The build stops on the first failure.
- Must not write secrets to disk or logs. If a role needs a secret (e.g. a DSRM
  password), add it to vault and pass it through a dedicated variable. Do not use `TN_VAR_`
  for secrets; those values land in plan.json.
- Must not reach outside the range. There is no internet access.
- Scripts are deleted from the guest after they run.

No role scripts ship with this kit. Role content (AD build-out, vulnerable services,
user emulation agents) is exercise content. It belongs here or comes from ARC².
