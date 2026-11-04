# Claude ↔ Taz worker protocol

Purpose: let Claude Code offload bounded TrueNorth work to Taz/GLM-5.3 without allowing
scope drift, silent fallback, invented requirements or self-certified completion.

## Trust model

Claude is supervisor and integrator. Taz is a capable but untrusted implementation worker.
Git, tests, generated contracts and independent review are the evidence. Neither model's prose
is evidence by itself.

Taz may inspect and edit only a disposable worktree on its assigned branch. It may not merge,
push to protected branches, change live infrastructure, use real learner data, alter shared
contracts or migrations without a Claude-issued contract token, or approve its own output.

## Qualification handshake

Before code-writing work, Claude asks Taz to return:

```yaml
served_model:
  id: exact server-reported model id
  revision: exact revision or digest
  quantization: exact method/dtype
  server: vLLM version
  tool_parser: exact parser
  chat_template: name or digest
  context_limit: integer
  concurrency: integer
  gpu_topology: observed layout
repository:
  path: disposable worktree path
  commit: pinned full SHA
  status: clean
```

Claude compares the response to direct server/Git evidence. A LiteLLM alias such as `glm-5.3`
does not satisfy this handshake. If the identity cannot be proven, Taz remains read-only.

Before promotion to program writes, run the fixed cases in
`SKILLS/tn-local-model-evaluation/references/repo-evaluation.md` and record exact prompts,
patches, test results, elapsed time, peak memory, invalid tool calls and unsupported claims.

## Ticket envelope Claude sends

```yaml
ticket_id: TN10X-<workstream>-<slice>
base_commit: <full SHA>
branch: <one branch>
objective: <one observable outcome>
skill: <one primary tn-* skill; optional second reviewer skill>
owned_files:
  - <paths or narrowly scoped globs>
read_only_context:
  - <paths>
forbidden_files:
  - docs/programs/truenorth-10x/STATUS.yaml
contracts:
  allowed: <none or exact accepted contract revision>
  migration_order_token: <none or integrator-issued token>
acceptance:
  - <testable behavior>
required_checks:
  - <targeted command>
  - scripts/dod.sh
stop_conditions:
  - missing policy or ambiguous contract
  - ownership conflict or dirty base
  - required live credential/data
  - change outside owned files
return_format: taz-return-v1
```

An assignment without this envelope is analysis-only.

## Conversation checkpoints

1. **Echo:** Taz restates objective, base SHA, owned files, acceptance tests and stop
   conditions. Claude corrects any mismatch before edits.
2. **Plan:** Taz proposes the smallest patch and tests. Claude issues `PROCEED`, `NARROW` or
   `STOP`. No `PROCEED`, no edit.
3. **Contract challenge:** if an API/schema/migration/shared event changes, Taz returns a diff
   proposal and stops. Claude either issues an exact contract/migration token or re-scopes.
4. **Implementation:** Taz commits locally and runs required targeted checks. It reports raw
   command/result summaries and never converts a blocked check into a pass.
5. **Adversarial self-check:** Taz lists the three most likely defects, searches for each and
   reports evidence. This is not the independent review.
6. **Return:** Taz emits the format below. Claude independently inspects diff and Git state,
   runs or delegates verification, and accepts/rejects the commit.

## Required return

```yaml
format: taz-return-v1
ticket_id: <id>
base_commit: <sha>
head_commit: <sha>
files_changed: []
behavior_delivered: []
contract_effect: none | <exact effect>
migration_effect: none | <revision and parent>
checks:
  - command: <exact command>
    result: pass | fail | blocked
    evidence: <count/error/artifact>
uncertainties: []
likely_defects_checked: []
scope_deviations: []
recommended_next_action: <one action>
```

Claude rejects the return if the commit is absent, the base moved, files exceed ownership,
test claims lack commands, an alias is presented as model identity, or the worker says “done”
while a required check is failed/blocked.

## Anti-hallucination rules

- Taz labels every important statement `OBSERVED`, `INFERRED`, `PROPOSED` or `UNKNOWN`.
- `UNKNOWN` triggers a question or stop; it never becomes an implementation assumption.
- Source pointers include file and line or command output. Historical docs are leads only.
- Taz may not invent test counts, API behavior, policy, classification markings, hardware
  capacity or a successful deployment.
- Taz does not weaken a test, skip a gate, add a broad exception or mock the behavior under
  test merely to obtain green output.
- After two failed approaches or a repeated tool error, Taz stops and returns the evidence;
  it does not widen scope.
- Claude periodically asks, “What evidence would falsify your current conclusion?” Taz must
  name and run the cheapest safe check before continuing.

## Efficient division of labour

Good Taz tickets: route/test repair, one API vertical slice with accepted schema, isolated
Angular component behavior, test generation, migration diagnostics, repository-wide pointer
search, docs/source reconciliation and long local gates.

Keep with Claude: product decisions, classification/safety policy, shared event schemas,
Alembic ordering, multi-workstream conflict resolution, live operations, acceptance and merge.
