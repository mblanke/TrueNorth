---
description: Review the current TrueNorth diff — independent Codex pass, reconciliation, targeted tests. Never commits.
argument-hint: "[subsystem or path to focus on]"
---

# /truenorth-review

Multi-perspective review of what has actually changed. Extends the existing nightly
GLM review (`/data/llm/night-review.sh`, 03:00 → `/data/llm/reports/`) rather than
duplicating it — that one is scheduled, read-only and repo-wide; this one is on-demand
and diff-scoped.

Focus: **$ARGUMENTS** (if empty, review the full working diff.)

## Steps

1. **Scope it.** `git status --short`, `git diff --stat`, `git diff`. Identify which
   subsystems are touched — API, worker, scenario engine, frontend, curriculum spine,
   infra, AI orchestration.
2. **Read the changed code properly** before asking anyone else. You are the integrator;
   do not outsource comprehension.
3. **Independent Codex pass.** Use the `codex` MCP tool for a review you did not lead:
   regressions, API-contract breaks, async/concurrency, test gaps, security.
   Ask for its own conclusion — do not hand it yours first (pack §20).
4. **Reconcile.** Where you and Codex disagree, resolve it against the code and say
   which view won and why. Disagreement is the point; do not average it away.
5. **Test what changed.** `.venv/bin/python -m pytest -q` for API/worker/scenario;
   `npx ng test --include='**/<changed>.spec.ts' --watch=false` for frontend.
   Baseline is **452 passed, 27 skipped** — a drop below that is a regression.
6. **Report**: findings ranked by severity, each with file:line; what you fixed; what
   you deliberately did not; residual risk.

## Hard rules

- **Never commit.** Reviewing is not merging.
- **Check which mode the box is in first**: `/opt/llm-stack/taz-status.sh`.
  - *Fleet* — four vLLM models concurrent. Fan out freely; Codex runs `coder-fast`
    (Qwen3-Coder), genuinely independent of anything else in the loop.
  - *Flagship* — GLM-5.2 alone, `--parallel 1`. **Serialize**: concurrent calls just
    queue and look like a hang. Codex falls back to the CPU Qwen, still independent
    but ~10 tok/s, so budget minutes per pass.
- **Consensus is not evidence.** Two models agreeing measures shared priors, not
  correctness. Use them to generate candidate problems, then confirm each by running
  something — a test, a query, a reproduction. A finding nobody executed is a guess.
