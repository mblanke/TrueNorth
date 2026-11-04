# Office brief: install and commission the TrueNorth range kit

**Audience:** Claude, in a Claude Code session on the Ansible control node at the office.
**Owner:** Marc-Andre Blanke.
**Supersedes:** `build-truenorth-infra-skill.md` and `build-truenorth-range-skill.md`. Ignore both.

## Goal

Install this kit into the TrueNorth repo, fill in the site facts from read-only discovery, prove it
in check mode against the real vCenter, and hand over a CANARY build that a human approves.

The kit is already built and unit-tested: the compiler, playbooks, hooks, skill, reviewer agent,
blueprints, canary, CI and a Packer skeleton. Your job is to **commission** it, not redesign it.
If you think something should change, list it in the handover. Do not rewrite it.

## Rules (read first)

1. **Read-only until the human approves.** No playbook without `--check`. No govc write verbs.
   The only real run in this brief is the CANARY build, and only after an approval exists.
2. **Install the hooks before you do anything else** (step 1). Once they are in place they enforce the rules for you.
3. **Never read, print or write secrets.** Vault values stay in `vault/`. You record the method, never the values.
4. **Do not invent facts.** Anything discovery cannot confirm stays `TBD`, and the compiler will refuse to build.
5. **Ask once.** Collect every question for Marc-Andre into one message (step 6).
6. Use the runbook format from the skill for anything you hand to a human.

## Step 0: Find the repo and control node facts

```bash
git rev-parse --show-toplevel
ansible --version | head -1
ansible-galaxy collection list 2>/dev/null | grep -E "community.vmware|vmware.vmware"
python3 -c "import pyVmomi, yaml; print('pyvmomi', pyVmomi.__version__ if hasattr(pyVmomi,'__version__') else 'ok')"
python3 -c "from vmware.vapi.vsphere.client import create_vsphere_client; print('vsphere sdk ok')"
ls ansible.cfg collections/requirements.yml 2>/dev/null
```

Expected: ansible-core 2.16 or later; community.vmware 6.x and vmware.vmware 2.x (the kit was tested
against 6.5.0 and 2.11.0); pyVmomi and the vSphere Automation SDK present.
If a collection or SDK is missing, write down the install command for the human. Do not install system-wide yourself.

## Step 1: Install the kit (hooks first)

The kit's paths are relative to the repo root. If the repo already has any of these paths, stop and list the conflicts.

| Kit path | Purpose |
|---|---|
| `.claude/settings.json`, `.claude/hooks/guard.py` | Guard hook. **Merge** with an existing settings.json; do not overwrite |
| `.claude/skills/truenorth-range-ansible/` | The skill and its references |
| `.claude/agents/range-reviewer.md` | Read-only reviewer agent |
| `tools/range`, `tools/tnrange/` | Compiler and CLI |
| `config/` | `site.yml`, `templates.yml`, `blackout.json`, `role_map.yml` |
| `blueprints/`, `ranges/` | Building blocks and range definitions (CANARY, EXCOURSE example) |
| `playbooks/` | Range playbooks and roles |
| `tests/`, `ci/`, `packer/`, `vault/README.md`, `.gitignore`, `.ansible-lint` | |

After copying:

```bash
python3 -m unittest discover -s tests      # expect: OK (50 tests)
ansible-lint playbooks/                    # expect: Passed
for p in playbooks/*.yml; do ansible-playbook --syntax-check -i localhost, "$p" -e range_id=CANARY; done
```

The `.claude/settings.json` changes take effect for the next Claude Code session, so ask the human to restart
the session after step 1. Then confirm the hook is live: `govc vm.destroy x` must be BLOCKED.

## Step 2: Vault, credentials and approval key (human does this)

Recommended: create the approval signing key (see `ci/README.md`) and run Claude Code under an account
that cannot read it. Then approvals cannot be forged even if the guard hook is bypassed.


Ask Marc-Andre to create `vault/vcenter.yml` per `vault/README.md` and to export:

```bash
export TNRANGE_ANSIBLE_ARGS="-e @vault/vcenter.yml --vault-id tn@prompt"
```

The service account should be least-privilege, scoped to the range folder, the range resource pool and the range switch.

