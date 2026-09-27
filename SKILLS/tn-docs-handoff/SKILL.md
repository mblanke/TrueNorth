---
name: tn-docs-handoff
description: "Reconcile TrueNorth docs with source and prepare compact handoffs; use after behavior changes or when setup/status documents disagree."
---

# Accurate documentation and handoff

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `docs/README.md`
- `docs/current-state.md`
- `RESUME.md`
- `CLAUDE.md`
- `Makefile`
- `docs/SKILLS.md`

## Workflow

1. Identify which document owns the fact and which documents merely reference it. Prefer one maintained source over copying commands and claims everywhere.
2. Verify commands, paths, ports, feature status, and test evidence against current code/configuration. Date observations and distinguish intent from implementation.
3. Record user decisions, current objective, completed work, exact files, checks, blockers, and next action in a compact handoff.
4. Never include secrets, private endpoint credentials, or restricted source material in transferable instructions.
5. Preserve useful history but label superseded observations. Do not overwrite a large document from memory after compaction.
6. Keep operating guidance task-selective; models should load the relevant skill, not every manual for every edit.

## Verification

Check local links and command paths, remove unsupported completion claims, and confirm the handoff can resume without redoing finished work.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Focused documentation updates or a resumable handoff with verified references and explicit uncertainty.

## Boundary

Documentation is not evidence that an unexecuted test, deployment, or infrastructure operation succeeded.

