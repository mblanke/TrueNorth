---
name: truenorth-range-ansible
description: Build, reset, validate and tear down TrueNorth cyber ranges (VMs, networks, multi-student ranges) with Ansible on vCenter. Use for any task that creates or changes range definitions, blueprints, VMs, portgroups, templates or the range playbooks in this repo.
---

# TrueNorth range automation

Ranges are code. A definition in `ranges/<ID>.yml` is compiled into a hash-stamped plan,
`build/<ID>/plan.json`. Ansible only executes that plan. It never decides VLANs,
addresses or names. If something about a range is wrong, fix the definition, a blueprint
or the site config, recompile, and run again. Never patch vCenter by hand.

## The one entry point

Every operation goes through `tools/range`. Run `tools/range -h` for the full list.

| Step | Command | Needs approval |
|---|---|---|
| Check a definition | `tools/range validate <ID>` | no |
| Compile the plan | `tools/range compile <ID> --write-allocation` | no |
| Show the plan | `tools/range plan <ID>` | no |
| Dry run against vCenter | `tools/range check <ID>` | no |
| Build | `tools/range build <ID>` | **yes** (`build`) |
| Reset to baseline | `tools/range reset <ID>` | **yes** (`reset`) |
| Re-run validation | `tools/range validate-live <ID>` | no (blackout applies) |
| Drift check | `tools/range drift <ID>` | no |
| Evidence record | `tools/range manifest <ID>` | no |
| Teardown | `tools/range teardown <ID> --confirm <ID>` | **yes** (`teardown`) |
| Scenario to range | `tools/range scenario <file> --id <ID> --expires YYYY-MM-DD` | no |

Approvals are created by a human in their own terminal:
`tools/range approve <ID> --action build [--action teardown] --hours 4`.
An approval is tied to the exact compiled plan. If you recompile and the plan changes,
the approval no longer applies and the human must review and approve again.
You cannot create, edit or bypass approvals. The guard hook blocks it. When a real run
is needed, finish everything up to `check`, show the plan, and ask the human to approve.

## Workflow for any range change

1. Edit `ranges/<ID>.yml`, a blueprint in `blueprints/`, or (rarely) `config/`.
2. `tools/range validate <ID>`. Fix every error it lists.
3. `tools/range compile <ID> --write-allocation`. Read the BUILDABLE line, blockers and warnings.
4. Show the human the plan text: instances, portgroups with VLAN/CIDR, VMs with addresses, totals.
5. `tools/range check <ID>`. This needs vault credentials (`TNRANGE_ANSIBLE_ARGS`). Report the outcome.
6. Ask for approval. After it is granted: `tools/range build <ID>`.
7. Report the manifest verdict from `build/<ID>/manifest.md`. A build is done only when the verdict is PASS.

Do not write VLAN numbers, CIDRs, IPs or portgroup names into definitions. The compiler
allocates them deterministically from the site pools and records slots in
`ranges/.allocations.json`. That file is committed, so the same definition always gets the
same addressing.

## Definitions and blueprints

- Schema, naming and examples: [reference/schema.md](reference/schema.md)
- Blueprints, parameters and composition: [reference/blueprints.md](reference/blueprints.md)
- Start new ranges from `ranges/CANARY.yml` (minimal) or `ranges/EXCOURSE.yml` (multi-student, sensor network).
- Prefer composing blueprints over listing VMs by hand. If you find yourself writing the same
  VMs in two ranges, write a blueprint.
- A new blueprint needs a test in `tests/test_tnrange.py` and must compile inside CANARY or a test range.
- `validation.probe_vm` is mandatory and must be a Linux VM with guest access. Isolation is
  always tested, even if a definition omits it.

## Hard rules

These are enforced by the guard hook, the CLI and the playbooks. Do not try to work around them.
If one is in the way, stop and explain why to the human.

1. One range ID and one playbook per run. No `all`, no wildcards, no loops over ranges.
   Call `tools/range`, `ansible-playbook` and `govc` directly, never through `sudo`, `bash -c`,
   `xargs`, `eval`, pipes or `python`. No `-e @file` (except `@vault/`), no JSON `-e` values.
2. `--check` first, always. Real runs only via `tools/range` with a human approval.
3. No `--skip-tags`, `--tags`, `--start-at-task`, or `-e tn_*` overrides.
4. Teardown is by tag, and refuses if any tagged VM is not in the plan. Never delete by name pattern. Never use govc or PowerCLI to change anything.
5. Templates are inputs, never outputs. A build never modifies a template. Only templates that are
   `validated: true` in `config/templates.yml`, with provenance and the vCenter tag
   `truenorth-template:validated`, may be cloned.
6. Isolation is proven by the probe, not assumed. A failed probe self-test makes isolation FAIL (inconclusive).
7. No secrets in files, plans, logs or chat. Credentials come from vault variables only. Do not read `vault/`.
8. No changes during a blackout window (`config/blackout.json`).
9. Do not edit `.claude/hooks/`, `.claude/settings.json`, `.claude/approvals/`, `config/blackout.json`,
   `build/` or `ranges/.allocations.json`. The CLI writes the last two.
10. An existing range is never modified in place. To change a built range: teardown, then build.

Details and rationale: [reference/safety.md](reference/safety.md)

## What "pristine" means here

A build is accepted only when all of these hold. See [reference/acceptance.md](reference/acceptance.md).

| Test | How it is checked |
|---|---|
| Reproducible | Same inputs give the same `plan_sha256`. `verify-plan` refuses stale plans |
| Idempotent | A second `build` reports no changes to existing objects |
| Isolated | Probe cannot reach `site.isolation_probe_targets`; self-test passed |
| Connected | Every VM reports its planned IPs; the probe reaches every gateway and listed service |
| Baseline | Every VM has exactly one `tn-baseline` snapshot (powered off, no memory) |
| Resettable | `reset` reverts to baseline and validation passes again |
| Clean teardown | Zero tagged VMs remain; planned portgroups, folders, pool and tags are gone |
| Evidenced | `manifest.json` records template provenance, addressing, checks and hashes |

## Runbook format

Whenever you give the human steps to run, use this format:

```
Where to run:   <control node / repo root>
Command:        <exact command>
Expected:       <key output lines>
Validation:     <command that proves it worked>
Rollback:       <exact command to undo>
```

Rollbacks: a failed build is rolled back with `tools/range teardown <ID> --confirm <ID>`
(needs a `teardown` approval). A bad reset is fixed by rebuilding. A wrong definition is
fixed with `git revert` plus recompile.

## Other references

- Guest configuration and access models: [reference/guest-access.md](reference/guest-access.md)
- Golden images and template promotion: [reference/golden-images.md](reference/golden-images.md)
- Scenario to range pipeline (ATT&CK): [reference/scenario-pipeline.md](reference/scenario-pipeline.md)
- TrueNorth module integration (range activity, greyspace, ARC²): [reference/integrations.md](reference/integrations.md)
- Troubleshooting: [reference/troubleshooting.md](reference/troubleshooting.md)

## Before you finish any range task

- `python3 -m unittest discover -s tests` passes
- `ansible-lint playbooks/` passes
- `tools/range verify-plan <ID>` is current for every range you touched
- Run the `range-reviewer` agent on your diff. Fix anything it flags before you hand over.