## Step 3: Read-only discovery

Use govc **read-only** verbs only (the hook allows them), with credentials coming from the environment the human set up.

```bash
govc about
govc ls /*/host
govc find / -type DistributedVirtualSwitch
govc ls /*/network
govc datastore.info
govc find / -type m -config.template true
govc tags.category.ls ; govc tags.ls
tools/range discover          # writes discovery/live-network.json (VLANs in use)
```

Fill `config/site.yml` from the results. Leave anything you cannot confirm as `TBD`:

| Field | From |
|---|---|
| `vcenter.datacenter`, `cluster`, `dvs`, `datastores_allowed` | govc output; confirm the range switch and datastores with Marc-Andre |
| `isolation_mode` | **Marc-Andre / network team decision**: `uplink_none` or `vlan_trunk_no_l3` (see reference/safety.md) |
| `vlan_pool`, `reserved_vlans` | Network team; cross-check with `discovery/live-network.json` |
| `cidr_pool`, `reserved_cidrs` | Network team; must not overlap any DND network |
| `isolation_probe_targets` | Management gateway, DNS, vCenter: addresses that answer when reachable |

Fill `config/templates.yml` for templates that exist and have known provenance. Leave the rest `validated: false`.
Do not promote templates. Promotion is a human step (reference/golden-images.md).

Record discovery facts in `discovery/notes.md` (git-ignored). No secrets.

## Step 4: Compile and check CANARY

```bash
tools/range validate CANARY
tools/range compile CANARY --write-allocation
tools/range check CANARY
```

Expected: `BUILDABLE: yes` once site and templates are complete. Check mode ends with `failed=0`.
In check mode, clone and guest steps are skipped while folders and pools do not exist yet. That is expected.

If BUILDABLE is NO, list the blockers in the handover. Each one maps to a question or a human action.

## Step 5: Review

Run the `range-reviewer` agent on the installed kit and on the config changes. Give it the diff, not your conclusions.
Include its verdict in the handover.

## Step 6: Handover (one message)

Send Marc-Andre:

1. What was installed and the test, lint and syntax results.
2. The filled `config/site.yml` values, with the source of each.
3. The CANARY plan (`build/CANARY/plan.txt`) and the check-mode result.
4. Remaining blockers and TBDs.
5. Questions, all at once:
   - Isolation model: `uplink_none` or `vlan_trunk_no_l3`? Who in the network team confirms?
   - Range VLAN pool, CIDR pool and reserved ranges.
   - Isolation probe targets.
   - Which templates are trusted, and their provenance (build ID, date, ISO checksum)?
   - Hardening baseline for images (CIS or STIG)?
   - Exercise blackout windows for `config/blackout.json`.
   - Should template building live in this repo (`packer/`) or elsewhere?
   - Integration contracts for range activity, greyspace and ARC² (owner and format).
6. The runbook for the first real build, for the human to run:

```
Where to run:   control node, repo root
Command:        tools/range approve CANARY --action build --action reset --action teardown --hours 4
                tools/range build CANARY
Expected:       PLAY RECAP failed=0; "PASS: wrote build/CANARY/manifest.json"
Validation:     cat build/CANARY/manifest.md      (verdict PASS, isolation checks present)
                tools/range drift CANARY          (no drift)
Rollback:       tools/range teardown CANARY --confirm CANARY
```

## Known limits (state these in the handover)

- An independent code review found 18 defects before handover, including teardown scope, guard bypasses,
  VM delete and power operations. All are fixed and covered by tests where possible. Re-test teardown
  carefully on the first CANARY run, because it has only been exercised against compiled plans and the simulator.

- Exercised so far: unit tests (compiler, allocation, scenario, manifest, drift, gates, hook) and check mode against a
  vCenter simulator. Real clone, tagging, guest operations, snapshots and teardown have **not** run against a real
  vCenter yet. The first CANARY build is that test. Watch it.
- Guest scripts (`files/linux/common.sh`, `files/windows/common.ps1`) need checking on each template OS.
- Role content (AD build-out and so on) is not included. It is exercise content.
- `ssh`/`winrm` guest access is not implemented. Use `tools`.
- The Packer template has not been through `packer validate`.
