# TrueNorth range kit

Ranges as code for TrueNorth on vCenter. You write a declarative definition, a compiler turns it
into a deterministic, hash-stamped plan, and Ansible builds exactly that plan. Claude Code is fenced
in by a guard hook and a human approval gate.

```
ranges/<ID>.yml + blueprints/ + config/
          │  tools/range compile           (validate, compose, allocate VLAN/CIDR/IP, hash)
          ▼
build/<ID>/plan.json ── tools/range check ──► ansible --check
          │  human: tools/range approve <ID> --action build
          ▼
tools/range build ──► preflight ► tags ► portgroups ► clone ► guest config ► baseline ► validate ► manifest
tools/range reset  ──► revert baseline ► validate ► manifest
tools/range drift  ──► live vs plan
tools/range teardown --confirm <ID>  ──► by tag, refuses if anything tagged is outside the plan
```

## Start here

- Office commissioning steps: [BRIEF.md](BRIEF.md)
- How Claude works with it: [.claude/skills/truenorth-range-ansible/SKILL.md](.claude/skills/truenorth-range-ansible/SKILL.md)
- Definition format: [reference/schema.md](.claude/skills/truenorth-range-ansible/reference/schema.md)

## Quick start (after site config is filled)

```bash
tools/range validate CANARY
tools/range compile CANARY --write-allocation
tools/range check CANARY
tools/range approve CANARY --action build --hours 4     # human, own terminal
tools/range build CANARY
cat build/CANARY/manifest.md
```

## Layout

| Path | What |
|---|---|
| `tools/range`, `tools/tnrange/` | CLI and compiler (Python 3.10+, PyYAML) |
| `config/site.yml` | vCenter targets, isolation model, VLAN/CIDR pools, probe targets |
| `config/templates.yml` | Template catalogue with provenance |
| `config/blackout.json` | Exercise and maintenance windows |
| `config/role_map.yml` | Scenario role → blueprint mapping |
| `blueprints/` | `ad_forest`, `dmz`, `attacker_enclave`, `core_router` |
| `ranges/` | `CANARY` (nightly canary), `EXCOURSE` (4-student example); `.allocations.json` slot registry |
| `playbooks/` | build, reset, validate, teardown, drift, discovery, template promote |
| `.claude/` | guard hook, settings, skill, reviewer agent |
| `ci/` | nightly canary and PR checks |
| `packer/` | golden image skeleton (optional) |
| `tests/` | unit tests for compiler and hook |

## Tested

- 50 unit tests pass (compiler, allocation, plan integrity, scenario conversion, manifest, drift, approval and blackout gates, guard hook including known bypass attempts).
- Two rounds of independent review: the first found 18 defects (3 critical), the second found 2 more critical guard/approval gaps. All are fixed and covered by tests or simulator runs.
- ansible-lint (production profile) passes. All playbooks pass syntax check.
- Against a vCenter simulator: check mode creates nothing; a planted VLAN collision is caught; a real run with no approval is refused inside the playbook before anything is created; with approval it creates portgroups, folders and pools, up to cloning (which the simulator cannot do).
- **Not yet run against a real vCenter**: clone, tagging, guest operations, snapshots, teardown. The first CANARY build is that test.
