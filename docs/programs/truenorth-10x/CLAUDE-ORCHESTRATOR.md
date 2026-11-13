# Claude Code program-orchestrator work order

Give this file to one Claude Code session as the persistent program integrator. Do not hand
the entire 10x program to a single implementation branch.

## Mission

Manage the TrueNorth foundation-closure and 10x delivery program as a dependency-aware set of
small, verified pull requests and evidence runs. Keep both lanes visible, run only
dependency-ready work in parallel, preserve security and tenant boundaries, and make no
implementation claim without repository evidence.

## Required startup

1. Read `CLAUDE.md`, `docs/AGENTS.md`, `docs/programs/truenorth-10x/README.md`,
   `docs/programs/truenorth-10x/CURRENT-GAP-MAP.md` and
   `docs/programs/truenorth-10x/STATUS.yaml`. If Taz will be used, also read
   `docs/programs/truenorth-10x/TAZ-WORKER-PROTOCOL.md` before contacting it.
2. Inspect `git status`, current branch/HEAD, configured remotes, open PRs and the Alembic
   heads. Treat `RESUME.md` and `docs/current-state.md` as historical leads only.
3. Confirm whether Taz is available. If using it, capture the exact served GLM-5.3 model ID,
   revision, quantization, serving version, tool parser/template, context, concurrency and GPU
   topology. Do not accept an alias as proof and do not silently use a fallback.
4. Select the highest-priority `ready` foundation or capability slice. Read only that packet
   and its matching skills.
5. Before creating code, state branch name, owned files, shared files, acceptance tests,
   migration/contract effect and dependencies.

## Coordination rules

- You are the only editor of `STATUS.yaml` and shared program contracts.
- Use one branch/worktree per PR. Suggested names are recorded in each packet.
- Maximum working set: you plus two non-overlapping implementation workers and one independent
  reviewer. Fewer is fine. Never create parallel Alembic revisions without assigning an
  explicit order.
- Workers may inspect the shared repository but may edit only their declared files.
- Give each worker its packet, relevant skill and acceptance criteria—not this entire program.
- Use Taz/GLM-5.3 for bounded implementation, tests, indexing and independent analysis after
  qualification. Every conversation follows `TAZ-WORKER-PROTOCOL.md`; never let it merge,
  approve itself, touch live infrastructure or process real learner data.
- Use GitHub issues/PRs/checks as coordination evidence when the connector or `gh` is available.
  The versioned files in this directory remain the source of program intent.
- Use Matt Pocock `grill-me`/`grill-with-docs` for unresolved owner decisions, `to-spec` and
  `to-tickets` for accepted work, `wayfinder` for multi-session decision tracking, and
  `claude-handoff` before context loss. These are owner-invoked where the repository toolkit
  says so. Do not emulate an unavailable owner-only skill.
- Do not use `loop-me`; the repository excludes it. Run the bounded loop in `README.md`.

## First execution sequence

1. Run F0's current-source reconciliation. In parallel, implement workstream 00 as one small
   PR; neither needs a schema contract.
2. Audit F2 required checks and F5 release limitations against current GitHub/source. Do not
   turn an already-resolved stale limitation into new code.
3. Start F1, F4 and F6 with separate owners. Record external environment/authority blockers
   for F3 and F7 rather than replacing them with simulator evidence.
4. Open decision/contract PRs for workstreams 01, 02 and 06. They may be researched in
   parallel, but the integrator resolves shared event identity, provenance fields and
   migration order.
5. After those contracts merge, run 01 and 02 in parallel; 03 may run independently; 06 may
   run once its classification/identity decisions are answered.
6. Do not start 04 until 02 has an accepted event/effect envelope. Do not start automated
   physical effects in 05; its first slice is manual and truthful.
7. Start 07 only after lifecycle and exchange contracts are stable enough to test a cell.

No capability is release-ready while one of its `foundation_release_gates` in `STATUS.yaml`
is open.

## Required worker return

Each worker must return exactly:

- branch and commit;
- accepted ticket/slice;
- files changed;
- schema, migration, API or event-contract effect;
- targeted checks and exact results;
- `scripts/dod.sh` result or exact environmental blocker;
- risks/assumptions remaining;
- recommended next action.

Reject a handoff that says only “tests pass,” uses stale counts, omits a foreign-tenant or
failure-path test where applicable, or changes scope without an explicit decision.

## Stop conditions

Stop and ask the owner when classification policy, accreditation, safety authority, vendor
protocol, authoritative-record ownership or an irreversible migration choice is missing. A
blocked decision is progress when it is documented precisely; guessing is not.
