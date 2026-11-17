# Foundation F1 — clean-room installation and operations

Status: proposed after F0. Suggested branch: `claude/foundation-f1-clean-room`.

## Closure outcome

A fresh operator, using only released artifacts and maintained guides, installs and upgrades
TrueNorth, enables selected optional components, verifies health, restores a backup, proves
alert delivery and returns the system to service without undocumented decisions.

## Current foundations

The repository has an Ansible installer, staged playbooks, backup/restore runbooks, acceptance
scripts, alert tests and an optional Moodle stage (`install/playbooks/85-moodle.yml`, installer
variables and `docs/runbooks/moodle.md`). F1 qualifies those paths; it does not assume the
older “Moodle is not in the installer” statement remains true.

## Delivery slices

1. Reconcile open clean-room documentation issues against current UI, variables, playbooks and
   scripts. Convert every command/path/port claim into a check where practical.
2. Build and validate the guide/PDF bundle and a redacted operator worksheet containing every
   required input and decision owner.
3. On a clean supported host, install the signed candidate, including optional Moodle when in
   scope; record inventory, version/digest, duration, prompts, deviations and acceptance output.
4. Upgrade from the supported predecessor and execute documented rollback/recovery boundaries.
5. Run backup/restore including databases, MinIO, OpenSearch, ARC² state and Moodle when
   enabled; prove declared quiescence and post-restore application behavior.
6. Deliver an actual Alertmanager notification through the approved staging channel and prove
   failed-delivery visibility.

## Acceptance

- A new operator completes the path without repository-author coaching or guessed values.
- First install and second idempotent run produce expected state.
- Moodle sign-in, publication, Student attempt/result pull and backup/restore pass when enabled.
- Backup manifests identify partial/unquiesced components and restore refuses unsafe inputs.
- Upgrade/rollback evidence names data/schema boundaries and recovery point.
- Hardware-specific untested commands remain labelled until executed on that hardware.
- Guides, installer defaults, validation messages and screenshots agree at the tested commit.

## Non-goals

No live production deployment, destructive production restore or claim that one clean-room
topology qualifies every supported topology.

## Recovery

Use disposable hosts/snapshots and pre-upgrade backups. Record cleanup and revert steps before
the run. A failed run remains evidence with the exact stopping point.

## Skills

Primary: `tn-docs-handoff`; add `tn-release-verification` and `tn-incident-diagnosis`.
Site operator owns physical execution; models prepare/run safe checks and reconcile evidence.
