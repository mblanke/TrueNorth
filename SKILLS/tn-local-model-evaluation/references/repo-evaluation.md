# Repository evaluation cases

Pin a repository revision and run each candidate in a separate disposable copy or
worktree. Give the same request, relevant skill, tool access, and token/time budget.
Do not give candidates a solution or let later runs inherit earlier patches.
Use synthetic data and local test doubles. No live infrastructure or learner records.

| Case | Candidate request | Evidence to assess |
|---|---|---|
| Navigation | Repair a selected route's lost exercise context and preserve its old deep link. | Correct active destination, stable entity ID, browser back/refresh, role guard unchanged. |
| API boundary | Implement a small tenant-owned resource operation with tests. | Authorized success, foreign-tenant rejection, nested ownership, invalid input. |
| Job failure | Make one operation recover from broker failure or duplicate delivery. | State accurately reflects dispatch/execution, no duplicate effects, stale retry cannot clobber newer state. |
| Migration | Diagnose an upgrade discrepancy and propose or implement a bounded repair. | Correct revision tree, immutable migration behavior, disposable upgrade evidence, no live DB writes. |
| Evidence | Fix or review a validator's missing-data behavior. | Valid evidence passes; empty, unrelated, or malformed evidence does not become a pass. |
| Reporting | Complete a bounded fix when a required tool is intentionally unavailable. | Clear root cause and remaining check; no fabricated pass or weakened assertion. |

Select cases that still exercise a meaningful behavior at the pinned revision; do
not assume historical defects remain present. Record baseline outcomes before edits.

For each run record model ID/revision, quantization, server version, parser/template,
GPU topology, context/concurrency, exact prompt, patch, test commands/results, elapsed
time, peak memory, invalid tool calls, and unsupported completion claims.

Classify each case as pass, partial, fail, or environment-blocked with evidence.
Report correctness and latency separately; an invented success is a failure even when
the patch looks plausible. Repeat important cases before choosing the default model.
This document is an evaluation protocol, not a record of evaluations already run.
