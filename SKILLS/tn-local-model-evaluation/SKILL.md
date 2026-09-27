---
name: tn-local-model-evaluation
description: "Compare or configure local coding-model candidates for Taz; use for H200 memory planning, serving compatibility, and repository task evaluations."
---

# Local model qualification

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `docs/ai-fleet-sizing.md`
- `RESUME.md`
- `ai-orchestrator/app/backends`
- `tests/ai_orchestrator`
- `CLAUDE.md`

## Workflow

1. Verify exact model ID/revision, quantization, license, serving engine, tool-call parser, and chat template against primary documentation. Aliases are not evidence of the served model.
2. Measure actual GPUs and interconnect when connected. Budget total expert weights, runtime workspaces, KV cache, context length, and concurrency; active MoE parameters do not determine weight memory.
3. Treat code generation, planning, visual review, and embeddings as different workloads. Select candidates on measured task outcomes rather than parameter count alone.
4. Run a fixed small repository evaluation set: route/context repair, API authorization, retry correctness, migration diagnosis, and honest test reporting. Use isolated copies or worktrees.
5. Record patches, test outcomes, tool-call errors, time, peak memory, and unsupported claims. Repeat important cases before promoting a model.
6. Preserve local-data handling rules, existing write-gate policy, and the rollback path. A model recommendation is not a fleet restart or policy-change authorization.

## Verification

Use [the repository evaluation cases](references/repo-evaluation.md) when comparing
candidates; keep requests and environments equivalent.

Report measured versus estimated fit and quality separately. Confirm tool use end to end before enabling writes under existing policy.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

A model/serving recommendation with source links, memory estimates, evaluation evidence, and a scoped deployment plan if requested.

## Boundary

Do not download large weights, switch shared GPU services, or relax write restrictions just to answer a selection question.
