# Safety model

Three independent layers enforce the same rules. A gap in one is caught by another.

| Layer | Where | Enforces |
|---|---|---|
| Guard hook | `.claude/hooks/guard.py` (PreToolUse on Bash, Write, Edit, Read) | Blocks Claude: unapproved real runs, bypass flags, govc writes, secret access, edits to protected files |
| CLI gates | `tools/range` (`tnrange/gates.py`) | Approval, blackout, plan freshness and buildability, typed teardown confirmation |
| Playbook asserts | `range_load`, `range_preflight`, `range_teardown` | Single range ID, vault credentials present, plan current, reserved VLANs, VLAN collisions, template validation tag, datastore capacity, teardown scope |

## Approvals

- File: `.claude/approvals/<ID>.json`, git-ignored, created by `tools/range approve` run by a human.
- `approve` needs an interactive terminal and the range ID typed back. Claude Code's shell has no terminal.
- Optional but recommended: a signing key (`/etc/truenorth/approval.key`). When it exists, approvals are
  HMAC-signed and unsigned or altered approvals are rejected. Keep the key away from the account Claude
  Code runs as. That is the only boundary that does not depend on pattern matching.
- Bound to the compiled plan hash and to a hash of `playbooks/` and `tools/`. Any change to either needs a new approval.
- Scoped to one range and a list of actions (`build`, `reset`, `teardown`, `promote`, `release`).
- Expires (default 4 hours, maximum 24).
- Template promotion uses the pseudo range `TEMPLATES`.

## Plan integrity

`plan.json` carries `plan_sha256`, the hash of its own content. `verify-plan` checks that
(1) the file still hashes to its stamp, so it has not been edited, (2) it equals a fresh
compile of the current inputs, and (3) for build and reset, the inputs are buildable today.
`range_load` also refuses a `plan` supplied through extra vars, asserts that `tn_root`, `tn_cli` and
`tn_build_dir` resolve to the repo the playbook lives in, and asserts that `tn_action`/`tn_changes` match
the playbook being run. Vault files passed with `-e @vault/...` must be ansible-vault encrypted.

## Teardown scope

1. Lists VMs under the range folder and selects those tagged `truenorth-range:<ID>`.
2. Refuses if any tagged VM is not in the plan. The tag was misapplied or someone added a VM; a human must investigate.
3. Asserts that every portgroup, folder and the pool in the plan sit inside this range's
   namespace (`TN-<ID>[-Snn]-*`, `<folder_root>/<ID>`, `<pool_root>-<ID>`).
4. Deletes tagged VMs by instance UUID, then DRS rules, then planned portgroups that exist,
   then folders only if empty (never `remove_vm_data`), then the resource pool and tags.
5. Verifies that no planned VM remains and writes `build/<ID>/teardown.json`.

VMs are tagged batch by batch as they are cloned, so a build that fails part-way can still be torn down.

## Isolation

Two supported models, set in `site.isolation_mode`:

- `uplink_none`: range portgroups live on a switch with no physical NICs bound. Each instance is
  pinned to one host by a mandatory DRS VM-VM affinity rule, so traffic never needs an uplink.
- `vlan_trunk_no_l3`: range VLANs are trunked on physical uplinks, and the network team confirms
  the physical switches have no L3 interface, route or ACL path for the range VLAN pool.

Either way, the probe proves it. Probe scoring: for TCP, connected or *refused* both mean the
target answered (reachable). Timeout or no route means unreachable. A failed guest operation is
inconclusive and never passes. If the probe's own self-test fails, every isolation result is
inconclusive. Range-internal firewalls should DROP rather than REJECT traffic to isolation
targets; a REJECT looks like an answer and fails the test, which is the safe direction. `site.isolation_probe_targets` should be addresses that answer
when reachable, such as the management gateway, DNS or vCenter, on ports that are known to be open.

## What the hook cannot see

The hook pattern-matches commands. It does not inspect scripts Claude writes and then runs.
The CLI and playbook gates still apply in that case, which is why real changes must go through
`tools/range`. Treat the hook as a seatbelt, not a sandbox.
